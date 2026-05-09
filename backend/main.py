"""FastAPI entry point for the NE Renewable Siting backend.

Endpoints:

* ``GET /api/health``
* ``GET /api/parcel/{parcel_id}``  /  ``GET /api/parcel/at?lng=X&lat=Y``
* ``GET /api/substation/{name}``  /  ``GET /api/substation/search?q=...``
* ``GET /api/repd/search`` (filterable)
* ``POST /api/chat`` (Claude tool-use, SSE-streamed)

Run with: ``uv run uvicorn backend.main:app --reload``
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI  # pyright: ignore[reportMissingImports]
from fastapi.middleware.cors import CORSMiddleware  # pyright: ignore[reportMissingImports]
from fastapi.staticfiles import StaticFiles  # pyright: ignore[reportMissingImports]

from backend.routers import chat, parcel, repd, substation

app = FastAPI(title="NE Renewable Siting API", version="0.1.0")

# Vite dev (5173) and preview (4173). Production is same-origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:4173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(parcel.router)
app.include_router(substation.router)
app.include_router(repd.router)
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
