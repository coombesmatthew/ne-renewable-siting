"""Aggregate per-source ``*.manifest.json`` sidecars into a single data manifest.

Walks ``data/processed/`` (and ``data/processed/constraints/``) for all
``*.manifest.json`` files, augments each with the absolute path of the
corresponding output (GeoJSON or raster), and writes the consolidated payload
to ``data/data_manifest.json``.

Also synthesises entries for outputs that don't ship a sidecar
(``ne_england.geojson``, ``la_boundaries.geojson``) and records explicit
``status: "deferred"`` placeholders for layers we haven't been able to
ingest yet (wind raster, EA flood zones, EA hydropower).

The manifest is read by the frontend footer for attribution and "data last
updated" lines, and by the FastAPI backend to validate that all required
inputs are present at startup.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from etl.config import (
    DATA_PROCESSED,
    DATA_RAW,
    MANIFEST_PATH,
    NE_BBOX,
    NE_LAD_CODES,
    NE_POLYGON_PATH,
    PROJECT_ROOT,
)

logger = logging.getLogger(__name__)

# Fixed entries for outputs without sidecars or that can't yet be downloaded.
LA_BOUNDARIES_NAME = "la_boundaries.geojson"
LA_BOUNDARIES_SOURCE = "ONS LAD Dec 2024 BGC"
LA_BOUNDARIES_LICENSE = "OGL 3.0"

DEFERRED_LAYERS: dict[str, dict[str, str]] = {
    "wind_speed_100m": {
        "type": "raster",
        "status": "deferred",
        "reason": "Manual download required from globalwindatlas.info/download/gis-files",
    },
    "constraints/flood_zones": {
        "type": "vector",
        "status": "deferred",
        "reason": "ETL agent timed out — will retry in Chunk 2",
    },
    "constraints/hydropower": {
        "type": "vector",
        "status": "deferred",
        "reason": "EA dataset deprecated 2010 — to be retried with caveat in Chunk 2",
    },
}


def _count_features(geojson_path: Path) -> int:
    """Return the feature count of a GeoJSON FeatureCollection."""
    with geojson_path.open() as fh:
        payload = json.load(fh)
    feats = payload.get("features", [])
    return len(feats)


def _resolve_output_path(sidecar_path: Path, sidecar_payload: dict[str, Any]) -> Path:
    """Find the GeoJSON / raster file the sidecar describes.

    Priority:
      1. Explicit ``raster_path`` (relative to repo root) in the payload.
      2. Same directory as the sidecar, with ``.manifest.json`` -> ``.geojson``.
    """
    raster_rel = sidecar_payload.get("raster_path")
    if raster_rel:
        candidate = (PROJECT_ROOT / raster_rel).resolve()
        if not candidate.exists():
            raise FileNotFoundError(
                f"Manifest {sidecar_path} references missing raster {candidate}"
            )
        return candidate

    # Strip the ``.manifest.json`` suffix and look for ``.geojson`` next to it.
    stem = sidecar_path.name[: -len(".manifest.json")]
    candidate = sidecar_path.with_name(f"{stem}.geojson")
    if not candidate.exists():
        raise FileNotFoundError(
            f"Manifest {sidecar_path} has no neighbouring GeoJSON at {candidate}"
        )
    return candidate


def _layer_key(sidecar_path: Path) -> str:
    """Convert a sidecar path into a stable manifest layer key.

    Examples
    --------
    ``data/processed/npg_headroom.manifest.json``    -> ``"npg_headroom"``
    ``data/processed/solar.manifest.json``           -> ``"solar_pvout"``
    ``data/processed/constraints/green-belt.manifest.json``
        -> ``"constraints/green-belt"``
    """
    stem = sidecar_path.name[: -len(".manifest.json")]
    rel = sidecar_path.parent.relative_to(DATA_PROCESSED)
    if str(rel) == ".":
        # Solar's sidecar is named ``solar.manifest.json`` but its layer key
        # in the aggregated manifest reads ``solar_pvout`` (the canonical
        # raster name). Map it explicitly.
        if stem == "solar":
            return "solar_pvout"
        return stem
    return f"{rel.as_posix()}/{stem}"


def _build_entry(sidecar_path: Path) -> tuple[str, dict[str, Any]]:
    """Read a sidecar and return ``(layer_key, augmented_payload)``."""
    with sidecar_path.open() as fh:
        payload: dict[str, Any] = json.load(fh)

    output_path = _resolve_output_path(sidecar_path, payload)
    payload["status"] = "live"
    payload["output_path"] = str(output_path)
    payload["manifest_path"] = str(sidecar_path)
    # File size is sometimes stale (e.g. if a re-clip happened). Refresh it.
    payload["file_size_bytes"] = output_path.stat().st_size

    return _layer_key(sidecar_path), payload


def _build_ne_polygon_entry() -> dict[str, Any]:
    """Synthesise the manifest entry for the dissolved NE England polygon."""
    if not NE_POLYGON_PATH.exists():
        raise FileNotFoundError(f"NE polygon not found at {NE_POLYGON_PATH}; run `polygon` first")
    with NE_POLYGON_PATH.open() as fh:
        payload = json.load(fh)
    feature = payload["features"][0]
    props = feature.get("properties", {})
    return {
        "name": "NE England dissolved polygon (clip mask)",
        "status": "live",
        "type": "vector",
        "output_path": str(NE_POLYGON_PATH),
        "source": props.get("source", LA_BOUNDARIES_SOURCE),
        "license": props.get("license", LA_BOUNDARIES_LICENSE),
        "last_updated": props.get("last_updated"),
        "region": props.get("region", "North East England"),
        "lad_count": props.get("lad_count", len(NE_LAD_CODES)),
        "feature_count": len(payload.get("features", [])),
        "file_size_bytes": NE_POLYGON_PATH.stat().st_size,
        "geometry_type": feature["geometry"]["type"],
    }


def _build_la_boundaries_entry() -> dict[str, Any]:
    """Synthesise the manifest entry for the per-LAD boundaries GeoJSON."""
    la_path = DATA_PROCESSED / LA_BOUNDARIES_NAME
    if not la_path.exists():
        raise FileNotFoundError(f"LA boundaries not found at {la_path}; run `polygon` first")
    return {
        "name": "Local authority district boundaries (12 NE England LADs)",
        "status": "live",
        "type": "vector",
        "output_path": str(la_path),
        "source": LA_BOUNDARIES_SOURCE,
        "license": LA_BOUNDARIES_LICENSE,
        "last_updated": "2024-12",
        "feature_count": _count_features(la_path),
        "file_size_bytes": la_path.stat().st_size,
        "geometry_type": "MultiPolygon",
    }


def _ne_polygon_area_km2() -> float:
    """Compute the area of the dissolved NE polygon in km² (BNG)."""
    # Local imports keep ``etl.manifest`` cheap to import in non-ETL contexts.
    import geopandas as gpd

    gdf = gpd.read_file(NE_POLYGON_PATH)
    return float(gdf.to_crs("EPSG:27700").geometry.area.iloc[0] / 1_000_000)


def _walk_sidecars() -> list[Path]:
    """Return all ``*.manifest.json`` sidecars under ``data/processed/``.

    Excludes the aggregated ``data_manifest.json`` itself.
    """
    sidecars = sorted(DATA_PROCESSED.rglob("*.manifest.json"))
    return [p for p in sidecars if p.name != MANIFEST_PATH.name]


def build_manifest() -> Path:
    """Aggregate sidecars and synthesised entries into ``data_manifest.json``.

    Returns the path to the written manifest. Idempotent: calling repeatedly
    overwrites the file with the current state of ``data/processed/``.
    """
    if not DATA_PROCESSED.exists():
        raise FileNotFoundError(f"{DATA_PROCESSED} does not exist; run vector/raster ETL first")

    layers: dict[str, dict[str, Any]] = {}

    # 1. Synthesised entries for files without sidecars.
    layers["ne_england_polygon"] = _build_ne_polygon_entry()
    layers["la_boundaries"] = _build_la_boundaries_entry()

    # 2. Per-source sidecars.
    for sidecar in _walk_sidecars():
        key, entry = _build_entry(sidecar)
        if key in layers:
            logger.warning("Duplicate layer key %s — overwriting", key)
        layers[key] = entry

    # 3. Deferred layer placeholders.
    for key, payload in DEFERRED_LAYERS.items():
        if key in layers:
            logger.warning(
                "Layer %s is marked deferred but a live entry already exists; "
                "keeping the live entry",
                key,
            )
            continue
        layers[key] = dict(payload)

    # 4. Totals.
    live_layers = [v for v in layers.values() if v.get("status") == "live"]
    deferred_layers = [v for v in layers.values() if v.get("status") == "deferred"]
    total_features = sum(int(v.get("feature_count", 0) or 0) for v in live_layers)
    total_size = sum(int(v.get("file_size_bytes", 0) or 0) for v in live_layers)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "region": "North East England",
        "lad_count": len(NE_LAD_CODES),
        "lad_codes": list(NE_LAD_CODES),
        "bbox": list(NE_BBOX),
        "ne_polygon_area_km2": round(_ne_polygon_area_km2(), 1),
        "layers": layers,
        "totals": {
            "live_layers": len(live_layers),
            "deferred_layers": len(deferred_layers),
            "total_features": total_features,
            "total_size_bytes": total_size,
        },
    }

    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("w") as fh:
        json.dump(manifest, fh, indent=2)

    logger.info(
        "Wrote %s with %d live layers, %d deferred (%d total features, %d bytes)",
        MANIFEST_PATH,
        len(live_layers),
        len(deferred_layers),
        total_features,
        total_size,
    )
    # Note: ``DATA_RAW`` is referenced just so static analysers don't strip
    # the import — the canonical raster the backend samples lives there.
    _ = DATA_RAW
    return MANIFEST_PATH


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build_manifest()
