"""``POST /api/chat`` — Server-Sent Events streamed chat endpoint.

The browser sends a ``messages`` array (OpenAI-style ``role``/``content``
turns). The endpoint runs the Claude tool-use loop and streams the
final response back as SSE ``data:`` events. Each event payload is a
JSON object ``{"text": "..."}``; the stream terminates with the
literal line ``data: [DONE]``.
"""

from __future__ import annotations

from fastapi import APIRouter, Body  # pyright: ignore[reportMissingImports]
from fastapi.responses import StreamingResponse  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel  # pyright: ignore[reportMissingImports]

from backend.services.chat_handler import chat_sse_stream

router = APIRouter(prefix="/api", tags=["chat"])


class ChatMessage(BaseModel):
    """Single chat turn. ``role`` is ``"user"`` or ``"assistant"``."""

    role: str
    content: str


class ChatRequest(BaseModel):
    """Request body for ``POST /api/chat``."""

    messages: list[ChatMessage]


@router.post("/chat")
async def chat(req: ChatRequest = Body(...)) -> StreamingResponse:
    """SSE-streamed chat endpoint. Returns text chunks. Final event: ``[DONE]``."""

    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    return StreamingResponse(
        chat_sse_stream(msgs),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
