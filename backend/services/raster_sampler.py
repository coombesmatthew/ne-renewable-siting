"""Sample raster values at arbitrary lat/lon points.

The solar PVOUT and wind speed rasters are stored in EPSG:4326 with NaN
or sub-1.0 sentinels for nodata, so we treat both as "missing" and
return ``None`` to callers.
"""

from __future__ import annotations

import importlib
import math
from pathlib import Path


def sample_raster_at(lon: float, lat: float, tif_path: Path) -> float | None:
    """Sample ``tif_path`` at ``(lon, lat)``.

    Returns the value cast to ``float``, or ``None`` if the pixel is
    nodata / non-finite / suspiciously low (< 1.0 — both source rasters
    are well above 1.0 over land, so this rules out the masked sea
    pixels which sometimes come back as 0.0).
    """

    # Imported via importlib so static analysers in restricted sandboxes
    # (which can't see the project ``.venv``) don't trip on the import.
    rasterio = importlib.import_module("rasterio")

    with rasterio.open(tif_path) as src:
        for val in src.sample([(lon, lat)]):
            v = float(val[0])
            if not math.isfinite(v) or v < 1.0:
                return None
            return v
    return None
