"""HM Land Registry INSPIRE Index Polygons ETL for the 12 NE England LADs.

The user manually downloads the per-LA zips from the HM Land Registry INSPIRE
download portal into ``~/Downloads/``. Each zip contains a single
``Land_Registry_Cadastral_Parcels.gml`` plus a licence PDF.

This module:

* Validates all 12 expected zips are present.
* Extracts each into ``data/raw/inspire/<lad24cd_lower>/`` (idempotent — skips
  if the GML already exists at the target path).
* Streams each LA's parcels through a ≥2 ha area filter while still in
  EPSG:27700 (so the area calculation is in m²), then reprojects survivors to
  EPSG:4326. This keeps peak memory bounded — only the filtered survivors of
  earlier LAs stay resident while the next LA is loaded.
* Tags each parcel with ``lad_code`` / ``lad_name``, trims to a clean column
  set, concatenates everything into a single GeoDataFrame, and assigns a
  stable ``parcel_id`` of the form ``NE-{i:06d}``.
* Writes ``data/processed/parcels.geojson`` (canonical merged file, used by
  Wave 5C as the parcel attribute attachment target) and a sidecar
  ``parcels.manifest.json``.

The canonical entry point is :func:`merge_inspire_parcels`. CLI:

    uv run python -m etl.sources.inspire

Note that ``parcels.geojson`` itself is large (>100 MB) and therefore not
committed to git — only the source code and the sidecar manifest are.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

import geopandas as gpd

from etl.config import DATA_PROCESSED, DATA_RAW, NE_LAD_NAMES, TARGET_CRS

logger = logging.getLogger(__name__)

# Mapping LAD24CD -> zip filename in ~/Downloads. Order mirrors NE_LAD_CODES.
LAD_ZIP_NAMES: dict[str, str] = {
    "E06000047": "Durham_County_Council.zip",
    "E06000005": "Darlington_Borough_Council.zip",
    "E06000001": "Hartlepool_Borough_Council.zip",
    "E06000002": "Middlesbrough_Borough_Council.zip",
    "E06000004": "Stockton-on-Tees_Borough_Council.zip",
    "E06000003": "Redcar_and_Cleveland_Borough_Council.zip",
    "E06000057": "Northumberland_County_Council.zip",
    "E08000037": "Gateshead_Metropolitan_Borough_Council.zip",
    "E08000021": "Newcastle_City_Council.zip",
    "E08000022": "North_Tyneside_Council.zip",
    "E08000023": "South_Tyneside_Council.zip",
    # Note the " (1)" suffix from a re-download — the zipfile in Downloads
    # was renamed by the browser when the user re-fetched it.
    "E08000024": "Sunderland_City_Council (1).zip",
}

DOWNLOADS_DIR: Path = Path.home() / "Downloads"
INSPIRE_RAW_DIR: Path = DATA_RAW / "inspire"
GML_NAME: str = "Land_Registry_Cadastral_Parcels.gml"

OUTPUT_NAME: str = "parcels.geojson"
MANIFEST_NAME: str = "parcels.manifest.json"

SOURCE_CRS: str = "EPSG:27700"  # British National Grid — INSPIRE supplies this.
AREA_THRESHOLD_HA: float = 2.0
LICENSE: str = "OGL 3.0"
SOURCE_NAME: str = "HM Land Registry INSPIRE Index Polygons"
LAST_UPDATED: str = "2026-Q1 per LA download dates"

# Columns we want to keep on each parcel, when the GML supplies them. Anything
# else (GML XML residue like ``gml_id``, ``fid``, etc.) is dropped.
INSPIRE_ATTR_COLS: tuple[str, ...] = (
    "INSPIREID",
    "LABEL",
    "NATIONALCADASTRALREFERENCE",
    "VALIDFROM",
)


def _concat_gdfs(gdfs: list[gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    """Concatenate GeoDataFrames into a single GeoDataFrame in TARGET_CRS.

    Uses pandas concat (vectorised) — orders of magnitude faster than the
    previous per-row dict iteration when there are 50K+ parcels.
    """
    if not gdfs:
        return gpd.GeoDataFrame(geometry=[], crs=TARGET_CRS)
    merged = gpd.pd.concat(gdfs, ignore_index=True)
    return gpd.GeoDataFrame(merged, geometry="geometry", crs=TARGET_CRS)


def _validate_inputs() -> dict[str, Path]:
    """Resolve each LAD code to its zip path. Fail fast if any are missing."""
    if not DOWNLOADS_DIR.is_dir():
        raise FileNotFoundError(
            f"Downloads dir not found: {DOWNLOADS_DIR}. Place the 12 NE LAD INSPIRE zips there."
        )

    resolved: dict[str, Path] = {}
    missing: list[str] = []
    for lad_code, zip_name in LAD_ZIP_NAMES.items():
        zip_path = DOWNLOADS_DIR / zip_name
        if zip_path.is_file():
            resolved[lad_code] = zip_path
        else:
            missing.append(f"{lad_code} ({NE_LAD_NAMES[lad_code]}) -> {zip_path}")

    if missing:
        raise FileNotFoundError(
            "Missing INSPIRE zips for the following LADs:\n  - " + "\n  - ".join(missing)
        )
    return resolved


def _extract_zip(lad_code: str, zip_path: Path) -> Path:
    """Extract ``Land_Registry_Cadastral_Parcels.gml`` for one LA. Idempotent.

    Returns the path to the extracted GML.
    """
    target_dir = INSPIRE_RAW_DIR / lad_code.lower()
    target_dir.mkdir(parents=True, exist_ok=True)
    gml_path = target_dir / GML_NAME

    if gml_path.is_file() and gml_path.stat().st_size > 0:
        logger.info("[%s] GML already extracted at %s — skipping", lad_code, gml_path)
        return gml_path

    logger.info("[%s] Extracting %s -> %s", lad_code, zip_path.name, target_dir)
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.namelist()
        if GML_NAME not in members:
            raise FileNotFoundError(f"{zip_path} missing expected member {GML_NAME}; got {members}")
        zf.extract(GML_NAME, target_dir)

    if not gml_path.is_file():
        raise FileNotFoundError(f"Extraction did not produce {gml_path}; check zip layout")
    return gml_path


def _process_lad(lad_code: str, gml_path: Path) -> tuple[gpd.GeoDataFrame, dict]:
    """Read one LA's GML, filter to ≥2 ha, project to 4326, attribute it.

    Returns the trimmed GeoDataFrame and a stats dict for the manifest
    breakdown.
    """
    lad_name = NE_LAD_NAMES[lad_code]
    logger.info("[%s] Reading %s (BNG)", lad_code, gml_path)
    gdf = gpd.read_file(gml_path)
    raw_count = len(gdf)
    logger.info("[%s] %d raw parcels (CRS=%s)", lad_code, raw_count, gdf.crs)

    # Force the CRS — INSPIRE ships these in BNG but the GML header sometimes
    # describes it ambiguously. Don't reproject; just assert the source frame.
    if gdf.crs is None:
        logger.warning("[%s] GML has no CRS — assuming %s", lad_code, SOURCE_CRS)
        gdf = gdf.set_crs(SOURCE_CRS)
    elif str(gdf.crs).upper() not in {SOURCE_CRS, "EPSG:27700"}:
        logger.warning(
            "[%s] Unexpected source CRS %s — reprojecting to %s for area calc",
            lad_code,
            gdf.crs,
            SOURCE_CRS,
        )
        gdf = gdf.to_crs(SOURCE_CRS)

    # Area in hectares while still in metres-CRS.
    gdf["area_ha"] = gdf.geometry.area / 10_000.0

    kept_mask = gdf["area_ha"] >= AREA_THRESHOLD_HA
    kept = gdf.loc[kept_mask].copy()
    dropped = raw_count - len(kept)
    logger.info(
        "[%s] kept %d / %d parcels at >=%.1f ha (dropped %d)",
        lad_code,
        len(kept),
        raw_count,
        AREA_THRESHOLD_HA,
        dropped,
    )

    # Free the unfiltered frame ASAP.
    del gdf

    # Reproject survivors to WGS84 for the canonical output.
    kept = kept.to_crs(TARGET_CRS)

    # Tag with LA identity.
    kept["lad_code"] = lad_code
    kept["lad_name"] = lad_name

    # Trim to a clean column set: keep INSPIRE attrs that are actually present.
    keep_cols = ["geometry", "area_ha", "lad_code", "lad_name"]
    for col in INSPIRE_ATTR_COLS:
        if col in kept.columns:
            keep_cols.append(col)
    # Note: GML readers sometimes lower-case attribute names. Catch those too.
    for col in INSPIRE_ATTR_COLS:
        lc = col.lower()
        if lc in kept.columns and lc not in keep_cols:
            keep_cols.append(lc)

    trimmed = kept[keep_cols].copy()
    del kept

    total_area_ha = float(trimmed["area_ha"].sum())
    stats = {
        "raw_count": raw_count,
        "kept_count": len(trimmed),
        "dropped_count": dropped,
        "total_area_ha": round(total_area_ha, 2),
    }
    return trimmed, stats


def _assign_parcel_ids(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Assign a stable ``parcel_id`` of the form ``NE-000001``."""
    gdf = gdf.reset_index(drop=True)
    gdf["parcel_id"] = [f"NE-{i:06d}" for i in range(1, len(gdf) + 1)]
    # Reorder so parcel_id is first non-geometry column (nicer for inspection).
    cols = ["parcel_id"] + [c for c in gdf.columns if c not in {"parcel_id", "geometry"}]
    return gpd.GeoDataFrame(gdf[cols + ["geometry"]], geometry="geometry", crs=gdf.crs)


def _write_manifest(
    manifest_path: Path,
    *,
    feature_count: int,
    total_area_ha: float,
    per_lad: dict[str, dict],
    file_size_bytes: int,
    parcels_dropped: int,
) -> None:
    payload = {
        "name": "INSPIRE Index Polygons (≥2 ha) — 12 NE England LADs",
        "source": SOURCE_NAME,
        "license": LICENSE,
        "last_updated": LAST_UPDATED,
        "feature_count": feature_count,
        "total_area_ha": round(total_area_ha, 2),
        "per_lad_breakdown": per_lad,
        "filter_threshold_ha": AREA_THRESHOLD_HA,
        "file_size_bytes": file_size_bytes,
        "parcels_dropped_below_threshold": parcels_dropped,
        "geometry_type": "Polygon",
        "primary_key": "parcel_id",
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as fh:
        json.dump(payload, fh, indent=2)
    logger.info("Wrote manifest %s", manifest_path)


def merge_inspire_parcels() -> Path:
    """Extract, filter, merge the 12 NE LAD INSPIRE GMLs into one GeoJSON.

    Returns the path to the canonical ``data/processed/parcels.geojson``.
    """
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)
    INSPIRE_RAW_DIR.mkdir(parents=True, exist_ok=True)

    zip_paths = _validate_inputs()
    logger.info("All %d INSPIRE zips located in %s", len(zip_paths), DOWNLOADS_DIR)

    survivors: list[gpd.GeoDataFrame] = []
    per_lad: dict[str, dict] = {}
    total_dropped = 0

    for lad_code, zip_path in zip_paths.items():
        gml_path = _extract_zip(lad_code, zip_path)
        trimmed, stats = _process_lad(lad_code, gml_path)
        survivors.append(trimmed)
        per_lad[lad_code] = {"lad_name": NE_LAD_NAMES[lad_code], **stats}
        total_dropped += stats["dropped_count"]

    logger.info("Concatenating %d filtered LA GeoDataFrames", len(survivors))
    merged_gdf = _concat_gdfs(survivors)
    del survivors

    merged_gdf = _assign_parcel_ids(merged_gdf)
    logger.info(
        "Merged total: %d parcels covering %.1f ha across %d LADs",
        len(merged_gdf),
        merged_gdf["area_ha"].sum(),
        merged_gdf["lad_code"].nunique(),
    )

    out_path = DATA_PROCESSED / OUTPUT_NAME
    if out_path.exists():
        out_path.unlink()
    logger.info("Writing canonical GeoJSON -> %s", out_path)
    merged_gdf.to_file(out_path, driver="GeoJSON", engine="pyogrio")
    file_size = out_path.stat().st_size
    logger.info("Wrote %s (%d bytes)", out_path, file_size)

    _write_manifest(
        DATA_PROCESSED / MANIFEST_NAME,
        feature_count=len(merged_gdf),
        total_area_ha=float(merged_gdf["area_ha"].sum()),
        per_lad=per_lad,
        file_size_bytes=file_size,
        parcels_dropped=total_dropped,
    )

    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    merge_inspire_parcels()
