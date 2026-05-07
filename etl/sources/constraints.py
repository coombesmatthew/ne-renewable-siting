"""Planning + flood + hydropower constraint ETL for NE England.

Three families of fetchers:

1. ``download_planning_constraints`` — pulls six datasets from
   planning.data.gov.uk (green-belt, area-of-outstanding-natural-beauty,
   national-park, site-of-special-scientific-interest, listed-building,
   scheduled-monument). Each is downloaded as a national bulk GeoJSON,
   read with a bbox pre-filter to keep memory bounded, clipped to the NE
   polygon, reprojected to ``TARGET_CRS``, and written to
   ``DATA_PROCESSED / constraints / <slug>.geojson`` with a sidecar
   manifest. The bulk download lives at
   ``https://files.planning.data.gov.uk/dataset/<slug>.geojson``.
   The brief calls one of these "national-landscape" — the live slug is
   the legacy ``area-of-outstanding-natural-beauty`` name, but we keep
   the requested output filename for downstream consumers.

2. ``download_flood_zones`` — fetches EA Flood Map for Planning
   (rivers + sea) zones 2 and 3 over a WFS endpoint. The capabilities
   document advertises a single combined typeName
   ``Flood_Zones_2_3_Rivers_and_Sea`` carrying a ``flood_zone``
   property (``FZ2`` / ``FZ3``). We page through it in WFS chunks using
   the NE bbox, stitch into one GeoDataFrame, clip and write a single
   ``flood_zones.geojson``.

3. ``download_hydropower`` — DEPRECATED. The 2010 EA dataset is
   officially retired on data.gov.uk and the linked download
   (``environment.data.gov.uk/datafiles/<id>``) currently returns 404.
   The function attempts the download once, logs the failure and
   returns ``None`` so the rest of the pipeline keeps moving.

Both families use raw caches under ``DATA_RAW`` so re-runs skip the
network call.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from pathlib import Path
from urllib.parse import urlencode

import geopandas as gpd
import requests
from shapely.geometry import box

from etl.config import DATA_PROCESSED, DATA_RAW, NE_BBOX, NE_POLYGON_PATH, TARGET_CRS

logger = logging.getLogger(__name__)

# --- planning.data.gov.uk -------------------------------------------------

PLANNING_BULK_URL = "https://files.planning.data.gov.uk/dataset/{slug}.geojson"

# (output_slug, source_slug). The brief refers to "national-landscape"
# but the live planning.data.gov.uk slug is still the legacy AONB name.
PLANNING_DATASETS: list[tuple[str, str]] = [
    ("green-belt", "green-belt"),
    ("national-landscape", "area-of-outstanding-natural-beauty"),
    ("national-park", "national-park"),
    ("site-of-special-scientific-interest", "site-of-special-scientific-interest"),
    ("listed-building", "listed-building"),
    ("scheduled-monument", "scheduled-monument"),
]
PLANNING_LICENSE = "OGL 3.0"

# --- EA flood zones -------------------------------------------------------

FLOOD_WFS_BASE = (
    "https://environment.data.gov.uk/spatialdata/flood-map-for-planning-flood-zones/wfs"
)
FLOOD_TYPENAME = "dataset-04532375-a198-476e-985e-0579a0a11b47:Flood_Zones_2_3_Rivers_and_Sea"
FLOOD_PAGE_SIZE = 5000
FLOOD_RAW_NAME = "ea_flood_zones.geojson"
FLOOD_OUT_NAME = "flood_zones.geojson"
FLOOD_LICENSE = "OGL 3.0"

# --- EA hydropower (deprecated) ------------------------------------------

HYDRO_LANDING_URL = "https://www.data.gov.uk/dataset/cda61957-f48b-4b75-b855-a18060302ed1"
HYDRO_DOWNLOAD_URL = "https://environment.data.gov.uk/datafiles/f0bec102512e49878f37d3b082f15358"
HYDRO_RAW_NAME = "ea_hydropower"  # extension determined at fetch time
HYDRO_OUT_NAME = "hydropower.geojson"
HYDRO_LICENSE = "OGL 3.0"

CONSTRAINTS_DIR = DATA_PROCESSED / "constraints"
DOWNLOAD_TIMEOUT_S = 1800


# ----------------------- shared helpers -----------------------------------


def _download_to_cache(url: str, cache_path: Path) -> Path:
    """Download ``url`` to ``cache_path`` if not already cached."""
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
    """Load NE clip polygon in TARGET_CRS."""
    mask = gpd.read_file(NE_POLYGON_PATH)
    if str(mask.crs).upper() != TARGET_CRS:
        mask = mask.to_crs(TARGET_CRS)
    return mask


def _ensure_target_crs(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if gdf.crs is None:
        logger.warning("Source has no CRS — assuming %s", TARGET_CRS)
        gdf = gdf.set_crs(TARGET_CRS)
    elif str(gdf.crs).upper() != TARGET_CRS:
        logger.info("Reprojecting from %s to %s", gdf.crs, TARGET_CRS)
        gdf = gdf.to_crs(TARGET_CRS)
    return gdf


def _fix_invalid_geoms(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Repair invalid geometries via buffer(0)."""
    if gdf.empty:
        return gdf
    invalid_mask = ~gdf.geometry.is_valid
    n_invalid = int(invalid_mask.sum())
    if n_invalid:
        logger.warning("Repairing %d invalid geometries via buffer(0)", n_invalid)
        gdf = gdf.copy()
        gdf.loc[invalid_mask, "geometry"] = gdf.loc[invalid_mask, "geometry"].buffer(0)
    return gdf


def _summarise_geom_type(gdf: gpd.GeoDataFrame) -> str:
    if gdf.empty:
        return "Unknown"
    types = sorted(set(gdf.geometry.geom_type))
    return types[0] if len(types) == 1 else "Mixed:" + ",".join(types)


def _write_geojson(gdf: gpd.GeoDataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    gdf.to_file(out_path, driver="GeoJSON")


def _write_manifest(
    manifest_path: Path,
    *,
    name: str,
    source_url: str | list[str],
    license_str: str,
    feature_count: int,
    file_size_bytes: int,
    properties_keys: list[str],
    geometry_type: str,
    extra: dict | None = None,
) -> None:
    payload: dict = {
        "name": name,
        "source_url": source_url,
        "license": license_str,
        "last_updated": _dt.date.today().isoformat(),
        "feature_count": feature_count,
        "file_size_bytes": file_size_bytes,
        "properties_keys": properties_keys,
        "geometry_type": geometry_type,
    }
    if extra:
        payload.update(extra)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as fh:
        json.dump(payload, fh, indent=2)
    logger.info("Wrote manifest %s", manifest_path)


def _clip_to_ne(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Clip GeoDataFrame to the NE polygon, dropping empties."""
    mask = _load_ne_mask()
    clipped = gpd.clip(gdf, mask)
    clipped = clipped[~clipped.geometry.is_empty & clipped.geometry.notna()].copy()
    clipped = _fix_invalid_geoms(clipped)
    return clipped


# ----------------------- planning.data.gov.uk -----------------------------


def _process_planning_dataset(output_slug: str, source_slug: str) -> Path:
    """Download a planning.data.gov.uk dataset, clip, and write outputs."""
    bulk_url = PLANNING_BULK_URL.format(slug=source_slug)
    cache_path = DATA_RAW / f"planning_{source_slug}.geojson"
    _download_to_cache(bulk_url, cache_path)

    # bbox-filtered read — keeps memory bounded for the 200 MB layers
    # (listed-building, SSSI, scheduled-monument). The bulk file is in
    # EPSG:4326 so NE_BBOX (lon/lat order) applies directly.
    logger.info("Reading %s with bbox=%s", cache_path, NE_BBOX)
    gdf = gpd.read_file(cache_path, bbox=NE_BBOX)
    raw_count = len(gdf)
    logger.info("%s: %d features in NE bbox (CRS=%s)", source_slug, raw_count, gdf.crs)

    gdf = _ensure_target_crs(gdf)
    gdf = _fix_invalid_geoms(gdf)
    clipped = _clip_to_ne(gdf)
    logger.info(
        "%s: %d -> %d after clip to NE polygon",
        source_slug,
        raw_count,
        len(clipped),
    )

    out_path = CONSTRAINTS_DIR / f"{output_slug}.geojson"
    _write_geojson(clipped, out_path)
    file_size = out_path.stat().st_size
    logger.info("Wrote %s (%d bytes)", out_path, file_size)

    properties_keys = [c for c in clipped.columns if c != "geometry"]
    _write_manifest(
        CONSTRAINTS_DIR / f"{output_slug}.manifest.json",
        name=f"planning.data.gov.uk — {source_slug}",
        source_url=bulk_url,
        license_str=PLANNING_LICENSE,
        feature_count=len(clipped),
        file_size_bytes=file_size,
        properties_keys=properties_keys,
        geometry_type=_summarise_geom_type(clipped),
        extra={"output_slug": output_slug, "source_slug": source_slug},
    )
    return out_path


def download_planning_constraints() -> dict[str, Path]:
    """Download all six planning.data.gov.uk constraint layers.

    Returns ``{output_slug: processed_path}`` for each successful layer.
    Layers that fail are logged and excluded.
    """
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    CONSTRAINTS_DIR.mkdir(parents=True, exist_ok=True)

    out: dict[str, Path] = {}
    for output_slug, source_slug in PLANNING_DATASETS:
        try:
            out[output_slug] = _process_planning_dataset(output_slug, source_slug)
        except Exception:  # noqa: BLE001 — keep going for other layers
            logger.exception("Failed to process planning slug=%s", source_slug)
    return out


# ----------------------- EA flood zones -----------------------------------


def _wfs_geojson_request(start_index: int, count: int) -> str:
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": FLOOD_TYPENAME,
        "srsName": "urn:ogc:def:crs:EPSG::4326",
        # JSON output uses bbox=miny,minx,maxy,maxx for EPSG:4326.
        "bbox": f"{NE_BBOX[1]},{NE_BBOX[0]},{NE_BBOX[3]},{NE_BBOX[2]},urn:ogc:def:crs:EPSG::4326",
        "count": count,
        "startIndex": start_index,
        "outputFormat": "application/json",
    }
    return f"{FLOOD_WFS_BASE}?{urlencode(params)}"


def _fetch_flood_zones_paged() -> dict:
    """Page through the WFS endpoint and assemble a single FeatureCollection."""
    features: list = []
    start = 0
    total: int | None = None
    while True:
        url = _wfs_geojson_request(start, FLOOD_PAGE_SIZE)
        logger.info("WFS GetFeature startIndex=%d count=%d", start, FLOOD_PAGE_SIZE)
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT_S)
        resp.raise_for_status()
        page = resp.json()
        page_features = page.get("features", [])
        features.extend(page_features)
        if total is None:
            total = page.get("numberMatched")
            logger.info("WFS reports numberMatched=%s", total)
        n = len(page_features)
        if n == 0 or n < FLOOD_PAGE_SIZE:
            break
        start += FLOOD_PAGE_SIZE
        if total is not None and start >= total:
            break
    fc = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
        "features": features,
    }
    logger.info("Assembled %d total flood-zone features", len(features))
    return fc


def download_flood_zones() -> Path:
    """Download EA Flood Zones 2 + 3, clip to NE polygon, write GeoJSON."""
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    CONSTRAINTS_DIR.mkdir(parents=True, exist_ok=True)

    cache_path = DATA_RAW / FLOOD_RAW_NAME
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info("Using cached flood-zone bundle: %s", cache_path)
    else:
        fc = _fetch_flood_zones_paged()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w") as fh:
            json.dump(fc, fh)
        logger.info(
            "Cached %d flood features to %s (%d bytes)",
            len(fc["features"]),
            cache_path,
            cache_path.stat().st_size,
        )

    logger.info("Reading flood zones from %s", cache_path)
    gdf = gpd.read_file(cache_path)
    raw_count = len(gdf)
    logger.info("Loaded %d raw flood features (CRS=%s)", raw_count, gdf.crs)

    gdf = _ensure_target_crs(gdf)
    # Tag a top-level numeric `zone` (2 / 3) alongside the source `flood_zone`
    # string ("FZ2" / "FZ3") so downstream styling is straightforward.
    if "flood_zone" in gdf.columns:
        gdf["zone"] = (
            gdf["flood_zone"].astype(str).str.extract(r"(\d+)", expand=False).astype("Int64")
        )
    else:
        logger.warning("flood_zone column missing — leaving zone unset")
        gdf["zone"] = None

    gdf = _fix_invalid_geoms(gdf)
    clipped = _clip_to_ne(gdf)
    logger.info("Flood: %d -> %d after NE clip", raw_count, len(clipped))

    if not clipped.empty:
        zone_counts = clipped["zone"].value_counts(dropna=False).to_dict()
        logger.info("Flood zones distribution: %s", zone_counts)

    out_path = CONSTRAINTS_DIR / FLOOD_OUT_NAME
    _write_geojson(clipped, out_path)
    file_size = out_path.stat().st_size
    logger.info("Wrote %s (%d bytes)", out_path, file_size)

    properties_keys = [c for c in clipped.columns if c != "geometry"]
    _write_manifest(
        CONSTRAINTS_DIR / "flood_zones.manifest.json",
        name="EA Flood Map for Planning — Flood Zones 2 & 3 (Rivers & Sea)",
        source_url=FLOOD_WFS_BASE,
        license_str=FLOOD_LICENSE,
        feature_count=len(clipped),
        file_size_bytes=file_size,
        properties_keys=properties_keys,
        geometry_type=_summarise_geom_type(clipped),
        extra={"typename": FLOOD_TYPENAME},
    )
    return out_path


# ----------------------- EA hydropower (deprecated) -----------------------


def _attempt_hydro_download(cache_path: Path) -> bool:
    """Try to fetch the deprecated EA hydropower datafile.

    Returns True on success, False if the upstream is gone (404 / non-200).
    """
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info("Using cached hydropower download: %s", cache_path)
        return True
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with requests.get(
            HYDRO_DOWNLOAD_URL, stream=True, timeout=DOWNLOAD_TIMEOUT_S, allow_redirects=True
        ) as resp:
            if resp.status_code != 200:
                logger.warning(
                    "Hydropower download returned HTTP %d — source unavailable",
                    resp.status_code,
                )
                return False
            tmp_path = cache_path.with_suffix(cache_path.suffix + ".part")
            with tmp_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(cache_path)
            logger.info(
                "Cached hydropower download (%d bytes) to %s",
                cache_path.stat().st_size,
                cache_path,
            )
            return True
    except requests.RequestException:
        logger.exception("Hydropower download network error")
        return False


def download_hydropower() -> Path | None:
    """Attempt to fetch the deprecated EA hydropower opportunities dataset.

    Returns the processed path on success, or ``None`` if the upstream
    download is unavailable (which is expected — the dataset has been
    retired by the EA).
    """
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    CONSTRAINTS_DIR.mkdir(parents=True, exist_ok=True)

    # We don't know the file extension up front; the legacy resource
    # was a Shapefile zip but the URL has no suffix. Try .zip first;
    # if the file is already cached under another extension, use it.
    candidates = list(DATA_RAW.glob(f"{HYDRO_RAW_NAME}.*"))
    cache_path = candidates[0] if candidates else DATA_RAW / f"{HYDRO_RAW_NAME}.zip"

    ok = _attempt_hydro_download(cache_path)
    if not ok:
        logger.warning(
            "Hydropower dataset unavailable (deprecated 2010 EA dataset). "
            "Landing page: %s. Skipping layer.",
            HYDRO_LANDING_URL,
        )
        return None

    logger.info("Reading hydropower dataset from %s", cache_path)
    try:
        gdf = gpd.read_file(cache_path)
    except Exception:
        logger.exception("Could not parse hydropower download %s — skipping layer", cache_path)
        return None

    raw_count = len(gdf)
    logger.info("Loaded %d raw hydropower features (CRS=%s)", raw_count, gdf.crs)

    gdf = _ensure_target_crs(gdf)
    gdf = _fix_invalid_geoms(gdf)
    # Hydropower is a small national point/line layer — clip directly.
    bbox_gdf = gpd.GeoDataFrame(geometry=[box(*NE_BBOX)], crs=TARGET_CRS)
    pre = gpd.overlay(gdf, bbox_gdf, how="intersection") if not gdf.empty else gdf
    clipped = _clip_to_ne(pre)
    logger.info("Hydropower: %d -> %d after NE clip", raw_count, len(clipped))

    out_path = CONSTRAINTS_DIR / HYDRO_OUT_NAME
    _write_geojson(clipped, out_path)
    file_size = out_path.stat().st_size
    logger.info("Wrote %s (%d bytes)", out_path, file_size)

    properties_keys = [c for c in clipped.columns if c != "geometry"]
    _write_manifest(
        CONSTRAINTS_DIR / "hydropower.manifest.json",
        name="EA — Potential Sites of Hydropower Opportunity (DEPRECATED)",
        source_url=HYDRO_DOWNLOAD_URL,
        license_str=HYDRO_LICENSE,
        feature_count=len(clipped),
        file_size_bytes=file_size,
        properties_keys=properties_keys,
        geometry_type=_summarise_geom_type(clipped),
        extra={
            "last_updated": "2010-2021",
            "notes": (
                "EA deprecated dataset — last updated 2010, retained for "
                "illustrative low-potential hydro mapping in NE England."
            ),
            "landing_page": HYDRO_LANDING_URL,
        },
    )
    return out_path


# ----------------------- entrypoint ---------------------------------------


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    planning_paths = download_planning_constraints()
    flood_path = download_flood_zones()
    hydro_path = download_hydropower()
    logger.info(
        "Done. planning=%d flood=%s hydro=%s",
        len(planning_paths),
        flood_path,
        hydro_path,
    )
