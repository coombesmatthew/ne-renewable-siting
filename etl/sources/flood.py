"""Flood-risk-zone clip for NE England.

The source is the **national** ``flood-risk-zone`` GeoJSON published by
planning.data.gov.uk:

    https://www.planning.data.gov.uk/dataset/flood-risk-zone

It's a single ~2.6 GB FeatureCollection with one feature per Environment
Agency Flood Zone polygon (levels 2 and 3, Tidal / Fluvial / Defended
models). The user pre-downloads it manually to
``~/Downloads/flood-risk-zone.geojson``; we then clip it to the NE
England polygon. Loading the whole file into RAM is a non-starter, so
we shell out to ``ogr2ogr`` which streams the source and writes a
clipped GeoJSON in one pass.

Strategy
--------
``ogr2ogr`` with ``-spat`` (a fast bbox pre-filter) plus
``-clipsrc data/ne_england.geojson`` (an exact polygon clip). The bbox
filter discards features whose envelope falls outside the NE bbox
without touching their geometry, which dramatically reduces work for
the polygon clip stage. Source CRS is already EPSG:4326 so no reproject
is needed.

If ``ogr2ogr`` is missing from PATH (it ships with the GDAL bundled in
the uv env) this module raises ``RuntimeError`` — there is no pure
Python fallback because streaming a 2.6 GB GeoJSON in fiona is
substantially slower and not worth maintaining as a parallel path.

CLI
---
``uv run python -m etl.sources.flood`` runs the clip end-to-end and
writes both the GeoJSON and its sidecar manifest.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path

from etl.config import DATA_PROCESSED, NE_BBOX, NE_POLYGON_PATH

logger = logging.getLogger(__name__)

CONSTRAINTS_DIR = DATA_PROCESSED / "constraints"
FLOOD_OUT_NAME = "flood_zones.geojson"
FLOOD_MANIFEST_NAME = "flood_zones.manifest.json"

# The user pre-downloads the source to ~/Downloads/flood-risk-zone.geojson.
SOURCE_PATH = Path.home() / "Downloads" / "flood-risk-zone.geojson"
SOURCE_URL = "https://www.planning.data.gov.uk/dataset/flood-risk-zone"
LICENSE = "OGL 3.0"


def _run_ogr2ogr(
    src: Path, dst: Path, clip_polygon: Path, bbox: tuple[float, float, float, float]
) -> None:
    """Stream-clip ``src`` to ``dst`` using ogr2ogr with bbox + polygon clip."""
    if shutil.which("ogr2ogr") is None:
        raise RuntimeError("ogr2ogr not found on PATH. Install GDAL or run inside the uv env.")

    if dst.exists():
        dst.unlink()

    cmd = [
        "ogr2ogr",
        "-f",
        "GeoJSON",
        "-clipsrc",
        str(clip_polygon),
        "-spat",
        str(bbox[0]),
        str(bbox[1]),
        str(bbox[2]),
        str(bbox[3]),
        # Skip features that fail the clip rather than aborting the whole job —
        # the upstream dataset has a few self-intersecting polygons.
        "-skipfailures",
        str(dst),
        str(src),
    ]
    logger.info("Running: %s", " ".join(cmd))
    start = time.monotonic()
    # ogr2ogr can take 5-15 minutes on a 2.6 GB source; let it run. The argv is
    # built from constants + repo-controlled paths, so the S603 warning about
    # "untrusted input" doesn't apply here.
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    elapsed = time.monotonic() - start
    if proc.returncode != 0:
        logger.error("ogr2ogr stdout: %s", proc.stdout)
        logger.error("ogr2ogr stderr: %s", proc.stderr)
        raise RuntimeError(f"ogr2ogr failed with exit code {proc.returncode} after {elapsed:.1f}s")
    logger.info("ogr2ogr completed in %.1fs", elapsed)


def _summarise_output(out_path: Path) -> dict:
    """Read the clipped GeoJSON and return summary stats for the manifest."""
    with out_path.open() as fh:
        payload = json.load(fh)
    features = payload.get("features", [])
    feature_count = len(features)

    zone_counts: Counter[str] = Counter()
    geom_types: set[str] = set()
    for feat in features:
        props = feat.get("properties") or {}
        level = props.get("flood-risk-level")
        if level is not None:
            zone_counts[str(level)] += 1
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype:
            geom_types.add(gtype)

    if not geom_types:
        geom_summary = "Unknown"
    elif len(geom_types) == 1:
        geom_summary = next(iter(geom_types))
    else:
        geom_summary = "Mixed:" + ",".join(sorted(geom_types))

    return {
        "feature_count": feature_count,
        "zone_breakdown": dict(zone_counts),
        "geometry_type": geom_summary,
    }


def _write_manifest(manifest_path: Path, *, summary: dict, file_size: int) -> None:
    payload = {
        "name": "Flood Risk Zones (Levels 2 & 3)",
        "source_url": SOURCE_URL,
        "source_file": str(SOURCE_PATH),
        "license": LICENSE,
        "last_updated": "2023-08 (per feature entry-date)",
        "feature_count": summary["feature_count"],
        "zone_breakdown": summary["zone_breakdown"],
        "file_size_bytes": file_size,
        "geometry_type": summary["geometry_type"],
        "notes": (
            "Clipped from national 2.58GB dataset using ogr2ogr -clipsrc data/ne_england.geojson"
        ),
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as fh:
        json.dump(payload, fh, indent=2)
    logger.info("Wrote manifest %s", manifest_path)


def clip_flood_zones() -> Path:
    """Clip the national flood-risk-zone GeoJSON to the NE England polygon.

    Reads ``~/Downloads/flood-risk-zone.geojson``, streams it through
    ogr2ogr with a NE bbox pre-filter and an exact polygon clip against
    ``data/ne_england.geojson``, and writes the result plus its sidecar
    manifest under ``data/processed/constraints/``.

    Returns the path to the clipped GeoJSON.
    """
    if not SOURCE_PATH.exists():
        raise FileNotFoundError(
            f"Flood source not found at {SOURCE_PATH}. Download it manually from "
            f"{SOURCE_URL} (~2.6 GB) and re-run."
        )
    if not NE_POLYGON_PATH.exists():
        raise FileNotFoundError(f"NE polygon not found at {NE_POLYGON_PATH}; run `polygon` first.")

    CONSTRAINTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CONSTRAINTS_DIR / FLOOD_OUT_NAME
    manifest_path = CONSTRAINTS_DIR / FLOOD_MANIFEST_NAME

    src_size = SOURCE_PATH.stat().st_size
    logger.info("Clipping %s (%.2f GB) -> %s", SOURCE_PATH, src_size / 1e9, out_path)

    _run_ogr2ogr(SOURCE_PATH, out_path, NE_POLYGON_PATH, NE_BBOX)

    file_size = out_path.stat().st_size
    summary = _summarise_output(out_path)
    logger.info(
        "Flood: %d features, zones=%s, %.2f MB output",
        summary["feature_count"],
        summary["zone_breakdown"],
        file_size / 1e6,
    )

    _write_manifest(manifest_path, summary=summary, file_size=file_size)
    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    clip_flood_zones()
