"""Global Solar Atlas PVOUT raster ETL — UK extract clipped to NE England.

The Global Solar Atlas (CC-BY 4.0, World Bank / Solargis) publishes country
level GIS data bundles via their public API. We pull the UK PVOUT (long-term
average yearly photovoltaic output, kWh/kWp/year) GeoTIFF, clip it to the NE
England bbox, and write a compressed, tiled raster suitable for live windowed
reads from the FastAPI ``/api/site-score`` endpoint.

Steps:

1. Download the UK GIS bundle zip into ``DATA_RAW`` (cached).
2. Extract ``PVOUT.tif`` (already EPSG:4326, ~1km resolution).
3. Window-read the NE bbox, write a small DEFLATE-compressed tiled GeoTIFF to
   ``DATA_RAW / "solar_pvout.tif"``.
4. Compute min/max/mean of the clipped raster and emit a sidecar manifest at
   ``DATA_PROCESSED / "solar.manifest.json"``.
"""

from __future__ import annotations

import json
import logging
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window, from_bounds

from etl.config import DATA_PROCESSED, DATA_RAW, NE_BBOX, TARGET_CRS

logger = logging.getLogger(__name__)

# Public Global Solar Atlas API (extracted from the SPA index.html
# meta name="x-api-base"). The /download/<country>/<filename> route returns a
# 302 redirect to a presigned S3 URL for the actual data archive.
GSA_API_BASE = "https://2eueu84zmf.execute-api.eu-west-1.amazonaws.com/prod"
UK_BUNDLE_NAME = "United-Kingdom_GISdata_LTAym_YearlyMonthlyTotals_GlobalSolarAtlas-v2_GEOTIFF.zip"
UK_BUNDLE_URL = f"{GSA_API_BASE}/download/United%20Kingdom/{UK_BUNDLE_NAME}"

# Inside the zip, the yearly PVOUT raster lives at this path.
PVOUT_MEMBER = (
    "United-Kingdom_GISdata_LTAy_YearlyMonthlyTotals_GlobalSolarAtlas-v2_GEOTIFF/PVOUT.tif"
)

RAW_BUNDLE_NAME = "solar_uk_bundle.zip"
RAW_PVOUT_UK_NAME = "solar_pvout_uk.tif"
CLIPPED_NAME = "solar_pvout.tif"
MANIFEST_NAME = "solar.manifest.json"

LICENSE = "CC-BY 4.0"
ATTRIBUTION = "© 2024 The World Bank, Solar resource data: Solargis."
SOURCE_VERSION = "v2.x"
SOURCE_LAST_UPDATED = "2024"

DOWNLOAD_TIMEOUT_S = 600


def _download_to_cache(url: str, cache_path: Path) -> Path:
    """Stream-download ``url`` to ``cache_path`` if not already present."""
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info("Using cached download: %s", cache_path)
        return cache_path

    # Local import keeps the module importable on minimal envs.
    import requests

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


def _extract_pvout(bundle_path: Path, out_path: Path) -> Path:
    """Extract the yearly PVOUT.tif from the UK bundle zip."""
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.info("Using cached PVOUT extract: %s", out_path)
        return out_path

    out_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Extracting %s from %s", PVOUT_MEMBER, bundle_path)
    with zipfile.ZipFile(bundle_path) as zf:
        with zf.open(PVOUT_MEMBER) as src, out_path.open("wb") as dst:
            dst.write(src.read())
    logger.info("Wrote %d bytes to %s", out_path.stat().st_size, out_path)
    return out_path


def _clip_to_ne(raw_path: Path, clipped_path: Path) -> tuple[float, float, float]:
    """Window-read NE bbox from ``raw_path`` and write a tiled DEFLATE GeoTIFF.

    Returns ``(min, max, mean)`` of the clipped band, computed over valid
    (non-NaN, non-nodata) pixels.
    """
    clipped_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(raw_path) as src:
        if src.crs is None or str(src.crs).upper() != TARGET_CRS:
            # Solar Atlas tiles are already EPSG:4326; we don't expect to hit
            # this branch but log loudly if we do.
            raise RuntimeError(
                f"Expected source CRS {TARGET_CRS}, got {src.crs}; "
                "warp logic not implemented (current data is 4326)."
            )

        window = from_bounds(*NE_BBOX, transform=src.transform).round_offsets().round_lengths()
        # Clip the window to the source extent so we never read past the edge.
        window = window.intersection(Window(0, 0, src.width, src.height))
        logger.info(
            "NE window: col_off=%s row_off=%s w=%s h=%s",
            window.col_off,
            window.row_off,
            window.width,
            window.height,
        )

        data = src.read(1, window=window)
        transform = src.window_transform(window)

        profile = src.profile.copy()
        profile.update(
            driver="GTiff",
            height=int(window.height),
            width=int(window.width),
            transform=transform,
            compress="DEFLATE",
            tiled=True,
            blockxsize=256,
            blockysize=256,
            predictor=3,  # float predictor
        )

        with rasterio.open(clipped_path, "w", **profile) as dst:
            dst.write(data, 1)

        # Build a clean mask: drop NaN and the source nodata sentinel
        # (Solar Atlas uses ~1.175e-38, the float32 min normal).
        mask = np.isfinite(data)
        if src.nodata is not None and np.isfinite(src.nodata):
            mask &= data != src.nodata
        valid = data[mask]
        if valid.size == 0:
            raise RuntimeError("Clipped PVOUT raster has no valid pixels")
        return float(valid.min()), float(valid.max()), float(valid.mean())


def _write_manifest(
    manifest_path: Path,
    *,
    source_url: str,
    raster_path: Path,
    bbox: tuple[float, float, float, float],
    crs: str,
    resolution_m: float,
    min_value: float,
    max_value: float,
    mean_value: float,
    file_size_bytes: int,
) -> None:
    payload = {
        "name": "Global Solar Atlas PVOUT (long-term-average)",
        "source_url": source_url,
        "license": LICENSE,
        "attribution": ATTRIBUTION,
        "last_updated": SOURCE_LAST_UPDATED,
        "version": SOURCE_VERSION,
        "raster_path": str(raster_path.relative_to(raster_path.parents[2])),
        "bbox": list(bbox),
        "crs": crs,
        "resolution_m": resolution_m,
        "min_value": min_value,
        "max_value": max_value,
        "mean_value": mean_value,
        "units": "kWh/kWp/year",
        "file_size_bytes": file_size_bytes,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as fh:
        json.dump(payload, fh, indent=2)
    logger.info("Wrote manifest %s", manifest_path)


def _approx_resolution_m(raw_path: Path) -> float:
    """Approximate pixel resolution in metres at the centre of the NE bbox."""
    with rasterio.open(raw_path) as src:
        deg_x, deg_y = src.res  # degrees per pixel
    centre_lat = (NE_BBOX[1] + NE_BBOX[3]) / 2.0
    # Rough metres-per-degree at this latitude.
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * float(np.cos(np.radians(centre_lat)))
    res_y_m = deg_y * m_per_deg_lat
    res_x_m = deg_x * m_per_deg_lon
    return float((res_x_m + res_y_m) / 2.0)


def download_and_clip_solar() -> Path:
    """Download UK PVOUT GeoTIFF, clip to NE England bbox, write outputs.

    Returns the path to the canonical clipped raster
    (``DATA_RAW / "solar_pvout.tif"``).
    """
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    bundle_path = DATA_RAW / RAW_BUNDLE_NAME
    _download_to_cache(UK_BUNDLE_URL, bundle_path)

    raw_pvout = DATA_RAW / RAW_PVOUT_UK_NAME
    _extract_pvout(bundle_path, raw_pvout)

    clipped_path = DATA_RAW / CLIPPED_NAME
    min_v, max_v, mean_v = _clip_to_ne(raw_pvout, clipped_path)
    logger.info(
        "Clipped PVOUT min=%.1f max=%.1f mean=%.1f kWh/kWp/yr",
        min_v,
        max_v,
        mean_v,
    )

    with rasterio.open(clipped_path) as src:
        cl_bounds = src.bounds
        cl_crs = str(src.crs)
    logger.info("Clipped bounds=%s CRS=%s", cl_bounds, cl_crs)

    res_m = _approx_resolution_m(raw_pvout)
    file_size = clipped_path.stat().st_size

    _write_manifest(
        DATA_PROCESSED / MANIFEST_NAME,
        source_url=UK_BUNDLE_URL,
        raster_path=clipped_path,
        bbox=NE_BBOX,
        crs=cl_crs,
        resolution_m=res_m,
        min_value=min_v,
        max_value=max_v,
        mean_value=mean_v,
        file_size_bytes=file_size,
    )
    return clipped_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = download_and_clip_solar()
    logger.info("Done. solar raster=%s", out)
