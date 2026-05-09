"""HM Land Registry CCOD ownership lookup endpoints.

The CCOD dataset only covers UK-registered companies — individual
private owners (which account for ~70% of agricultural land) are NOT
in the data.

Endpoints:

- ``GET /api/ownership/by_title/{title_number}`` — exact title lookup.
- ``GET /api/ownership/by_postcode/{postcode}`` — all records at a postcode.
- ``GET /api/ownership/by_proprietor?q=...`` — case-insensitive substring
  search on ``proprietor_name_1``.
- ``GET /api/ownership/nearest?lng=&lat=&radius_m=`` — radius search around
  a point (jittered postcode centroid, so coordinates are approximate).
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd  # pyright: ignore[reportMissingImports]
import pandas as pd  # pyright: ignore[reportMissingImports]
from fastapi import APIRouter, HTTPException, Query  # pyright: ignore[reportMissingImports]
from pydantic import BaseModel  # pyright: ignore[reportMissingImports]

from backend.services.data_store import get_data_store


def _s(v: Any) -> str | None:
    """NaN-safe string coerce — converts pandas NaN/None/empty to None."""

    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none"):
        return None
    return s


router = APIRouter(prefix="/api/ownership", tags=["ownership"])


class OwnershipRecord(BaseModel):
    """Single CCOD title record. Geometry collapsed to lon/lat."""

    title_number: str | None
    tenure: str | None
    property_address: str | None
    district: str | None
    county: str | None
    postcode: str | None
    proprietor_name_1: str | None
    company_registration_no_1: str | None
    proprietorship_category_1: str | None
    proprietor_address_1: str | None
    additional_proprietors: str | None
    date_proprietor_added: str | None
    lon: float
    lat: float


def _row_to_ownership(row) -> OwnershipRecord:
    return OwnershipRecord(
        title_number=_s(row.get("title_number")),
        tenure=_s(row.get("tenure")),
        property_address=_s(row.get("property_address")),
        district=_s(row.get("district")),
        county=_s(row.get("county")),
        postcode=_s(row.get("postcode")),
        proprietor_name_1=_s(row.get("proprietor_name_1")),
        company_registration_no_1=_s(row.get("company_registration_no_1")),
        proprietorship_category_1=_s(row.get("proprietorship_category_1")),
        proprietor_address_1=_s(row.get("proprietor_address_1")),
        additional_proprietors=_s(row.get("additional_proprietors")),
        date_proprietor_added=_s(row.get("date_proprietor_added")),
        lon=float(row.geometry.x),
        lat=float(row.geometry.y),
    )


def _by_title(df, title_number: str) -> list[OwnershipRecord]:
    rows = df[df["title_number"] == title_number]
    return [_row_to_ownership(r) for _, r in rows.iterrows()]


def _by_postcode(df, postcode: str, limit: int = 100) -> list[OwnershipRecord]:
    pc = postcode.strip().upper().replace(" ", "")
    df_pc = df["postcode"].fillna("").str.upper().str.replace(" ", "", regex=False)
    rows = df[df_pc == pc].head(limit)
    return [_row_to_ownership(r) for _, r in rows.iterrows()]


def _by_proprietor(df, q: str, limit: int = 100) -> list[OwnershipRecord]:
    needle = q.strip().lower()
    if not needle:
        return []
    mask = df["proprietor_name_1"].fillna("").str.lower().str.contains(needle)
    rows = df.loc[mask].head(limit)
    return [_row_to_ownership(r) for _, r in rows.iterrows()]


def _nearest(
    df, lng: float, lat: float, radius_m: int = 200, limit: int = 50
) -> list[OwnershipRecord]:
    bng = df.to_crs("EPSG:27700")
    target = gpd.GeoSeries.from_xy([lng], [lat], crs="EPSG:4326").to_crs("EPSG:27700").iloc[0]
    dists = bng.geometry.distance(target)
    rows = df.loc[dists <= radius_m].assign(_dist=dists).sort_values("_dist").head(limit)
    return [_row_to_ownership(r) for _, r in rows.iterrows()]


@router.get("/by_title/{title_number}", response_model=list[OwnershipRecord])
def by_title(title_number: str) -> list[OwnershipRecord]:
    rows = _by_title(get_data_store().ccod, title_number)
    if not rows:
        raise HTTPException(404, f"title not found: {title_number}")
    return rows


@router.get("/by_postcode/{postcode}", response_model=list[OwnershipRecord])
def by_postcode(postcode: str, limit: int = Query(100, ge=1, le=500)) -> list[OwnershipRecord]:
    return _by_postcode(get_data_store().ccod, postcode, limit)


@router.get("/by_proprietor", response_model=dict)
def by_proprietor(q: str, limit: int = Query(50, ge=1, le=500)) -> dict:
    rows = _by_proprietor(get_data_store().ccod, q, limit)
    return {"count": len(rows), "results": rows}


@router.get("/nearest", response_model=dict)
def nearest(
    lng: float,
    lat: float,
    radius_m: int = Query(200, ge=10, le=5000),
    limit: int = Query(50, ge=1, le=500),
) -> dict:
    rows = _nearest(get_data_store().ccod, lng, lat, radius_m, limit)
    return {"count": len(rows), "results": rows}
