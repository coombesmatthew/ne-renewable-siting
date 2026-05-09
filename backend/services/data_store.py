"""Singleton data store loaded once at app startup.

All datasets are read into memory using pyogrio for speed. Total memory
budget is ~70MB (parcels dominate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import geopandas as gpd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataStore:
    """Container for all in-memory datasets used by the API."""

    parcels: gpd.GeoDataFrame
    """33,969 parcels with attributes (parcel_id, area_ha, lad_code/name,
    centroid_lon/lat, mean_pvout_kwhkwp, mean_wind_speed_100m_ms,
    dist_substation_*_headroom_m, nearest_substation_name, intersects_*).
    """

    substation_catchments: gpd.GeoDataFrame
    """683 substation catchment polygons with full headroom attributes."""

    substation_points: gpd.GeoDataFrame
    """681 substation point locations with the same headroom attributes."""

    repd: gpd.GeoDataFrame
    """572 Renewable Energy Planning Database records (NE only)."""

    ccod: gpd.GeoDataFrame
    """120,398 HM Land Registry Commercial and Corporate Ownership records
    (NE England subset). Excludes individual private owners. Geometry is a
    jittered postcode centroid (Point)."""

    solar_tif_path: Path
    wind_tif_path: Path


def _data_root() -> Path:
    """Project ``data/`` directory (../../../data relative to this file)."""

    return Path(__file__).resolve().parent.parent.parent / "data"


@lru_cache(maxsize=1)
def get_data_store() -> DataStore:
    """Load all datasets once and return a frozen :class:`DataStore`.

    Uses ``functools.lru_cache`` so the first FastAPI request (or app
    startup) primes the cache and subsequent calls are O(1).
    """

    root = _data_root()
    processed = root / "processed"
    raw = root / "raw"

    parcels = gpd.read_file(processed / "parcels_attributed.geojson", engine="pyogrio")
    substation_catchments = gpd.read_file(processed / "npg_headroom.geojson", engine="pyogrio")
    substation_points = gpd.read_file(
        processed / "npg_substations_points.geojson", engine="pyogrio"
    )
    repd = gpd.read_file(processed / "repd.geojson", engine="pyogrio")
    ccod = gpd.read_file(processed / "ccod_ne.geojson", engine="pyogrio")

    # Build a spatial index on parcels up-front so /api/parcel/at is fast.
    _ = parcels.sindex

    logger.info(
        "[data_store] loaded parcels=%d substation_catchments=%d "
        "substation_points=%d repd=%d ccod=%d",
        len(parcels),
        len(substation_catchments),
        len(substation_points),
        len(repd),
        len(ccod),
    )

    return DataStore(
        parcels=parcels,
        substation_catchments=substation_catchments,
        substation_points=substation_points,
        repd=repd,
        ccod=ccod,
        solar_tif_path=raw / "solar_pvout.tif",
        wind_tif_path=raw / "wind_speed_100m.tif",
    )
