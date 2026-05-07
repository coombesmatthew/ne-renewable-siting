"""DESNZ Renewable Energy Planning Database (REPD) Q1 2026 ETL.

Downloads the public REPD Q1 2026 CSV, filters to North East England (by
``Region == 'North East'`` plus a NE-polygon spatial filter), reprojects the
British National Grid (EPSG:27700) easting/northing coordinates to WGS84
(EPSG:4326), trims to a useful column subset, and writes both a GeoJSON
feature collection and a sidecar manifest JSON.

Notes / quirks discovered:
    * The published CSV is **cp1252** encoded, not utf-8 (contains non-breaking
      spaces 0xA0).
    * REPD has no ``Local Authority`` column. The closest fields are ``Region``
      (clean, e.g. ``North East``) and ``County`` (messy: leading spaces,
      historical groupings like ``Tyne And Wear`` / ``Cleveland`` not
      matching modern LADs). We therefore filter by ``Region`` and accept the
      twelve NE LAD names as a fallback if a row is missing ``Region`` but has
      a recognisable ``County``.
    * ``X-coordinate`` / ``Y-coordinate`` are stored as strings, often with a
      leading tab character. They are British National Grid (OSGB36, EPSG:27700)
      easting/northing, not lat/lon.
    * Some rows have wildly wrong coordinates (e.g. Salters Lane Solar Panels
      with a Y-coordinate of 188837 — south coast of England). These are
      dropped by the BNG range guard plus the final NE polygon spatial filter.
    * REPD coordinates are sometimes the planning office, not the actual site
      — disclose in methodology.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point

from etl.config import (
    DATA_PROCESSED,
    DATA_RAW,
    NE_LAD_NAMES,
    NE_POLYGON_PATH,
    TARGET_CRS,
)

logger = logging.getLogger(__name__)

REPD_URL = (
    "https://assets.publishing.service.gov.uk/media/"
    "69fc56908cc72d2f863ea58d/REPD_publication_Q1_2026.csv"
)
RAW_NAME = "repd_q1_2026.csv"
OUT_NAME = "repd.geojson"
MANIFEST_NAME = "repd.manifest.json"

# Loose BNG sanity range for NE England. Final spatial filter via NE polygon
# does the precise check; this just catches obviously wrong coordinates
# (e.g. a row in Cornwall whose row says Region=North East).
BNG_X_MIN, BNG_X_MAX = 350_000, 500_000
BNG_Y_MIN, BNG_Y_MAX = 450_000, 660_000

KEEP_COLS = [
    "Ref ID",
    "Site Name",
    "Operator (or Applicant)",
    "Technology Type",
    "Installed Capacity (MWelec)",
    "Development Status",
    "County",
    "Region",
    "Country",
    "Planning Application Reference",
    "X-coordinate",
    "Y-coordinate",
]


def _download_repd(cache_path: Path) -> Path:
    """Download the REPD Q1 2026 CSV, caching to ``cache_path``."""
    if cache_path.exists():
        logger.info("Using cached REPD CSV: %s", cache_path)
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading REPD CSV from %s", REPD_URL)
    resp = requests.get(REPD_URL, timeout=180)
    resp.raise_for_status()
    cache_path.write_bytes(resp.content)
    logger.info("Cached %d bytes to %s", len(resp.content), cache_path)
    return cache_path


def _coerce_bng(series: pd.Series) -> pd.Series:
    """REPD stores X/Y as strings, sometimes with a leading tab. Clean + numeric."""
    return pd.to_numeric(series.astype(str).str.strip(), errors="coerce")


def _passes_primary_filter(df: pd.DataFrame) -> pd.Series:
    """Boolean mask: row is in North East by Region or by County→LAD name match."""
    region = df["Region"].fillna("").astype(str).str.strip().str.casefold()
    county = df["County"].fillna("").astype(str).str.strip().str.casefold()
    ne_names = {n.casefold() for n in NE_LAD_NAMES.values()}
    return (region == "north east") | county.isin(ne_names)


def download_repd() -> Path:
    """Run the full REPD ETL and return the path to the processed GeoJSON."""
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    cache_path = DATA_RAW / RAW_NAME
    _download_repd(cache_path)

    # CSV is cp1252 encoded (contains 0xA0 NBSPs). low_memory=False to silence
    # mixed-dtype warnings on the big sparse columns.
    logger.info("Reading REPD CSV (cp1252)…")
    raw = pd.read_csv(cache_path, encoding="cp1252", low_memory=False)
    raw_row_count = len(raw)
    logger.info("Loaded %d raw REPD rows, %d columns", raw_row_count, len(raw.columns))

    # 1. Primary filter: Region == North East, or County name in our NE LADs.
    primary_mask = _passes_primary_filter(raw)
    primary = raw[primary_mask].copy()
    logger.info("Primary filter (Region/County): %d rows", len(primary))

    # 2. Coerce coordinates to numeric and drop rows with missing/invalid BNG.
    primary["_x"] = _coerce_bng(primary["X-coordinate"])
    primary["_y"] = _coerce_bng(primary["Y-coordinate"])

    coord_valid = (
        primary["_x"].notna()
        & primary["_y"].notna()
        & primary["_x"].between(BNG_X_MIN, BNG_X_MAX)
        & primary["_y"].between(BNG_Y_MIN, BNG_Y_MAX)
    )
    dropped_bad_coords = (~coord_valid).sum()
    if dropped_bad_coords:
        logger.info(
            "Dropping %d rows with missing or out-of-range BNG coordinates",
            dropped_bad_coords,
        )
        for _, row in primary[~coord_valid].iterrows():
            logger.debug(
                "  drop coords: %r x=%r y=%r",
                row.get("Site Name"),
                row.get("X-coordinate"),
                row.get("Y-coordinate"),
            )
    primary = primary[coord_valid].copy()

    # 3. Build geometry in BNG, reproject to WGS84.
    geom = [Point(x, y) for x, y in zip(primary["_x"], primary["_y"], strict=True)]
    gdf = gpd.GeoDataFrame(primary, geometry=geom, crs="EPSG:27700")
    logger.info("Built %d points in EPSG:27700; reprojecting to %s", len(gdf), TARGET_CRS)
    gdf = gdf.to_crs(TARGET_CRS)

    # 4. Final spatial filter against the NE polygon clip mask.
    ne_polygon = gpd.read_file(NE_POLYGON_PATH)
    if str(ne_polygon.crs).upper() != TARGET_CRS:
        ne_polygon = ne_polygon.to_crs(TARGET_CRS)
    ne_geom = ne_polygon.geometry.union_all()
    in_polygon = gdf.geometry.within(ne_geom)
    dropped_outside_polygon = (~in_polygon).sum()
    if dropped_outside_polygon:
        logger.info(
            "Dropping %d rows that fall outside the NE polygon",
            dropped_outside_polygon,
        )
    gdf = gdf[in_polygon].copy()

    # 5. Trim to useful columns. Drop blank-named or all-NaN columns first.
    blank_named = [c for c in gdf.columns if isinstance(c, str) and not c.strip()]
    if blank_named:
        logger.info("Dropping %d blank-named columns", len(blank_named))
        gdf = gdf.drop(columns=blank_named)

    keep = [c for c in KEEP_COLS if c in gdf.columns] + ["geometry"]
    gdf = gdf[keep].copy()

    # Drop columns that are 100% NaN within the surviving rows.
    all_nan = [c for c in gdf.columns if c != "geometry" and gdf[c].isna().all()]
    if all_nan:
        logger.info("Dropping %d all-NaN columns: %s", len(all_nan), all_nan)
        gdf = gdf.drop(columns=all_nan)

    feature_count = len(gdf)
    logger.info("Final REPD feature count: %d", feature_count)

    # 6. Write GeoJSON.
    out_path = DATA_PROCESSED / OUT_NAME
    if out_path.exists():
        out_path.unlink()
    gdf.to_file(out_path, driver="GeoJSON")
    logger.info("Wrote %s", out_path)

    # 7. Manifest.
    tech_breakdown = (
        gdf["Technology Type"].fillna("(unknown)").value_counts().to_dict()
        if "Technology Type" in gdf.columns
        else {}
    )
    status_breakdown = (
        gdf["Development Status"].fillna("(unknown)").value_counts().to_dict()
        if "Development Status" in gdf.columns
        else {}
    )
    dropped_total = int(
        primary_mask.sum() - feature_count
    )  # rows that passed primary then got cleaned out
    file_size = out_path.stat().st_size

    manifest = {
        "name": "REPD Q1 2026",
        "source_url": REPD_URL,
        "license": "OGL 3.0",
        "last_updated": "2026-Q1",
        "feature_count": feature_count,
        "raw_row_count": raw_row_count,
        "file_size_bytes": file_size,
        "tech_breakdown": tech_breakdown,
        "status_breakdown": status_breakdown,
        "dropped_rows": dropped_total,
        "notes": (
            "REPD coords are sometimes the planning office, not the actual "
            "site — disclose in methodology."
        ),
    }
    manifest_path = DATA_PROCESSED / MANIFEST_NAME
    with manifest_path.open("w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("Wrote manifest %s", manifest_path)

    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    download_repd()
