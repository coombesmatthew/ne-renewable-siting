"""Build the NE England bounding polygon from ONS LAD Dec 2024 boundaries.

Produces:
    - data/ne_england.geojson         single dissolved feature (clip mask)
    - data/processed/la_boundaries.geojson  per-LAD boundaries (12 features)

Both are written in EPSG:4326. The raw ONS download is cached at
data/raw/ons_lad_dec2024_bgc.geojson so subsequent runs skip the network call.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import geopandas as gpd
import requests
from shapely.geometry import mapping
from shapely.ops import unary_union

from etl.config import (
    DATA_PROCESSED,
    DATA_RAW,
    NE_LAD_CODES,
    NE_POLYGON_PATH,
    TARGET_CRS,
)

logger = logging.getLogger(__name__)

ONS_LAD_FEATURESERVER = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Local_Authority_Districts_December_2024_Boundaries_UK_BGC/FeatureServer/0/query"
)
RAW_CACHE_NAME = "ons_lad_dec2024_bgc.geojson"
LA_BOUNDARIES_NAME = "la_boundaries.geojson"


def _download_ons_lads(cache_path: Path) -> Path:
    """Fetch all UK LADs from the ONS feature server, caching to ``cache_path``."""
    if cache_path.exists():
        logger.info("Using cached ONS LAD download: %s", cache_path)
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    page_size = 2000
    offset = 0
    all_features: list[dict] = []
    crs_block: dict | None = None

    while True:
        params = {
            "where": "1=1",
            "outFields": "*",
            "outSR": "4326",
            "f": "geojson",
            "resultOffset": str(offset),
            "resultRecordCount": str(page_size),
        }
        logger.info("Fetching ONS LADs (offset=%d)…", offset)
        resp = requests.get(ONS_LAD_FEATURESERVER, params=params, timeout=120)
        resp.raise_for_status()
        page = resp.json()
        feats = page.get("features", [])
        if crs_block is None and "crs" in page:
            crs_block = page["crs"]
        all_features.extend(feats)
        # ArcGIS sets exceededTransferLimit when more pages remain.
        if not page.get("exceededTransferLimit") and len(feats) < page_size:
            break
        offset += len(feats)
        if not feats:
            break

    fc = {
        "type": "FeatureCollection",
        "features": all_features,
    }
    if crs_block is not None:
        fc["crs"] = crs_block

    with cache_path.open("w") as fh:
        json.dump(fc, fh)
    logger.info("Cached %d LAD features to %s", len(all_features), cache_path)
    return cache_path


def build_ne_polygon(force: bool = False) -> Path:
    """Build the NE England bounding polygon from ONS LAD Dec 2024 boundaries.

    Returns the path to the dissolved single-feature GeoJSON.
    """
    if NE_POLYGON_PATH.exists() and not force:
        logger.info("NE polygon already exists at %s — skipping rebuild", NE_POLYGON_PATH)
        return NE_POLYGON_PATH

    DATA_RAW.mkdir(parents=True, exist_ok=True)
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    NE_POLYGON_PATH.parent.mkdir(parents=True, exist_ok=True)

    cache_path = DATA_RAW / RAW_CACHE_NAME
    _download_ons_lads(cache_path)

    logger.info("Reading ONS LAD layer with geopandas…")
    gdf = gpd.read_file(cache_path)
    logger.info("Loaded %d UK LAD features (CRS=%s)", len(gdf), gdf.crs)

    code_field = "LAD24CD"
    name_field = "LAD24NM"
    if code_field not in gdf.columns:
        raise RuntimeError(
            f"Expected field '{code_field}' not found in ONS layer. Columns: {list(gdf.columns)}"
        )

    ne = gdf[gdf[code_field].isin(NE_LAD_CODES)].copy()
    found_codes = set(ne[code_field].tolist())
    expected = set(NE_LAD_CODES)
    missing = expected - found_codes
    if missing:
        raise RuntimeError(f"Missing NE LAD codes from ONS layer: {sorted(missing)}")
    if len(ne) != len(NE_LAD_CODES):
        raise RuntimeError(
            f"Expected {len(NE_LAD_CODES)} NE LADs, got {len(ne)}: {sorted(found_codes)}"
        )

    if str(ne.crs).upper() != TARGET_CRS:
        logger.info("Reprojecting from %s to %s", ne.crs, TARGET_CRS)
        ne = ne.to_crs(TARGET_CRS)

    # Per-LAD output (frontend filter UI / REPD clip).
    la_out_path = DATA_PROCESSED / LA_BOUNDARIES_NAME
    la_out = ne[[code_field, name_field, "geometry"]].copy()
    if la_out_path.exists():
        la_out_path.unlink()
    la_out.to_file(la_out_path, driver="GeoJSON")
    logger.info("Wrote %d per-LAD features to %s", len(la_out), la_out_path)

    # Dissolve to a single geometry.
    dissolved_geom = unary_union(ne.geometry.values)

    feature = {
        "type": "Feature",
        "properties": {
            "region": "North East England",
            "lad_count": 12,
            "source": "ONS LAD Dec 2024 BGC",
            "license": "OGL 3.0",
            "last_updated": "2024-12",
        },
        "geometry": mapping(dissolved_geom),
    }
    fc = {"type": "FeatureCollection", "features": [feature]}
    with NE_POLYGON_PATH.open("w") as fh:
        json.dump(fc, fh)
    logger.info(
        "Wrote dissolved NE polygon (geom=%s) to %s",
        feature["geometry"]["type"],
        NE_POLYGON_PATH,
    )

    # Sanity check: log area in km² (British National Grid).
    diss_gdf = gpd.GeoDataFrame(geometry=[dissolved_geom], crs=TARGET_CRS)
    area_km2 = diss_gdf.to_crs("EPSG:27700").geometry.area.iloc[0] / 1_000_000
    logger.info("Dissolved area: %.1f km² (expected ~8,592 km²)", area_km2)

    return NE_POLYGON_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build_ne_polygon()
