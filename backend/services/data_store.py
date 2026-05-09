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


# Logical layer name -> filename stem on disk. Most layers share the same
# name as their stem; ``parcels`` is the exception because the file is
# ``parcels_attributed`` while the in-memory layer is just ``parcels``.
_LAYER_STEMS: dict[str, str] = {
    "parcels": "parcels_attributed",
    "npg_headroom": "npg_headroom",
    "npg_substations_points": "npg_substations_points",
    "repd": "repd",
    "ccod_ne": "ccod_ne",
}


def _read_layer(name: str, root: Path) -> gpd.GeoDataFrame:
    """Read a processed layer, preferring ``.gpkg`` over ``.geojson``.

    GeoPackage is ~5-10x faster to read and ~30% the on-disk size of the
    equivalent GeoJSON, so we use it when available. The Dockerfile
    converts the big GeoJSONs at build time; local dev still works
    because we fall back to the original ``.geojson``.
    """

    stem = _LAYER_STEMS.get(name, name)
    processed = root / "processed"
    gpkg = processed / f"{stem}.gpkg"
    if gpkg.exists():
        return gpd.read_file(gpkg, engine="pyogrio")
    return gpd.read_file(processed / f"{stem}.geojson", engine="pyogrio")


@lru_cache(maxsize=1)
def get_data_store() -> DataStore:
    """Load all datasets once and return a frozen :class:`DataStore`.

    Uses ``functools.lru_cache`` so the first FastAPI request (or app
    startup) primes the cache and subsequent calls are O(1).
    """

    root = _data_root()
    raw = root / "raw"

    parcels = _read_layer("parcels", root)
    substation_catchments = _read_layer("npg_headroom", root)
    substation_points = _read_layer("npg_substations_points", root)
    repd = _read_layer("repd", root)
    ccod = _read_layer("ccod_ne", root)

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
