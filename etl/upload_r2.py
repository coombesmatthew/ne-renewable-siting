"""Upload PMTiles archives to Cloudflare R2 and apply CORS for browser fetches.

Usage:
    uv run python -m etl.upload_r2
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from dotenv import load_dotenv

from etl.config import DATA_PMTILES, PROJECT_ROOT

logger = logging.getLogger(__name__)

CONSTRAINT_LAYERS = [
    "green_belt",
    "national_landscape",
    "national_park",
    "sssi",
    "listed_building",
    "scheduled_monument",
    "flood_zones",
]

TILE_URLS_PATH = PROJECT_ROOT / "frontend" / "public" / "tile_urls.json"


def _client_and_bucket() -> tuple[Any, str, str]:
    """Build a boto3 S3 client targeting R2 plus the bucket + public base URL."""
    load_dotenv()
    account_id = os.environ["R2_ACCOUNT_ID"]
    access_key = os.environ["R2_ACCESS_KEY_ID"]
    secret = os.environ["R2_SECRET_ACCESS_KEY"]
    bucket = os.environ["R2_BUCKET_NAME"]
    public_base = os.environ["R2_PUBLIC_URL"].rstrip("/")

    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret,
        region_name="auto",
    )
    return s3, bucket, public_base


def _apply_cors(s3, bucket: str) -> None:
    """Apply a permissive CORS policy so PMTiles can be fetched from any origin."""
    cors_policy = {
        "CORSRules": [
            {
                "AllowedOrigins": ["*"],
                "AllowedMethods": ["GET", "HEAD"],
                "AllowedHeaders": [
                    "Range",
                    "Content-Type",
                    "If-Match",
                    "If-None-Match",
                ],
                "ExposeHeaders": [
                    "Content-Range",
                    "Content-Length",
                    "Accept-Ranges",
                    "ETag",
                ],
                "MaxAgeSeconds": 3600,
            }
        ]
    }
    s3.put_bucket_cors(Bucket=bucket, CORSConfiguration=cors_policy)
    logger.info("CORS policy applied to bucket %s", bucket)


def upload_pmtiles_to_r2() -> dict[str, str]:
    """Upload every PMTiles file in data/pmtiles/ to R2. Returns {layer: public_url}."""
    s3, bucket, public_base = _client_and_bucket()
    try:
        _apply_cors(s3, bucket)
    except Exception as exc:  # noqa: BLE001 - tolerate insufficient token scope
        logger.warning("CORS apply failed (likely missing token scope): %s", exc)

    if not DATA_PMTILES.exists():
        raise FileNotFoundError(f"PMTiles directory missing: {DATA_PMTILES}")

    urls: dict[str, str] = {}
    for path in sorted(DATA_PMTILES.glob("*.pmtiles")):
        key = path.name
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": "application/vnd.pmtiles",
                "CacheControl": "public, max-age=86400",
            },
        )
        public_url = f"{public_base}/{key}"
        urls[path.stem] = public_url
        logger.info("uploaded %s (%s bytes) -> %s", key, f"{path.stat().st_size:,}", public_url)

    return urls


def write_tile_urls_json(urls: dict[str, str], public_base: str) -> Path:
    """Write the frontend manifest used by MapLibre to load layers."""
    payload = {
        "base": public_base,
        "tiles": urls,
        "constraint_layers": CONSTRAINT_LAYERS,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    TILE_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    TILE_URLS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    logger.info("wrote %s", TILE_URLS_PATH)
    return TILE_URLS_PATH


def list_uploaded(s3, bucket: str) -> list[tuple[str, int]]:
    """Return [(key, size)] for all objects in the bucket (debug helper)."""
    resp = s3.list_objects_v2(Bucket=bucket)
    return [(o["Key"], o["Size"]) for o in resp.get("Contents", [])]


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()
    public_base = os.environ["R2_PUBLIC_URL"].rstrip("/")
    urls = upload_pmtiles_to_r2()
    write_tile_urls_json(urls, public_base)


if __name__ == "__main__":
    _main()
