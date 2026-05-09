"""Chat handler — runs Claude tool-use loop and yields SSE chunks.

The endpoint streams text chunks back to the browser as Server-Sent
Events. We synchronously run the Claude tool-use loop (because the
Anthropic streaming API + tool use is non-trivial) and then split the
final text into word chunks for visual flow.

This is good enough for the MVP. A future iteration could use the
streaming Anthropic API to surface tokens as they arrive.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from typing import Any

from anthropic import Anthropic  # pyright: ignore[reportMissingImports]

from backend.services.claude_tools import TOOL_DEFS, execute_tool

SYSTEM_PROMPT = """You are an assistant for an interactive map of renewable-energy siting opportunities in North East England. You have access to tools that let you query parcels, substations, REPD projects, and sample solar/wind resource rasters.

Scope: This map covers ~33,000 land parcels (>=2 ha) across the 12 NE England local authorities, plus Northern Powergrid substations (GSP/BSP/Primary), the DESNZ Renewable Energy Planning Database (operational + pipeline projects), and planning constraints (AONB, National Park, Green Belt, SSSI, flood zones, listed buildings, scheduled monuments).

Be concise. Use bullets for lists. Cite specific parcel IDs / substation names / REPD project names when answering. If a question is off-topic or outside NE England, briefly say so and redirect to the map's scope. Don't invent data — use the tools.

Note: the tools do NOT support broad parcel queries by attribute (e.g. "find parcels with good wind near a 33 kV substation with no AONB"). For that, direct the user to the filter panel in the map UI. The `get_parcel` tool only fetches a single parcel by ID or lng/lat.

Common questions you should handle:
- "What's the largest battery project under construction in Durham?" — use search_repd with tech=['Battery'], status=['Under Construction'].
- "Tell me about substation Hartmoor" — use search_substations.
- "What's the wind speed at lat/lon?" — use sample_renewables_at.
- "Tell me about parcel NE-001234" — use get_parcel."""

MODEL = "claude-haiku-4-5-20251001"


def _call_claude_with_tools(
    client: Anthropic,
    messages: list[dict[str, Any]],
    max_tool_rounds: int = 4,
) -> str:
    """Run the Claude tool-use loop and return the final assistant text."""

    for _ in range(max_tool_rounds + 1):
        resp = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFS,
            messages=messages,
        )

        if resp.stop_reason == "tool_use":
            # Echo the assistant turn back into history (required by the
            # API) and execute every tool call in this turn.
            messages.append(
                {
                    "role": "assistant",
                    "content": [b.model_dump() for b in resp.content],
                }
            )
            tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            tool_results: list[dict[str, Any]] = []
            for tu in tool_uses:
                result = execute_tool(tu.name, dict(tu.input or {}))
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result, default=str),
                    }
                )
            messages.append({"role": "user", "content": tool_results})
            continue

        # end_turn / max_tokens / stop_sequence — collect text blocks.
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    return "(max tool rounds reached)"


async def chat_sse_stream(
    messages: list[dict[str, Any]],
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted lines to stream a Claude response."""

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        yield 'data: {"error": "ANTHROPIC_API_KEY not configured"}\n\n'
        yield "data: [DONE]\n\n"
        return

    client = Anthropic(api_key=api_key)
    # Run the (synchronous) SDK call off the event loop so we don't
    # block the FastAPI worker.
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, _call_claude_with_tools, client, list(messages))

    # Stream in word chunks for visual flow. The frontend should
    # concatenate `text` fields until [DONE].
    for chunk in text.split(" "):
        yield f"data: {json.dumps({'text': chunk + ' '})}\n\n"
        await asyncio.sleep(0.02)
    yield "data: [DONE]\n\n"
