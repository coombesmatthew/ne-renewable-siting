"""ETL orchestrator entrypoint.

Usage:
    python -m etl.run_all <subcommand>

Subcommands:
    polygon   Build NE England bounding polygon from ONS LADs.
    vector    Run all vector ETL sources (NPg, REPD, constraints, flood).
    raster    Run raster ETL (Solar Atlas, Wind Atlas).
    manifest  Aggregate data manifest from processed outputs.
    score     Build hex grid + per-tech site scores.
    pmtiles   Generate PMTiles (vector + raster).
    upload    Upload PMTiles to Cloudflare R2.
    all       Run the full pipeline end-to-end.
"""

from __future__ import annotations

import argparse
import logging
import sys

logger = logging.getLogger(__name__)

SUBCOMMANDS = ("polygon", "vector", "raster", "manifest", "score", "pmtiles", "upload", "all")


def _run_polygon() -> None:
    from etl.ne_polygon import build_ne_polygon

    out_path = build_ne_polygon()
    logger.info("polygon: wrote %s", out_path)


def _run_vector() -> None:
    from etl.sources.constraints import download_planning_constraints
    from etl.sources.flood import clip_flood_zones
    from etl.sources.npg import download_ecr, download_headroom
    from etl.sources.repd import download_repd

    headroom_path = download_headroom()
    logger.info("vector: NPg headroom -> %s", headroom_path)

    ecr_path = download_ecr()
    logger.info("vector: NPg ECR -> %s", ecr_path)

    repd_path = download_repd()
    logger.info("vector: REPD -> %s", repd_path)

    constraint_paths = download_planning_constraints()
    for slug, path in constraint_paths.items():
        logger.info("vector: constraint %s -> %s", slug, path)

    flood_path = clip_flood_zones()
    logger.info("vector: flood zones -> %s", flood_path)


def _run_raster() -> None:
    from etl.sources.solar import download_and_clip_solar
    from etl.sources.wind import clip_wind

    solar_path = download_and_clip_solar()
    logger.info("raster: solar -> %s", solar_path)

    wind_path = clip_wind()
    logger.info("raster: wind -> %s", wind_path)


def _run_manifest() -> None:
    from etl.manifest import build_manifest

    manifest_path = build_manifest()
    logger.info("manifest: wrote %s", manifest_path)


def _run_all() -> None:
    """Run the Chunk 1 pipeline: polygon -> vector -> raster -> manifest."""
    _run_polygon()
    _run_vector()
    _run_raster()
    _run_manifest()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="etl.run_all",
        description="ETL orchestrator for the NE renewable siting demo.",
    )
    parser.add_argument(
        "subcommand",
        choices=SUBCOMMANDS,
        help="Which stage of the pipeline to run.",
    )
    args = parser.parse_args(argv)

    if args.subcommand == "polygon":
        _run_polygon()
        return 0
    if args.subcommand == "vector":
        _run_vector()
        return 0
    if args.subcommand == "raster":
        _run_raster()
        return 0
    if args.subcommand == "manifest":
        _run_manifest()
        return 0
    if args.subcommand == "all":
        _run_all()
        return 0
    if args.subcommand in ("score", "pmtiles", "upload"):
        logger.info("TODO: Chunk 2 — '%s' not yet implemented", args.subcommand)
        return 0

    # argparse would have rejected anything else, but be defensive.
    logger.error("Unknown subcommand: %s", args.subcommand)
    return 2


if __name__ == "__main__":
    sys.exit(main())
