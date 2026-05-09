"""FastAPI entry point for the NE Renewable Siting backend.

Endpoints:

* ``GET /api/health``
* ``GET /api/parcel/{parcel_id}``  /  ``GET /api/parcel/at?lng=X&lat=Y``
* ``GET /api/substation/{name}``  /  ``GET /api/substation/search?q=...``
* ``GET /api/repd/search`` (filterable)
* ``GET /api/ownership/{by_title,by_postcode,by_proprietor,nearest}``
* ``POST /api/chat`` (Claude tool-use, SSE-streamed)

Run with: ``uv run uvicorn backend.main:app --reload``
"""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv  # pyright: ignore[reportMissingImports]
from fastapi import FastAPI  # pyright: ignore[reportMissingImports]
from fastapi.middleware.cors import CORSMiddleware  # pyright: ignore[reportMissingImports]
from fastapi.staticfiles import StaticFiles  # pyright: ignore[reportMissingImports]

# Load .env early so ANTHROPIC_API_KEY etc. are available before any router imports.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.routers import chat, ownership, parcel, repd, substation  # noqa: E402

app = FastAPI(title="NE Renewable Siting API", version="0.1.0")

# Allow any localhost port for local dev (vite picks 5174, 5175 etc. when 5173 busy).
# Production is same-origin so this regex doesn't loosen anything in prod.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(parcel.router)
app.include_router(substation.router)
app.include_router(repd.router)
app.include_router(ownership.router)
app.include_router(chat.router)


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, str]:
    """Liveness probe — also useful as a smoke test for tooling."""

    return {"status": "ok"}


# Serve the built frontend at / when present (production deployment).
# Mounted last so /api/* routes always win.
_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _DIST.exists():
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="frontend")
