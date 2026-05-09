"""Parcel lookup endpoints.

- ``GET /api/parcel/{parcel_id}`` — fetch by ID.
- ``GET /api/parcel/at`` — point-in-polygon lookup at a lat/lon.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel  # pyright: ignore[reportMissingImports]
from shapely.geometry import Point  # pyright: ignore[reportMissingImports]

from backend.services.data_store import get_data_store

router = APIRouter(prefix="/api/parcel", tags=["parcel"])


class Parcel(BaseModel):
    """Strongly-typed view of a parcel row from
    ``data/processed/parcels_attributed.geojson``.

    Geometry is intentionally omitted — the frontend already has it from
    the PMTiles vector tile layer, so re-shipping per-parcel polygons
    would be wasteful.
    """

    parcel_id: str
    area_ha: float
    lad_code: Optional[str] = None
    lad_name: Optional[str] = None
    centroid_lon: float
    centroid_lat: float
    mean_pvout_kwhkwp: Optional[float] = None
    mean_wind_speed_100m_ms: Optional[float] = None
    dist_substation_gen_headroom_m: Optional[float] = None
    dist_substation_any_headroom_m: Optional[float] = None
    nearest_substation_name: Optional[str] = None
    intersects_aonb: bool = False
    intersects_national_park: bool = False
    intersects_green_belt: bool = False
    intersects_sssi: bool = False
    intersects_flood: bool = False


def _row_to_parcel(row) -> Parcel:
    """Convert a single parcels GeoDataFrame row to a :class:`Parcel`.

    Pandas/numpy NaN values are coerced to ``None`` so they serialise as
    JSON ``null`` rather than the string ``"NaN"``.
    """

    import math

    def _f(key: str) -> Optional[float]:
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

    def _s(key: str) -> Optional[str]:
        v = row.get(key)
        if v is None:
            return None
        s = str(v)
        if s in ("nan", "NaN", "None"):
            return None
        return s

    return Parcel(
        parcel_id=str(row["parcel_id"]),
        area_ha=float(row["area_ha"]),
        lad_code=_s("lad_code"),
        lad_name=_s("lad_name"),
        centroid_lon=float(row["centroid_lon"]),
        centroid_lat=float(row["centroid_lat"]),
        mean_pvout_kwhkwp=_f("mean_pvout_kwhkwp"),
        mean_wind_speed_100m_ms=_f("mean_wind_speed_100m_ms"),
        dist_substation_gen_headroom_m=_f("dist_substation_gen_headroom_m"),
        dist_substation_any_headroom_m=_f("dist_substation_any_headroom_m"),
        nearest_substation_name=_s("nearest_substation_name"),
        intersects_aonb=bool(row.get("intersects_aonb", False)),
        intersects_national_park=bool(row.get("intersects_national_park", False)),
        intersects_green_belt=bool(row.get("intersects_green_belt", False)),
        intersects_sssi=bool(row.get("intersects_sssi", False)),
        intersects_flood=bool(row.get("intersects_flood", False)),
    )


def _find_parcel_at(parcels, lng: float, lat: float):
    """Return the parcel row containing ``(lng, lat)`` or ``None``.

    Reusable helper used by both the HTTP endpoint and the Claude
    ``get_parcel`` tool.
    """

    pt = Point(lng, lat)
    sindex = parcels.sindex
    candidate_idx = list(sindex.query(pt, predicate="intersects"))
    if not candidate_idx:
        return None
    candidates = parcels.iloc[candidate_idx]
    hits = candidates[candidates.geometry.contains(pt)]
    if hits.empty:
        hits = candidates[candidates.geometry.intersects(pt)]
    if hits.empty:
        return None
    return hits.iloc[0]


def _find_by_id(parcels, parcel_id: str):
    """Return the parcel row matching ``parcel_id`` or ``None``."""

    matches = parcels[parcels["parcel_id"] == parcel_id]
    if matches.empty:
        return None
    return matches.iloc[0]


@router.get("/at", response_model=Parcel)
def parcel_at(
    lng: float = Query(..., ge=-180, le=180, description="Longitude (WGS84)"),
    lat: float = Query(..., ge=-90, le=90, description="Latitude (WGS84)"),
) -> Parcel:
    """Find the parcel containing the given ``(lng, lat)`` point.

    Uses the spatial index built at startup, so this is fast even on
    33K parcels. Returns 404 if no parcel contains the point.
    """

    store = get_data_store()
    row = _find_parcel_at(store.parcels, lng, lat)
    if row is None:
        raise HTTPException(status_code=404, detail="No parcel at that location")
    return _row_to_parcel(row)


@router.get("/{parcel_id}", response_model=Parcel)
def get_parcel(parcel_id: str) -> Parcel:
    """Fetch a parcel by its ``parcel_id`` (e.g. ``NE-000001``).

    Returns 404 if no parcel matches.
    """

    store = get_data_store()
    row = _find_by_id(store.parcels, parcel_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Parcel {parcel_id!r} not found")
    return _row_to_parcel(row)
