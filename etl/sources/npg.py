"""Northern Powergrid (NPg) ETL — substation headroom heatmap and ECR.

Two functions:

* ``download_headroom`` — fetches NPg's network heatmap (substation polygons
  with thermal headroom, fault level, demand/generation headroom etc.), clips
  to the NE England polygon, and writes a single GeoJSON + sidecar manifest.

* ``download_ecr`` — fetches the two-part Embedded Capacity Register
  (≥1 MW and <1 MW), tags each feature with its tier, concatenates, clips to
  the NE polygon, and writes a single GeoJSON + sidecar manifest.

Both use raw caches under ``DATA_RAW`` so re-runs skip the network call.
Outputs land in ``DATA_PROCESSED`` in EPSG:4326.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path

import geopandas as gpd
import requests

from etl.config import DATA_PROCESSED, DATA_RAW, NE_POLYGON_PATH, TARGET_CRS

logger = logging.getLogger(__name__)

HEATMAP_URL = (
    "https://northernpowergrid.opendatasoft.com/api/explore/v2.1/catalog/"
    "datasets/heatmapsubstationareas/exports/geojson"
)
ECR_GE1MW_URL = (
    "https://northernpowergrid.opendatasoft.com/api/explore/v2.1/catalog/"
    "datasets/embedded-capacity-register/exports/geojson"
)
ECR_LT1MW_URL = (
    "https://northernpowergrid.opendatasoft.com/api/explore/v2.1/catalog/"
    "datasets/embedded-capacity-register-part-2/exports/geojson"
)

HEATMAP_RAW_NAME = "npg_heatmap.geojson"
# Authoritative full-coverage manual download from NPg portal — covers BOTH
# NE and Yorkshire licence areas (683 substations). When present, this takes
# precedence over the API-fetched cache so the user's hand-curated source wins.
HEATMAP_FULL_RAW_NAME = "npg_heatmap_full.geojson"
ECR_GE1MW_RAW_NAME = "npg_ecr_ge1mw.geojson"
ECR_LT1MW_RAW_NAME = "npg_ecr_lt1mw.geojson"

HEATMAP_OUT_NAME = "npg_headroom.geojson"
HEATMAP_MANIFEST_NAME = "npg_headroom.manifest.json"
SUBSTATIONS_POINTS_OUT_NAME = "npg_substations_points.geojson"
SUBSTATIONS_POINTS_MANIFEST_NAME = "npg_substations_points.manifest.json"


def _voltage_tier(pvoltage: float | None) -> str | None:
    """Bucket pvoltage into one of 5 tier strings: 132/66/33/20/11.

    132 kV and above -> "132". 11 kV and below (incl 6, 5.75) -> "11".
    Anything outside the expected set returns None (caller can decide).
    """
    if pvoltage is None:
        return None
    try:
        v = float(pvoltage)
    except (TypeError, ValueError):
        return None
    if v >= 132:
        return "132"
    if v >= 66:
        return "66"
    if v >= 33:
        return "33"
    if v >= 20:
        return "20"
    return "11"  # 11 / 6 / 5.75 all collapse here


ECR_OUT_NAME = "npg_ecr.geojson"
ECR_MANIFEST_NAME = "npg_ecr.manifest.json"

LICENSE = "OGL 3.0"
DOWNLOAD_TIMEOUT_S = 600


def _download_to_cache(url: str, cache_path: Path) -> Path:
    """Download ``url`` to ``cache_path`` if not already present."""
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info("Using cached download: %s", cache_path)
        return cache_path

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Fetching %s -> %s", url, cache_path)
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_S) as resp:
        resp.raise_for_status()
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".part")
        with tmp_path.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    fh.write(chunk)
        tmp_path.replace(cache_path)
    logger.info("Cached %d bytes to %s", cache_path.stat().st_size, cache_path)
    return cache_path


def _load_ne_mask() -> gpd.GeoDataFrame:
    """Load the NE England clip polygon as a GeoDataFrame in TARGET_CRS."""
    mask = gpd.read_file(NE_POLYGON_PATH)
    if str(mask.crs).upper() != TARGET_CRS:
        mask = mask.to_crs(TARGET_CRS)
    return mask


def _ensure_target_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Reproject ``gdf`` to TARGET_CRS if needed; assume 4326 if CRS missing."""
    if gdf.crs is None:
        logger.warning("Source has no CRS — assuming %s", TARGET_CRS)
        gdf = gdf.set_crs(TARGET_CRS)
    elif str(gdf.crs).upper() != TARGET_CRS:
        logger.info("Reprojecting from %s to %s", gdf.crs, TARGET_CRS)
        gdf = gdf.to_crs(TARGET_CRS)
    return gdf


def _fix_invalid_geoms(gdf: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    """Repair invalid geometries via buffer(0). Returns (gdf, n_repaired)."""
    if gdf.empty:
        return gdf, 0
    invalid_mask = ~gdf.geometry.is_valid
    n_invalid = int(invalid_mask.sum())
    if n_invalid:
        logger.warning("Repairing %d invalid geometries via buffer(0)", n_invalid)
        gdf = gdf.copy()
        gdf.loc[invalid_mask, "geometry"] = gdf.loc[invalid_mask, "geometry"].buffer(0)
    return gdf, n_invalid


def _write_manifest(
    manifest_path: Path,
    *,
    name: str,
    source_url: str | list[str],
    feature_count: int,
    file_size_bytes: int,
    properties_keys: list[str],
    geometry_type: str,
    voltage_tier_counts: dict | None = None,
) -> None:
    payload: dict = {
        "name": name,
        "source_url": source_url,
        "license": LICENSE,
        "last_updated": _dt.date.today().isoformat(),
        "feature_count": feature_count,
        "file_size_bytes": file_size_bytes,
        "properties_keys": properties_keys,
        "geometry_type": geometry_type,
    }
    if voltage_tier_counts is not None:
        payload["voltage_tier_counts"] = voltage_tier_counts
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as fh:
        json.dump(payload, fh, indent=2)
    logger.info("Wrote manifest %s", manifest_path)


def _summarise_geom_type(gdf: gpd.GeoDataFrame) -> str:
    if gdf.empty:
        return "Unknown"
    types = sorted(set(gdf.geometry.geom_type))
    return types[0] if len(types) == 1 else "Mixed:" + ",".join(types)


def _json_safe_value(value: object) -> object:
    """Convert pandas/numpy/datetime scalars into JSON-serialisable values."""
    if value is None:
        return None
    # Catch NaN/NaT without importing numpy/pandas at module top level.
    try:
        if value != value:  # NaN
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (str, bool, int, float)):
        return value
    # Datetime-like (pandas Timestamp, datetime, date) → ISO string.
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:  # noqa: BLE001
            return str(value)
    # numpy scalars expose .item().
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:  # noqa: BLE001
            return str(value)
    return str(value)


def _write_geojson(gdf: gpd.GeoDataFrame, out_path: Path) -> None:
    """Write a GeoDataFrame to GeoJSON manually.

    Avoids pyogrio's strict type inference that fails on int columns with NaN
    after a multi-source concat, and on tz-aware datetime columns.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    prop_cols = [c for c in gdf.columns if c != "geometry"]
    features: list[dict] = []
    for _, row in gdf.iterrows():
        geom = row["geometry"]
        if geom is None or geom.is_empty:
            continue
        properties = {c: _json_safe_value(row[c]) for c in prop_cols}
        features.append(
            {
                "type": "Feature",
                "properties": properties,
                "geometry": geom.__geo_interface__,
            }
        )

    fc = {"type": "FeatureCollection", "features": features}
    with out_path.open("w") as fh:
        json.dump(fc, fh)


def download_headroom() -> Path:
    """Download NPg substation heatmap, clip to NE England, write processed output.

    Returns the path to the processed GeoJSON.
    """
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    # Prefer the user-supplied authoritative full-coverage file if present —
    # it covers both NE + Yorkshire licence areas (683 stations). Otherwise
    # fall back to the API-fetched single-region export.
    full_path = DATA_RAW / HEATMAP_FULL_RAW_NAME
    if full_path.is_file():
        logger.info("Using authoritative full-coverage NPg heatmap %s", full_path)
        cache_path = full_path
    else:
        cache_path = DATA_RAW / HEATMAP_RAW_NAME
        _download_to_cache(HEATMAP_URL, cache_path)
        logger.info("Reading NPg heatmap %s", cache_path)

    gdf = gpd.read_file(cache_path)
    raw_count = len(gdf)
    logger.info("Loaded %d raw heatmap features (CRS=%s)", raw_count, gdf.crs)

    gdf = _ensure_target_crs(gdf)
    gdf, _ = _fix_invalid_geoms(gdf)

    # Clip to NE polygon — the authoritative NPg file covers both NE + Yorkshire
    # licence areas (683 stations); the demo is NE-focused so we only keep the
    # ~171 stations whose catchment intersects the NE polygon.
    mask = _load_ne_mask()
    logger.info("Clipping to NE England polygon…")
    clipped = gpd.clip(gdf, mask)
    clipped = clipped[~clipped.geometry.is_empty & clipped.geometry.notna()].copy()
    clipped, _ = _fix_invalid_geoms(clipped)
    logger.info("Heatmap: %d -> %d features after NE clip", raw_count, len(clipped))

    # Add voltage_tier — used by the frontend to render 5 distinct layers.
    if "pvoltage" in clipped.columns:
        clipped["voltage_tier"] = clipped["pvoltage"].map(_voltage_tier)
    else:
        logger.warning("No pvoltage column on heatmap; voltage_tier will be empty")
        clipped["voltage_tier"] = None

    tier_counts = (
        clipped["voltage_tier"].value_counts().to_dict()
        if "voltage_tier" in clipped.columns
        else {}
    )
    logger.info("voltage_tier counts: %s", tier_counts)

    out_path = DATA_PROCESSED / HEATMAP_OUT_NAME
    _write_geojson(clipped, out_path)
    file_size = out_path.stat().st_size
    logger.info("Wrote %s (%d bytes)", out_path, file_size)

    properties_keys = [c for c in clipped.columns if c != "geometry"]
    _write_manifest(
        DATA_PROCESSED / HEATMAP_MANIFEST_NAME,
        name="NPg substation headroom heatmap",
        source_url=HEATMAP_URL,
        feature_count=len(clipped),
        file_size_bytes=file_size,
        properties_keys=properties_keys,
        geometry_type=_summarise_geom_type(clipped),
        voltage_tier_counts=tier_counts,
    )

    # Also emit a parallel point-geometry file at the actual station location.
    _emit_substation_points(clipped)

    return out_path


def _emit_substation_points(catchment_gdf: gpd.GeoDataFrame) -> Path:
    """Build a point GeoDataFrame from substation_location, write to disk.

    Each catchment polygon has a substation_location dict {lon, lat} in WGS84.
    Rows lacking substation_location are skipped with a warning — verified during
    plan exploration that all 185 NPg substations have it populated.
    """
    from shapely.geometry import Point

    points: list = []
    rows: list[dict] = []
    skipped = 0

    for _idx, row in catchment_gdf.iterrows():
        loc = row.get("substation_location")
        lon = lat = None
        # substation_location may already be a dict, or a JSON string, or None
        if isinstance(loc, dict):
            lon, lat = loc.get("lon"), loc.get("lat")
        elif isinstance(loc, str):
            try:
                parsed = json.loads(loc.replace("'", '"'))
                lon, lat = parsed.get("lon"), parsed.get("lat")
            except (json.JSONDecodeError, AttributeError):
                pass
        if lon is None or lat is None:
            skipped += 1
            continue

        points.append(Point(float(lon), float(lat)))
        attrs = {k: v for k, v in row.items() if k != "geometry"}
        rows.append(attrs)

    if skipped:
        logger.warning("Skipped %d substations with missing substation_location", skipped)

    points_gdf = gpd.GeoDataFrame(rows, geometry=points, crs=TARGET_CRS)
    out_path = DATA_PROCESSED / SUBSTATIONS_POINTS_OUT_NAME
    _write_geojson(points_gdf, out_path)
    file_size = out_path.stat().st_size
    logger.info("Wrote %s (%d points, %d bytes)", out_path, len(points_gdf), file_size)

    properties_keys = [c for c in points_gdf.columns if c != "geometry"]
    tier_counts = (
        points_gdf["voltage_tier"].value_counts().to_dict()
        if "voltage_tier" in points_gdf.columns
        else {}
    )
    _write_manifest(
        DATA_PROCESSED / SUBSTATIONS_POINTS_MANIFEST_NAME,
        name="NPg substation point markers (paired with heatmap catchments)",
        source_url=HEATMAP_URL,
        feature_count=len(points_gdf),
        file_size_bytes=file_size,
        properties_keys=properties_keys,
        geometry_type="Point",
        voltage_tier_counts=tier_counts,
    )
    return out_path


def _concat_gdfs(gdfs: list[gpd.GeoDataFrame]) -> gpd.GeoDataFrame:
    """Concatenate GeoDataFrames without importing pandas directly.

    Builds a unified column set, fills missing columns with None, and emits a
    new GeoDataFrame in TARGET_CRS. Suitable for the ECR two-tier merge.
    """
    if not gdfs:
        return gpd.GeoDataFrame(geometry=[], crs=TARGET_CRS)

    all_cols: list[str] = []
    seen: set[str] = set()
    for g in gdfs:
        for c in g.columns:
            if c not in seen:
                seen.add(c)
                all_cols.append(c)
    if "geometry" not in seen:
        all_cols.append("geometry")

    rows: list[dict] = []
    for g in gdfs:
        records = g.to_dict(orient="records")
        for rec in records:
            row = {c: rec.get(c) for c in all_cols}
            rows.append(row)

    geom = [r.pop("geometry") for r in rows]
    return gpd.GeoDataFrame(rows, geometry=geom, crs=TARGET_CRS)


def _load_ecr_tier(url: str, cache_path: Path, tier: str) -> gpd.GeoDataFrame:
    _download_to_cache(url, cache_path)
    logger.info("Reading ECR tier=%s from %s", tier, cache_path)
    gdf = gpd.read_file(cache_path)
    gdf["tier"] = tier
    logger.info("ECR tier=%s loaded %d features (CRS=%s)", tier, len(gdf), gdf.crs)
    return gdf


def download_ecr() -> Path:
    """Download both ECR tiers, tag, concat, clip, write processed output.

    Returns the path to the processed GeoJSON.
    """
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    ge_path = DATA_RAW / ECR_GE1MW_RAW_NAME
    lt_path = DATA_RAW / ECR_LT1MW_RAW_NAME

    ge_gdf = _load_ecr_tier(ECR_GE1MW_URL, ge_path, "ge1mw")
    lt_gdf = _load_ecr_tier(ECR_LT1MW_URL, lt_path, "lt1mw")

    # Reproject each to target CRS before concatenating.
    ge_gdf = _ensure_target_crs(ge_gdf)
    lt_gdf = _ensure_target_crs(lt_gdf)

    combined = _concat_gdfs([ge_gdf, lt_gdf])
    raw_count = len(combined)
    logger.info(
        "ECR combined raw count: %d (ge1mw=%d, lt1mw=%d)",
        raw_count,
        len(ge_gdf),
        len(lt_gdf),
    )

    combined, _ = _fix_invalid_geoms(combined)
    # Drop rows that have no geometry — ECR records sometimes lack coords.
    no_geom = combined.geometry.isna() | combined.geometry.is_empty
    if no_geom.any():
        logger.warning("Dropping %d ECR rows with missing/empty geometry", int(no_geom.sum()))
        combined = combined[~no_geom].copy()

    mask = _load_ne_mask()
    logger.info("Clipping ECR to NE England polygon…")
    clipped = gpd.clip(combined, mask)
    clipped = clipped[~clipped.geometry.is_empty & clipped.geometry.notna()].copy()
    logger.info("ECR: %d -> %d features after NE clip", raw_count, len(clipped))

    out_path = DATA_PROCESSED / ECR_OUT_NAME
    _write_geojson(clipped, out_path)
    file_size = out_path.stat().st_size
    logger.info("Wrote %s (%d bytes)", out_path, file_size)

    properties_keys = [c for c in clipped.columns if c != "geometry"]
    _write_manifest(
        DATA_PROCESSED / ECR_MANIFEST_NAME,
        name="NPg Embedded Capacity Register (combined ge1mw + lt1mw)",
        source_url=[ECR_GE1MW_URL, ECR_LT1MW_URL],
        feature_count=len(clipped),
        file_size_bytes=file_size,
        properties_keys=properties_keys,
        geometry_type=_summarise_geom_type(clipped),
    )
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    headroom_path = download_headroom()
    ecr_path = download_ecr()
    logger.info("Done. headroom=%s ecr=%s", headroom_path, ecr_path)
