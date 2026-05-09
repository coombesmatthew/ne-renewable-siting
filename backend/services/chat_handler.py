"""Chat handler — runs Claude tool-use loop and yields SSE chunks.

The endpoint streams text chunks back to the browser as Server-Sent
Events using ``client.messages.stream(...)`` so the user sees text
appear character-by-character as the model writes it.

Typed event payload schema:

* ``{"type": "tool_call", "name": "<tool_name>"}`` — surface as 🔧 chip
* ``{"type": "tool_result", "name": "<tool_name>", "summary": "..."}`` —
  finish the chip with a one-line UX summary
* ``{"type": "text", "text": "<delta>"}`` — append delta to current
  assistant message bubble
* ``{"type": "error", "error": "<message>"}`` — terminal
* ``[DONE]`` — terminal

The Anthropic SDK's streaming API exposes a synchronous context
manager. To keep the FastAPI worker responsive we drive the stream on a
background thread and pipe events back to the async generator through a
thread-safe queue.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import AsyncGenerator
from queue import Queue
from typing import Any

from anthropic import Anthropic  # pyright: ignore[reportMissingImports]

from backend.services.claude_tools import (
    TOOL_DEFS,
    _summarize_tool_result,
    execute_tool,
)

SYSTEM_PROMPT = """You are an assistant for an interactive map of renewable-energy siting opportunities in NE England (~33,000 land parcels >=2 ha across 12 LADs, plus NPg substations GSP/BSP/Primary, DESNZ REPD pipeline + operational projects, planning constraints).

Style:
- Be concise. Bullets for lists.
- Always cite specific parcel_ids, substation names, and REPD project names you reference — they're the link back to the map.
- Never apologise or hedge ("I'd be happy to..."). Just answer.
- If a question is off-topic, briefly redirect.
- Use the tools — never invent data.

Tools:
- find_parcels(...) — broad parcel filtering (the most powerful tool; combine multiple criteria)
- get_parcel(parcel_id | lng+lat) — single parcel by ID or location
- search_substations(q, limit) — find substations by name substring
- search_repd(tech, status, capacity, bbox, limit) — pipeline + operational project search
- sample_renewables_at(lng, lat) — solar PVOUT and wind at an arbitrary point

Worked patterns:
- "Find 5 parcels >10 ha with wind > 8 m/s within 5 km of a 33 kV substation, no AONB" → find_parcels with min_area_ha=10, min_wind_speed_100m_ms=8, max_dist_substation_gen_headroom_m=5000, min_voltage_kv="33", exclude_aonb=true, limit=5
- "Operational solar farms over 5 MW in County Durham" → search_repd with tech=["Solar Photovoltaics"], status=["Operational"], min_capacity_mw=5 — then narrow by inspecting county field in results
- "Compare wind on parcel NE-001234 vs the nearest hilltop" → get_parcel for the parcel, sample_renewables_at for the hilltop coordinate"""

MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_ROUNDS = 6


async def chat_sse_stream(
    messages: list[dict[str, Any]],
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted lines streaming a Claude tool-use response.

    The Anthropic streaming context manager is synchronous; we run it on
    a worker thread and pipe events through a queue so this generator
    stays async-friendly.
    """

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        yield (
            "data: "
            + json.dumps({"type": "error", "error": "ANTHROPIC_API_KEY not configured"})
            + "\n\n"
        )
        yield "data: [DONE]\n\n"
        return

    q: Queue[Any] = Queue()
    SENTINEL = object()

    def producer() -> None:
        try:
            client = Anthropic(api_key=api_key)
            msgs: list[dict[str, Any]] = list(messages)

            for _ in range(MAX_TOOL_ROUNDS + 1):
                with client.messages.stream(
                    model=MODEL,
                    max_tokens=2048,
                    system=SYSTEM_PROMPT,
                    tools=TOOL_DEFS,
                    messages=msgs,
                ) as stream:
                    current_tool_use: Any = None
                    current_tool_input_json = ""
                    for event in stream:
                        et = getattr(event, "type", None)
                        if et == "content_block_start":
                            block = getattr(event, "content_block", None)
                            if block is not None and getattr(block, "type", None) == "tool_use":
                                current_tool_use = block
                                current_tool_input_json = ""
                                q.put(
                                    {
                                        "type": "tool_call",
                                        "name": getattr(block, "name", ""),
                                    }
                                )
                        elif et == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            dt = getattr(delta, "type", None) if delta else None
                            if dt == "text_delta":
                                q.put(
                                    {
                                        "type": "text",
                                        "text": getattr(delta, "text", ""),
                                    }
                                )
                            elif dt == "input_json_delta":
                                current_tool_input_json += getattr(delta, "partial_json", "")
                        elif et == "content_block_stop" and current_tool_use is not None:
                            try:
                                parsed = (
                                    json.loads(current_tool_input_json)
                                    if current_tool_input_json
                                    else {}
                                )
                            except json.JSONDecodeError:
                                parsed = {}
                            tool_name = getattr(current_tool_use, "name", "")
                            result = execute_tool(tool_name, parsed)
                            summary = _summarize_tool_result(tool_name, result)
                            q.put(
                                {
                                    "type": "tool_result",
                                    "name": tool_name,
                                    "summary": summary,
                                }
                            )
                            current_tool_use = None
                    final = stream.get_final_message()

                if final.stop_reason == "tool_use":
                    msgs.append(
                        {
                            "role": "assistant",
                            "content": [b.model_dump() for b in final.content],
                        }
                    )
                    tool_results: list[dict[str, Any]] = []
                    for tu in [b for b in final.content if getattr(b, "type", None) == "tool_use"]:
                        result = execute_tool(tu.name, dict(tu.input or {}))
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": json.dumps(result, default=str),
                            }
                        )
                    msgs.append({"role": "user", "content": tool_results})
                    continue
                # end_turn / max_tokens / stop_sequence — finished
                break
        except Exception as exc:  # pragma: no cover — surface to client
            q.put({"type": "error", "error": str(exc)[:200]})
        finally:
            q.put(SENTINEL)

    threading.Thread(target=producer, daemon=True).start()

    loop = asyncio.get_event_loop()
    while True:
        item = await loop.run_in_executor(None, q.get)
        if item is SENTINEL:
            break
        yield "data: " + json.dumps(item) + "\n\n"
        if isinstance(item, dict) and item.get("type") == "error":
            break
    yield "data: [DONE]\n\n"
