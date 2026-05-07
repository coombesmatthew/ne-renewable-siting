"""REPD (Renewable Energy Planning Database) search endpoint.

The source GeoDataFrame uses verbose column names with spaces (e.g.
``"Site Name"``, ``"Technology Type"``). This module aliases them to
snake_case in the API response — see :class:`RepdProject`.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel  # pyright: ignore[reportMissingImports]

from backend.services.data_store import get_data_store

router = APIRouter(prefix="/api/repd", tags=["repd"])


# Mapping from canonical (API) name -> source (REPD) column name.
_COL = {
    "ref_id": "Ref ID",
    "site_name": "Site Name",
    "operator": "Operator (or Applicant)",
    "technology_type": "Technology Type",
    "installed_capacity_mw": "Installed Capacity (MWelec)",
    "development_status": "Development Status",
    "county": "County",
    "region": "Region",
    "country": "Country",
    "planning_application_reference": "Planning Application Reference",
}


class RepdProject(BaseModel):
    """Single REPD record. ``capacity_mw`` is parsed from the source
    string column ``Installed Capacity (MWelec)`` (which contains the
    occasional non-numeric value, hence ``Optional[float]``)."""

    ref_id: Optional[int] = None
    site_name: Optional[str] = None
    operator: Optional[str] = None
    technology_type: Optional[str] = None
    development_status: Optional[str] = None
    capacity_mw: Optional[float] = None
    county: Optional[str] = None
    region: Optional[str] = None
    country: Optional[str] = None
    planning_application_reference: Optional[str] = None
    lon: Optional[float] = None
    lat: Optional[float] = None


class RepdSearchResults(BaseModel):
    count: int
    """Number of matches returned (after ``limit``)."""
    total_matched: int
    """Total matches before ``limit`` was applied."""
    results: list[RepdProject]


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


def _to_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if s in ("", "nan", "NaN", "None"):
        return None
    return s


def _row_to_project(row: Any) -> RepdProject:
    geom = row.get("geometry")
    lon: Optional[float] = None
    lat: Optional[float] = None
    if geom is not None and not geom.is_empty:
        lon = float(geom.x)
        lat = float(geom.y)

    capacity = _to_float(row.get(_COL["installed_capacity_mw"]))

    ref_id_raw = row.get(_COL["ref_id"])
    try:
        ref_id = int(ref_id_raw) if ref_id_raw is not None else None
    except (TypeError, ValueError):
        ref_id = None

    return RepdProject(
        ref_id=ref_id,
        site_name=_to_str(row.get(_COL["site_name"])),
        operator=_to_str(row.get(_COL["operator"])),
        technology_type=_to_str(row.get(_COL["technology_type"])),
        development_status=_to_str(row.get(_COL["development_status"])),
        capacity_mw=capacity,
        county=_to_str(row.get(_COL["county"])),
        region=_to_str(row.get(_COL["region"])),
        country=_to_str(row.get(_COL["country"])),
        planning_application_reference=_to_str(row.get(_COL["planning_application_reference"])),
        lon=lon,
        lat=lat,
    )


@router.get("/search", response_model=RepdSearchResults)
def search_repd(
    tech: Optional[list[str]] = Query(
        None,
        description=(
            "Technology types to include (e.g. 'Solar Photovoltaics', "
            "'Wind Onshore', 'Battery', 'Small Hydro'). Multi-valued."
        ),
    ),
    status: Optional[list[str]] = Query(
        None,
        description=(
            "Development statuses to include (e.g. 'Operational', "
            "'Under Construction'). Multi-valued."
        ),
    ),
    min_capacity_mw: Optional[float] = Query(None, ge=0),
    max_capacity_mw: Optional[float] = Query(None, ge=0),
    bbox: Optional[str] = Query(
        None,
        description="Comma-separated 'minlon,minlat,maxlon,maxlat'",
    ),
    limit: int = Query(50, ge=1, le=1000),
) -> RepdSearchResults:
    """Filter the REPD by tech / status / capacity / bounding box.

    All filters are AND-combined. Returns ``count`` results plus the
    pre-limit ``total_matched`` so callers can paginate if needed.
    """

    store = get_data_store()
    df = store.repd

    # Pre-parse capacity once for filter + projection.
    capacity_series = df[_COL["installed_capacity_mw"]].apply(_to_float)

    mask = capacity_series.notna() | capacity_series.isna()  # all True

    if tech:
        tech_lower = {t.lower() for t in tech}
        mask &= df[_COL["technology_type"]].astype(str).str.lower().isin(tech_lower)

    if status:
        status_lower = {s.lower() for s in status}
        mask &= df[_COL["development_status"]].astype(str).str.lower().isin(status_lower)

    if min_capacity_mw is not None:
        mask &= capacity_series.fillna(-1) >= min_capacity_mw
    if max_capacity_mw is not None:
        mask &= capacity_series.fillna(float("inf")) <= max_capacity_mw

    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="bbox must be 'minlon,minlat,maxlon,maxlat'",
            ) from exc
        if len(parts) != 4:
            raise HTTPException(
                status_code=400,
                detail="bbox must have exactly 4 comma-separated floats",
            )
        minlon, minlat, maxlon, maxlat = parts
        # REPD rows are points, so a simple coordinate filter is fastest.
        xs = df.geometry.x
        ys = df.geometry.y
        mask &= (xs >= minlon) & (xs <= maxlon) & (ys >= minlat) & (ys <= maxlat)

    matched = df[mask]
    total = int(len(matched))
    page = matched.head(limit)
    results = [_row_to_project(r) for _, r in page.iterrows()]
    return RepdSearchResults(count=len(results), total_matched=total, results=results)
