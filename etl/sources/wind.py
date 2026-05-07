"""Global Wind Atlas v3 wind speed at 100m raster ETL — UK extract clipped to NE England.

The Global Wind Atlas (CC-BY 4.0, DTU + World Bank Group) publishes country
level GIS data bundles via their public web download portal. The user manually
downloads the UK 100m wind-speed GeoTIFF (the portal requires an interactive
session) into ``~/Downloads``; this module copies it into the canonical raw
cache, clips to the NE England bbox, and writes a compressed, tiled raster
suitable for live windowed reads from the FastAPI ``/api/site-score`` endpoint.

Steps:

1. Copy ``~/Downloads/GBR_wind-speed_100m.tif`` -> ``DATA_RAW`` (cached). The
   original is left in Downloads in case the user wants it.
2. Window-read the NE bbox from the cached UK raster (already EPSG:4326,
   ~250m resolution).
3. Write a small DEFLATE-compressed tiled GeoTIFF to
   ``DATA_RAW / "wind_speed_100m.tif"``.
4. Compute min/max/mean of the clipped raster (excluding NaN nodata and zeros)
   and emit a sidecar manifest at ``DATA_PROCESSED / "wind.manifest.json"``.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window, from_bounds

from etl.config import DATA_PROCESSED, DATA_RAW, NE_BBOX, TARGET_CRS

logger = logging.getLogger(__name__)

# Source: user-supplied UK extract from globalwindatlas.info (interactive).
DOWNLOADS_TIF = Path.home() / "Downloads" / "GBR_wind-speed_100m.tif"
RAW_UK_NAME = "GBR_wind-speed_100m.tif"
CLIPPED_NAME = "wind_speed_100m.tif"
MANIFEST_NAME = "wind.manifest.json"

SOURCE_URL = "https://globalwindatlas.info/en/download/gis-files"
LICENSE = "CC-BY 4.0"
ATTRIBUTION = "Global Wind Atlas 3.0, Technical University of Denmark (DTU) and World Bank Group"
SOURCE_VERSION = "v3.0"
SOURCE_LAST_UPDATED = "2024"
HEIGHT_AGL_M = 100
NOMINAL_RESOLUTION_M = 250


def _ensure_raw_cache(downloads_path: Path, cache_path: Path) -> Path:
    """Copy the user's downloaded UK TIFF into the raw cache if needed.

    If the cache already holds the file (and the Downloads copy is gone), we
    just use the cache. Otherwise we ``shutil.copy2`` so the user's original
    stays put.
    """
    if cache_path.exists() and cache_path.stat().st_size > 0:
        logger.info("Using cached UK wind raster: %s", cache_path)
        return cache_path

    if not downloads_path.exists():
        raise FileNotFoundError(
            f"Source wind TIFF not found at {downloads_path} and no cache at "
            f"{cache_path}. Download GBR_wind-speed_100m.tif from "
            "globalwindatlas.info/en/download/gis-files."
        )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Copying %s -> %s", downloads_path, cache_path)
    shutil.copy2(downloads_path, cache_path)
    logger.info("Cached %d bytes to %s", cache_path.stat().st_size, cache_path)
    return cache_path


def _clip_to_ne(raw_path: Path, clipped_path: Path) -> tuple[float, float, float]:
    """Window-read NE bbox from ``raw_path`` and write a tiled DEFLATE GeoTIFF.

    Returns ``(min, max, mean)`` of the clipped band, computed over valid
    (finite, > 0) pixels.
    """
    clipped_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(raw_path) as src:
        if src.crs is None or str(src.crs).upper() != TARGET_CRS:
            raise RuntimeError(
                f"Expected source CRS {TARGET_CRS}, got {src.crs}; "
                "warp logic not implemented (Global Wind Atlas tiles are 4326)."
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
        # Preserve NaN nodata sentinel from the source.
        if src.nodata is not None:
            profile["nodata"] = src.nodata

        with rasterio.open(clipped_path, "w", **profile) as dst:
            dst.write(data, 1)

        # Drop NaN/inf and any non-positive sentinels (the source uses NaN as
        # nodata; positive-only filter also guards against 0 fill).
        mask = np.isfinite(data) & (data > 0)
        valid = data[mask]
        if valid.size == 0:
            raise RuntimeError("Clipped wind raster has no valid pixels")
        return float(valid.min()), float(valid.max()), float(valid.mean())


def _write_manifest(
    manifest_path: Path,
    *,
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
        "name": "Global Wind Atlas v3 — Wind Speed at 100m",
        "source_url": SOURCE_URL,
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
        "units": "m/s",
        "height_above_ground_m": HEIGHT_AGL_M,
        "file_size_bytes": file_size_bytes,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as fh:
        json.dump(payload, fh, indent=2)
    logger.info("Wrote manifest %s", manifest_path)


def clip_wind() -> Path:
    """Cache UK 100m wind raster, clip to NE England bbox, write outputs.

    Returns the path to the canonical clipped raster
    (``DATA_RAW / "wind_speed_100m.tif"``).
    """
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    raw_uk = DATA_RAW / RAW_UK_NAME
    _ensure_raw_cache(DOWNLOADS_TIF, raw_uk)

    clipped_path = DATA_RAW / CLIPPED_NAME
    min_v, max_v, mean_v = _clip_to_ne(raw_uk, clipped_path)
    logger.info(
        "Clipped wind speed @100m min=%.2f max=%.2f mean=%.2f m/s",
        min_v,
        max_v,
        mean_v,
    )

    with rasterio.open(clipped_path) as src:
        cl_bounds = src.bounds
        cl_crs = str(src.crs)
    logger.info("Clipped bounds=%s CRS=%s", cl_bounds, cl_crs)

    file_size = clipped_path.stat().st_size

    _write_manifest(
        DATA_PROCESSED / MANIFEST_NAME,
        raster_path=clipped_path,
        bbox=NE_BBOX,
        crs=cl_crs,
        resolution_m=float(NOMINAL_RESOLUTION_M),
        min_value=min_v,
        max_value=max_v,
        mean_value=mean_v,
        file_size_bytes=file_size,
    )
    return clipped_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = clip_wind()
    logger.info("Done. wind raster=%s", out)
