"""Attach raw measurement attributes to merged INSPIRE parcels (Wave 5C).

For each parcel in ``data/processed/parcels.geojson`` (33,969 polygons across
the 12 NE England LADs), we compute and attach:

* ``centroid_lon`` / ``centroid_lat`` — parcel centroid in WGS84.
* ``mean_pvout_kwhkwp`` — mean Global Horizontal solar PVOUT (kWh/kWp/yr)
  inside the parcel polygon, from ``data/raw/solar_pvout.tif``. Pixels with a
  float32-min sentinel (treated as values < 1.0) are ignored.
* ``mean_wind_speed_100m_ms`` — mean 100 m wind speed (m/s) from
  ``data/raw/wind_speed_100m.tif``. Nodata is NaN.
* ``dist_substation_gen_headroom_m`` — Euclidean distance (BNG / EPSG:27700)
  from the parcel centroid to the nearest substation polygon centroid where
  ``genhr > 0`` (i.e. has gen headroom).
* ``dist_substation_any_headroom_m`` — same, but to the nearest substation
  with any headroom (``genhr > 0`` *or* ``demhr > 0``).
* ``nearest_substation_name`` — name of the nearest substation matching
  ``dist_substation_any_headroom_m``.
* ``intersects_aonb`` / ``intersects_national_park`` /
  ``intersects_green_belt`` / ``intersects_sssi`` / ``intersects_flood`` —
  boolean flags for any spatial overlap with the corresponding constraint
  layer in ``data/processed/constraints/``.

We **do not** synthesise any composite "score" here. The frontend filters and
editable thresholds operate over these raw measurements directly so the
scoring logic remains transparent and adjustable.

The canonical entry point is :func:`attach_parcel_attributes`. CLI:

    uv run python -m etl.attributes

The output is ``data/processed/parcels_attributed.geojson``. The original
``parcels.geojson`` is left in place as the un-attributed source.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import rasterio
import shapely
from rasterio.mask import mask
from scipy.spatial import cKDTree

from etl.config import DATA_PROCESSED, DATA_RAW

logger = logging.getLogger(__name__)

PARCELS_INPUT_NAME: str = "parcels.geojson"
PARCELS_OUTPUT_NAME: str = "parcels_attributed.geojson"
MANIFEST_NAME: str = "parcels.manifest.json"

SOLAR_TIF: Path = DATA_RAW / "solar_pvout.tif"
WIND_TIF: Path = DATA_RAW / "wind_speed_100m.tif"
NPG_HEADROOM_PATH: Path = DATA_PROCESSED / "npg_headroom.geojson"
CONSTRAINTS_DIR: Path = DATA_PROCESSED / "constraints"

# Solar TIFF uses a float32-min sentinel for nodata. Values below this are
# treated as nodata (the sentinel is ~1.18e-38 — well below any plausible
# real PVOUT value, which is in the hundreds-to-thousands of kWh/kWp/yr).
SOLAR_NODATA_THRESHOLD: float = 1.0


def _fix_geometries(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Repair invalid geometries via ``shapely.make_valid``.

    ``make_valid`` can return GeometryCollections containing a mix of
    Polygon/MultiPolygon and Line/Point parts. We keep only the polygonal
    parts so downstream rasterio.mask + sjoin keep working.
    """
    gdf = gdf.copy()
    gdf["geometry"] = shapely.make_valid(gdf.geometry.values)

    def _polygonal_part(geom):
        if geom is None or geom.is_empty:
            return geom
        if geom.geom_type in {"Polygon", "MultiPolygon"}:
            return geom
        # GeometryCollection or similar — pull out the first polygonal piece.
        if hasattr(geom, "geoms"):
            for sub in geom.geoms:
                if sub.geom_type in {"Polygon", "MultiPolygon"}:
                    return sub
        return geom

    gdf["geometry"] = gdf.geometry.apply(_polygonal_part)
    return gdf


def _add_centroids(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Attach centroid lon/lat in EPSG:4326."""
    centroids = gdf.geometry.centroid
    gdf["centroid_lon"] = centroids.x
    gdf["centroid_lat"] = centroids.y
    return gdf


def _sample_raster_means(
    geoms: list,
    raster_path: Path,
    *,
    nodata_threshold: float | None = None,
) -> list[float]:
    """Compute the mean raster value inside each geometry.

    Uses ``rasterio.mask`` per geometry. Pixels are treated as nodata if:

    * The raster's declared nodata is reached (rasterio returns these masked
      already, so they appear as NaN after we cast to float).
    * ``nodata_threshold`` is set and the value falls below it (used for
      Solar Atlas, where the float32-min sentinel manifests as values
      effectively zero).

    Returns one float (or NaN) per geometry. Parcels that don't overlap the
    raster get NaN.
    """
    means: list[float] = []
    with rasterio.open(raster_path) as src:
        for geom in geoms:
            value = float("nan")
            # Try strict masking first (only pixels whose centre falls inside
            # the polygon). For small parcels and coarse rasters (e.g. the
            # Solar Atlas tile here is ~925 m / pixel) most parcels miss every
            # pixel centre — fall back to ``all_touched=True`` so we still
            # get a representative value from any overlapping pixel.
            for all_touched in (False, True):
                try:
                    data, _ = mask(src, [geom], crop=True, all_touched=all_touched)
                except ValueError:
                    # Geometry doesn't overlap the raster's bounds.
                    break
                arr = data[0].astype("float64")
                # finite check rejects NaN/Inf (NaN is the nodata for wind).
                valid_mask = np.isfinite(arr)
                if nodata_threshold is not None:
                    valid_mask &= arr >= nodata_threshold
                if valid_mask.any():
                    value = float(arr[valid_mask].mean())
                    break
            means.append(value)
    return means


def _add_substation_distances(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Compute distance (m) from each parcel centroid to nearest substation.

    Two distances are produced:

    * ``dist_substation_gen_headroom_m`` — nearest substation with
      ``genhr > 0``.
    * ``dist_substation_any_headroom_m`` — nearest substation with
      ``genhr > 0 OR demhr > 0``.

    Plus the name of the latter as ``nearest_substation_name``.

    All work done in BNG (EPSG:27700) so distances come out in metres.
    """
    parcels_bng = gdf.to_crs("EPSG:27700")
    parcel_centroids = np.column_stack(
        [parcels_bng.geometry.centroid.x.values, parcels_bng.geometry.centroid.y.values]
    )

    subs = gpd.read_file(NPG_HEADROOM_PATH, engine="pyogrio").to_crs("EPSG:27700")
    sub_centroids = np.column_stack(
        [subs.geometry.centroid.x.values, subs.geometry.centroid.y.values]
    )

    gen_mask = subs["genhr"].fillna(0) > 0
    if not gen_mask.any():
        raise ValueError("No substations have genhr > 0 — check headroom dataset")
    gen_centroids = sub_centroids[gen_mask.values]
    gen_tree = cKDTree(gen_centroids)
    dist_gen, _ = gen_tree.query(parcel_centroids, k=1)
    gdf["dist_substation_gen_headroom_m"] = dist_gen.round(0)

    any_mask = (subs["genhr"].fillna(0) > 0) | (subs["demhr"].fillna(0) > 0)
    if not any_mask.any():
        raise ValueError("No substations have any headroom — check headroom dataset")
    any_centroids = sub_centroids[any_mask.values]
    any_tree = cKDTree(any_centroids)
    dist_any, idx_any = any_tree.query(parcel_centroids, k=1)
    gdf["dist_substation_any_headroom_m"] = dist_any.round(0)
    sub_names = subs.loc[any_mask, "name"].reset_index(drop=True)
    gdf["nearest_substation_name"] = sub_names.iloc[idx_any].values
    return gdf


def _add_intersect_flag(gdf: gpd.GeoDataFrame, constraint_path: Path, col: str) -> gpd.GeoDataFrame:
    """Mark each parcel True if it intersects any feature in ``constraint_path``.

    Uses ``geopandas.sjoin`` with the ``intersects`` predicate. Falls back to
    all-False when the constraint layer is empty or missing.
    """
    if not constraint_path.exists():
        logger.warning("Constraint %s missing — defaulting %s to False", constraint_path, col)
        gdf[col] = False
        return gdf

    cgdf = gpd.read_file(constraint_path, engine="pyogrio")
    if len(cgdf) == 0:
        gdf[col] = False
        return gdf
    target_crs = gdf.crs
    if target_crs is not None and cgdf.crs != target_crs:
        cgdf = cgdf.to_crs(target_crs)

    joined = gpd.sjoin(
        gdf[["parcel_id", "geometry"]],
        cgdf[["geometry"]],
        predicate="intersects",
        how="left",
    )
    matched_ids = set(joined.loc[joined.index_right.notna(), "parcel_id"])
    gdf[col] = gdf["parcel_id"].isin(matched_ids)
    return gdf


def _summary_stats(values: np.ndarray) -> dict[str, float | None]:
    """Min/max/mean for a numeric column, ignoring NaN.

    Returns ``None`` for any stat when no finite values exist (so the
    JSON-serialised manifest reads ``null`` rather than e.g. ``NaN``).
    """
    arr = np.asarray(values, dtype="float64")
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return {"min": None, "max": None, "mean": None}
    return {
        "min": round(float(finite.min()), 4),
        "max": round(float(finite.max()), 4),
        "mean": round(float(finite.mean()), 4),
    }


def _update_manifest(
    gdf: gpd.GeoDataFrame,
    *,
    output_path: Path,
    manifest_path: Path,
    constraint_columns: list[str],
) -> None:
    """Overwrite ``parcels.manifest.json`` with attribute summary stats.

    Preserves the existing per-LAD breakdown / source / license blob and
    adds an ``attributes_attached`` block describing what was added in
    Wave 5C.
    """
    if manifest_path.exists():
        with manifest_path.open() as fh:
            payload: dict[str, Any] = json.load(fh)
    else:
        payload = {}

    constraint_overlap_counts = {
        col: int(gdf[col].sum()) for col in constraint_columns if col in gdf.columns
    }

    summary_stats = {
        "mean_pvout_kwhkwp": _summary_stats(gdf["mean_pvout_kwhkwp"].values),
        "mean_wind_speed_100m_ms": _summary_stats(gdf["mean_wind_speed_100m_ms"].values),
        "dist_substation_gen_headroom_m": _summary_stats(
            gdf["dist_substation_gen_headroom_m"].values
        ),
        "dist_substation_any_headroom_m": _summary_stats(
            gdf["dist_substation_any_headroom_m"].values
        ),
        "constraint_overlap_counts": constraint_overlap_counts,
    }

    payload["attributes_attached"] = True
    payload["attributed_output_path"] = str(output_path)
    payload["attributed_file_size_bytes"] = output_path.stat().st_size
    payload["attribute_columns"] = [
        "area_ha",
        "centroid_lon",
        "centroid_lat",
        "mean_pvout_kwhkwp",
        "mean_wind_speed_100m_ms",
        "dist_substation_gen_headroom_m",
        "dist_substation_any_headroom_m",
        "nearest_substation_name",
        "intersects_aonb",
        "intersects_national_park",
        "intersects_green_belt",
        "intersects_sssi",
        "intersects_flood",
    ]
    payload["summary_stats"] = summary_stats

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as fh:
        json.dump(payload, fh, indent=2)
    logger.info("Updated manifest %s", manifest_path)


def attach_parcel_attributes() -> Path:
    """Compute and attach per-parcel attributes; write attributed GeoJSON.

    Returns the path to ``data/processed/parcels_attributed.geojson``.
    """
    parcels_path = DATA_PROCESSED / PARCELS_INPUT_NAME
    output_path = DATA_PROCESSED / PARCELS_OUTPUT_NAME
    manifest_path = DATA_PROCESSED / MANIFEST_NAME

    if not parcels_path.exists():
        raise FileNotFoundError(
            f"{parcels_path} not found — run `uv run python -m etl.sources.inspire` first"
        )

    timings: dict[str, float] = {}

    t0 = time.perf_counter()
    logger.info("Reading %s", parcels_path)
    gdf = gpd.read_file(parcels_path, engine="pyogrio")
    logger.info("Loaded %d parcels (CRS=%s)", len(gdf), gdf.crs)
    timings["read_parcels"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    logger.info("Repairing invalid geometries via shapely.make_valid")
    gdf = _fix_geometries(gdf)
    timings["fix_geometries"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    logger.info("Computing parcel centroids")
    gdf = _add_centroids(gdf)
    timings["centroids"] = time.perf_counter() - t0

    t0 = time.perf_counter()
    logger.info("Sampling solar PVOUT raster (%d parcels)", len(gdf))
    gdf["mean_pvout_kwhkwp"] = _sample_raster_means(
        list(gdf.geometry), SOLAR_TIF, nodata_threshold=SOLAR_NODATA_THRESHOLD
    )
    timings["solar_sample"] = time.perf_counter() - t0
    logger.info("Solar sampling done in %.1fs", timings["solar_sample"])

    t0 = time.perf_counter()
    logger.info("Sampling 100 m wind raster (%d parcels)", len(gdf))
    gdf["mean_wind_speed_100m_ms"] = _sample_raster_means(
        list(gdf.geometry), WIND_TIF, nodata_threshold=None
    )
    timings["wind_sample"] = time.perf_counter() - t0
    logger.info("Wind sampling done in %.1fs", timings["wind_sample"])

    t0 = time.perf_counter()
    logger.info("Computing substation distances (cKDTree on BNG centroids)")
    gdf = _add_substation_distances(gdf)
    timings["substations"] = time.perf_counter() - t0

    constraint_columns: list[str] = []
    constraint_specs = [
        ("national-landscape.geojson", "intersects_aonb"),
        ("national-park.geojson", "intersects_national_park"),
        ("green-belt.geojson", "intersects_green_belt"),
        ("site-of-special-scientific-interest.geojson", "intersects_sssi"),
        ("flood_zones.geojson", "intersects_flood"),
    ]

    t0 = time.perf_counter()
    for filename, col in constraint_specs:
        logger.info("Computing %s overlap (%s)", col, filename)
        gdf = _add_intersect_flag(gdf, CONSTRAINTS_DIR / filename, col)
        constraint_columns.append(col)
    timings["constraints"] = time.perf_counter() - t0

    # Sanity: enforce bool dtype for the intersects_* columns.
    for col in constraint_columns:
        gdf[col] = gdf[col].astype(bool)

    t0 = time.perf_counter()
    logger.info("Writing attributed GeoJSON -> %s", output_path)
    if output_path.exists():
        output_path.unlink()
    gdf.to_file(output_path, driver="GeoJSON", engine="pyogrio")
    timings["write"] = time.perf_counter() - t0
    logger.info("Wrote %s (%d bytes)", output_path, output_path.stat().st_size)

    _update_manifest(
        gdf,
        output_path=output_path,
        manifest_path=manifest_path,
        constraint_columns=constraint_columns,
    )

    total = sum(timings.values())
    logger.info(
        "Attribute attachment complete in %.1fs (per step: %s)",
        total,
        ", ".join(f"{k}={v:.1f}s" for k, v in timings.items()),
    )

    return output_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    attach_parcel_attributes()
