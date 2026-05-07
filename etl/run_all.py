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
        from etl.ne_polygon import build_ne_polygon

        out_path = build_ne_polygon()
        logger.info("polygon: wrote %s", out_path)
        return 0

    logger.info("TODO: implement '%s'", args.subcommand)
    return 0


if __name__ == "__main__":
    sys.exit(main())
