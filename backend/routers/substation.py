"""Substation lookup endpoints.

Both endpoints query the catchment GeoDataFrame (which carries identical
attributes to the points dataset, plus polygon geometry).
"""

from __future__ import annotations

import math
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel  # pyright: ignore[reportMissingImports]

from backend.services.data_store import get_data_store

router = APIRouter(prefix="/api/substation", tags=["substation"])


class Substation(BaseModel):
    """A subset of the headroom record useful for chat / popups."""

    name: str
    type: Optional[str] = None
    """``GSP``, ``BSP`` or ``Primary``."""
    local_authority: Optional[str] = None
    pvoltage: Optional[float] = None
    firm_cap: Optional[float] = None
    gentot: Optional[float] = None
    demtot: Optional[float] = None
    genhr: Optional[float] = None
    """Generation headroom (MW)."""
    demhr: Optional[float] = None
    """Demand headroom (MW)."""
    genconstraint: Optional[str] = None
    demconstraint: Optional[str] = None
    worst_case_constraint_gen_colour: Optional[str] = None
    worst_case_constraint_dem_colour: Optional[str] = None
    upstreamname: Optional[str] = None
    gsp_name: Optional[str] = None
    voltage_tier: Optional[str] = None
    centroid_lon: Optional[float] = None
    centroid_lat: Optional[float] = None


class SubstationSearchResults(BaseModel):
    query: str
    count: int
    results: list[Substation]


def _f(row: Any, key: str) -> Optional[float]:
    v = row.get(key)
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _s(row: Any, key: str) -> Optional[str]:
    v = row.get(key)
    if v is None:
        return None
    s = str(v)
    if s in ("nan", "NaN", "None", ""):
        return None
    return s


def _row_to_substation(row: Any) -> Substation:
    """Convert a single catchment row to a :class:`Substation`.

    The point-on-surface centroid is included when geometry is present
    so callers (chat, popups) can re-locate the station on a map.
    """

    centroid_lon: Optional[float] = None
    centroid_lat: Optional[float] = None
    geom = row.get("geometry")
    if geom is not None and not geom.is_empty:
        c = geom.centroid
        centroid_lon = float(c.x)
        centroid_lat = float(c.y)

    return Substation(
        name=str(row["name"]),
        type=_s(row, "type"),
        local_authority=_s(row, "local_authority"),
        pvoltage=_f(row, "pvoltage"),
        firm_cap=_f(row, "firm_cap"),
        gentot=_f(row, "gentot"),
        demtot=_f(row, "demtot"),
        genhr=_f(row, "genhr"),
        demhr=_f(row, "demhr"),
        genconstraint=_s(row, "genconstraint"),
        demconstraint=_s(row, "demconstraint"),
        worst_case_constraint_gen_colour=_s(row, "worst_case_constraint_gen_colour"),
        worst_case_constraint_dem_colour=_s(row, "worst_case_constraint_dem_colour"),
        upstreamname=_s(row, "upstreamname"),
        gsp_name=_s(row, "gsp_name"),
        voltage_tier=_s(row, "voltage_tier"),
        centroid_lon=centroid_lon,
        centroid_lat=centroid_lat,
    )


def _search_substations(df, q: str, limit: int = 10) -> list[Substation]:
    """Pure substring search helper — used by the HTTP endpoint and the
    Claude ``search_substations`` tool. Returns a list of
    :class:`Substation` (already serialisable via ``model_dump``)."""

    if not q:
        return []
    mask = df["name"].astype(str).str.contains(q, case=False, na=False)
    hits = df[mask].head(limit)
    return [_row_to_substation(r) for _, r in hits.iterrows()]


@router.get("/search", response_model=SubstationSearchResults)
def search_substations(
    q: str = Query(..., min_length=1, description="Substring to match against name"),
    limit: int = Query(10, ge=1, le=200),
) -> SubstationSearchResults:
    """Case-insensitive substring search by substation name."""

    store = get_data_store()
    results = _search_substations(store.substation_catchments, q, limit)
    return SubstationSearchResults(query=q, count=len(results), results=results)


@router.get("/{name}", response_model=Substation)
def get_substation(name: str) -> Substation:
    """Case-insensitive exact-name lookup. 404 if no station matches."""

    store = get_data_store()
    df = store.substation_catchments
    name_lower = name.lower()
    matches = df[df["name"].astype(str).str.lower() == name_lower]
    if matches.empty:
        raise HTTPException(
            status_code=404,
            detail=f"Substation {name!r} not found (try /api/substation/search?q=...)",
        )
    return _row_to_substation(matches.iloc[0])
