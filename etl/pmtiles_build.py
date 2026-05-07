"""Build PMTiles for vector and raster layers.

Vector layers (parcels, substations, REPD, NE polygon, constraints) are built
with tippecanoe. Raster layers (solar, wind) are colorized to RGB and converted
with `rio pmtiles write`.

Usage:
    uv run python -m etl.pmtiles_build
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import rasterio

from etl.config import DATA_PMTILES, DATA_PROCESSED, DATA_RAW, NE_POLYGON_PATH, PROJECT_ROOT

logger = logging.getLogger(__name__)

TIPPECANOE = "tippecanoe"
PMTILES_BIN = "pmtiles"

CONSTRAINT_DIR = DATA_PROCESSED / "constraints"

# (layer_name, source_path, [tippecanoe args])
_VECTOR_JOBS: list[tuple[str, Path, list[str]]] = [
    (
        "parcels",
        DATA_PROCESSED / "parcels_attributed.geojson",
        [
            "-Z",
            "8",
            "-z",
            "14",
            "-l",
            "parcels",
            "--drop-densest-as-needed",
            "--extend-zooms-if-still-dropping",
            "--simplification",
            "4",
            "--no-tile-stats",
        ],
    ),
    # Substations are now built via _build_substations() (two source-layers:
    # catchment polygon + point marker) — see below. Removed from this job list.
    (
        "repd",
        DATA_PROCESSED / "repd.geojson",
        [
            "-Z",
            "7",
            "-z",
            "14",
            "-l",
            "repd",
            "--cluster-distance=0",
            "--no-tile-stats",
        ],
    ),
    (
        "ne_polygon",
        NE_POLYGON_PATH,
        [
            "-Z",
            "5",
            "-z",
            "12",
            "-l",
            "ne_polygon",
        ],
    ),
]

# Constraint sub-layers combined into a single multi-layer PMTiles.
_CONSTRAINT_LAYERS: list[tuple[str, Path]] = [
    ("green_belt", CONSTRAINT_DIR / "green-belt.geojson"),
    ("national_landscape", CONSTRAINT_DIR / "national-landscape.geojson"),
    ("national_park", CONSTRAINT_DIR / "national-park.geojson"),
    ("sssi", CONSTRAINT_DIR / "site-of-special-scientific-interest.geojson"),
    ("listed_building", CONSTRAINT_DIR / "listed-building.geojson"),
    ("scheduled_monument", CONSTRAINT_DIR / "scheduled-monument.geojson"),
    ("flood_zones", CONSTRAINT_DIR / "flood_zones.geojson"),
]


def _run_tippecanoe(args: list[str]) -> None:
    """Run tippecanoe and stream any warnings to the logger."""
    logger.info("tippecanoe %s", " ".join(args))
    proc = subprocess.run(  # noqa: S603
        [TIPPECANOE, "--force", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    # tippecanoe writes progress to stderr; only log non-empty
    if proc.stderr.strip():
        for line in proc.stderr.strip().splitlines():
            logger.info("  %s", line)
    if proc.returncode != 0:
        raise RuntimeError(f"tippecanoe failed (exit {proc.returncode}): {proc.stderr}")


def _verify_pmtiles(path: Path) -> None:
    """Run `pmtiles verify` to check archive integrity."""
    logger.info("verify %s", path.name)
    proc = subprocess.run(  # noqa: S603
        [PMTILES_BIN, "verify", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    out = (proc.stdout + proc.stderr).strip()
    if out:
        for line in out.splitlines():
            logger.info("  %s", line)
    if proc.returncode != 0:
        raise RuntimeError(f"pmtiles verify failed for {path}: {out}")


def _build_vector_layer(name: str, source: Path, args: list[str]) -> Path:
    out = DATA_PMTILES / f"{name}.pmtiles"
    if not source.exists():
        raise FileNotFoundError(f"vector source missing: {source}")
    _run_tippecanoe(["-o", str(out), *args, str(source)])
    _verify_pmtiles(out)
    return out


def _build_constraints() -> Path:
    out = DATA_PMTILES / "constraints.pmtiles"
    args = [
        "-o",
        str(out),
        "-Z",
        "8",
        "-z",
        "14",
        "--drop-densest-as-needed",
        "--extend-zooms-if-still-dropping",
        "--simplification",
        "4",
        "--no-tile-stats",
    ]
    for layer_name, src in _CONSTRAINT_LAYERS:
        if not src.exists():
            raise FileNotFoundError(f"constraint source missing: {src}")
        args.extend(["-L", f"{layer_name}:{src}"])
    _run_tippecanoe(args)
    _verify_pmtiles(out)
    return out


def _build_substations() -> Path:
    """Build substations.pmtiles with two source-layers: catchment + point.

    Each feature carries a `voltage_tier` attribute so the frontend can render
    5 separate colour-coded layer pairs (one per voltage class).
    """
    out = DATA_PMTILES / "substations.pmtiles"
    catchment_src = DATA_PROCESSED / "npg_headroom.geojson"
    point_src = DATA_PROCESSED / "npg_substations_points.geojson"
    if not catchment_src.exists():
        raise FileNotFoundError(f"substations catchment source missing: {catchment_src}")
    if not point_src.exists():
        raise FileNotFoundError(f"substations point source missing: {point_src}")
    args = [
        "-o",
        str(out),
        "-Z",
        "5",
        "-z",
        "14",
        # Preserve every substation at every zoom (we only have 681 points,
        # tippecanoe's default density-drop would silently lose most of them
        # at low zooms — make all of them visible from regional zoom 5+).
        "-r1",
        "--no-feature-limit",
        "--no-tile-size-limit",
        "--no-tile-stats",
        "-L",
        f"substation_catchment:{catchment_src}",
        "-L",
        f"substation_point:{point_src}",
    ]
    _run_tippecanoe(args)
    _verify_pmtiles(out)
    return out


# Hand-rolled colormaps as numpy arrays (256 stops, RGB uint8).
# Simple two-stop gradients keep the visual readable without pulling in
# matplotlib as a runtime dep.
def _gradient(stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    """Build a (256, 3) uint8 lookup table from sorted (position, rgb) stops."""
    lut = np.zeros((256, 3), dtype="uint8")
    xs = np.linspace(0.0, 1.0, 256)
    for i, x in enumerate(xs):
        # find bracketing stops
        for j in range(len(stops) - 1):
            x0, c0 = stops[j]
            x1, c1 = stops[j + 1]
            if x0 <= x <= x1:
                t = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
                lut[i] = [int(c0[k] + t * (c1[k] - c0[k])) for k in range(3)]
                break
    return lut


# Inferno-ish for solar (dark purple -> orange -> yellow)
_INFERNO_LUT = _gradient(
    [
        (0.0, (0, 0, 4)),
        (0.25, (87, 16, 110)),
        (0.5, (188, 55, 84)),
        (0.75, (249, 142, 9)),
        (1.0, (252, 255, 164)),
    ]
)
# Viridis-ish for wind (deep purple -> teal -> yellow-green)
_VIRIDIS_LUT = _gradient(
    [
        (0.0, (68, 1, 84)),
        (0.25, (59, 82, 139)),
        (0.5, (33, 145, 140)),
        (0.75, (94, 201, 98)),
        (1.0, (253, 231, 37)),
    ]
)
_LUTS = {"inferno": _INFERNO_LUT, "viridis": _VIRIDIS_LUT}


def _colorize_to_rgb(src_tif: Path, dst_tif: Path, cmap_name: str = "viridis") -> None:
    """Read a single-band float raster and write a 3-band uint8 RGB GeoTIFF.

    Linear stretch on finite values; NaN/nodata pixels become black.
    """
    with rasterio.open(src_tif) as src:
        data = src.read(1).astype("float32")
        profile = src.profile.copy()
        nodata = src.nodata

    mask = np.isfinite(data)
    if nodata is not None and np.isfinite(nodata):
        mask &= data != nodata
    if mask.any():
        valid = data[mask]
        vmin, vmax = float(np.percentile(valid, 1)), float(np.percentile(valid, 99))
        if vmax <= vmin:
            vmax = vmin + 1e-6
    else:
        vmin, vmax = 0.0, 1.0

    norm = np.zeros_like(data, dtype="float32")
    norm[mask] = np.clip((data[mask] - vmin) / (vmax - vmin), 0.0, 1.0)

    lut = _LUTS.get(cmap_name, _VIRIDIS_LUT)
    idx = (norm * 255).astype("uint8")
    rgb = lut[idx]  # H, W, 3
    rgb[~mask] = 0

    profile.update(
        count=3,
        dtype="uint8",
        nodata=None,
        compress="deflate",
        photometric="RGB",
    )
    with rasterio.open(dst_tif, "w", **profile) as dst:
        for i in range(3):
            dst.write(rgb[..., i], i + 1)


def _build_raster_layer(
    name: str,
    src_tif: Path,
    cmap: str,
    zoom_min: int = 5,
    zoom_max: int = 12,
) -> Path:
    if not src_tif.exists():
        raise FileNotFoundError(f"raster source missing: {src_tif}")

    out = DATA_PMTILES / f"{name}.pmtiles"
    with tempfile.TemporaryDirectory(prefix=f"rio_{name}_") as tmp:
        rgb_tif = Path(tmp) / f"{name}_rgb.tif"
        _colorize_to_rgb(src_tif, rgb_tif, cmap_name=cmap)

        args = [
            "uv",
            "run",
            "rio",
            "pmtiles",
            str(rgb_tif),
            str(out),
            "--zoom-levels",
            f"{zoom_min}..{zoom_max}",
            "--format",
            "PNG",
            "--silent",
            "--name",
            name,
        ]
        logger.info("rio pmtiles %s -> %s (zoom %d..%d)", name, out.name, zoom_min, zoom_max)
        # Force-overwrite by removing first; rio pmtiles errors if file exists.
        if out.exists():
            out.unlink()
        proc = subprocess.run(  # noqa: S603
            args,
            check=False,
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        if proc.stdout.strip():
            for line in proc.stdout.strip().splitlines():
                logger.info("  %s", line)
        if proc.stderr.strip():
            for line in proc.stderr.strip().splitlines():
                logger.info("  %s", line)
        if proc.returncode != 0:
            raise RuntimeError(f"rio pmtiles failed for {name}: {proc.stderr}")

    _verify_pmtiles(out)
    return out


def build_all_pmtiles() -> dict[str, Path]:
    """Build every PMTiles archive under data/pmtiles/. Returns {name: path}."""
    if shutil.which(TIPPECANOE) is None:
        raise RuntimeError("tippecanoe not found on PATH")
    if shutil.which(PMTILES_BIN) is None:
        raise RuntimeError("pmtiles CLI not found on PATH")

    DATA_PMTILES.mkdir(parents=True, exist_ok=True)

    outputs: dict[str, Path] = {}

    for name, source, args in _VECTOR_JOBS:
        outputs[name] = _build_vector_layer(name, source, args)
        logger.info(
            "%s -> %s (%s bytes)", name, outputs[name].name, f"{outputs[name].stat().st_size:,}"
        )

    outputs["constraints"] = _build_constraints()
    logger.info(
        "constraints -> %s (%s bytes)",
        outputs["constraints"].name,
        f"{outputs['constraints'].stat().st_size:,}",
    )

    outputs["substations"] = _build_substations()
    logger.info(
        "substations -> %s (%s bytes)",
        outputs["substations"].name,
        f"{outputs['substations'].stat().st_size:,}",
    )

    outputs["solar"] = _build_raster_layer("solar", DATA_RAW / "solar_pvout.tif", cmap="inferno")
    logger.info(
        "solar -> %s (%s bytes)", outputs["solar"].name, f"{outputs['solar'].stat().st_size:,}"
    )

    outputs["wind"] = _build_raster_layer("wind", DATA_RAW / "wind_speed_100m.tif", cmap="viridis")
    logger.info(
        "wind -> %s (%s bytes)", outputs["wind"].name, f"{outputs['wind'].stat().st_size:,}"
    )

    return outputs


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = build_all_pmtiles()
    total = sum(p.stat().st_size for p in outputs.values())
    logger.info("---")
    logger.info("built %d PMTiles archives (%s bytes total)", len(outputs), f"{total:,}")
    for name in sorted(outputs):
        p = outputs[name]
        logger.info("  %-15s %12s  %s", name, f"{p.stat().st_size:,}", p.name)


if __name__ == "__main__":
    _main()
