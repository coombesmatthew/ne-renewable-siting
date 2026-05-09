"""HM Land Registry CCOD (Commercial / Corporate Ownership Data) ETL.

Filters the national CCOD CSV (~4.4M rows, 1.5GB) down to UK-company-owned
properties in NE England, geocodes by postcode via postcodes.io, jitters
coincident points, and writes a GeoJSON + manifest sidecar.

Source CSV is expected at ``~/Downloads/CCOD_FULL_2026_05.csv`` (the user's
manual download from the HMLR public dataset). Pipeline:

1. **Filter** — chunked CSV scan, keep rows where ``County`` matches one of
   the 8 NE counties or ``Postcode`` looks NE-ish (NE/DH/TS/SR or DL1-3).
   Cached at ``data/raw/ccod_ne_raw.csv`` for fast re-runs.
2. **Geocode** — bulk POST to ``api.postcodes.io/postcodes`` (100 at a time,
   no auth). Cached to ``data/raw/postcode_geocode.json``.
3. **Jitter + GeoJSON** — convert lat/lon to BNG, apply seeded ±30m jitter
   so co-postcoded records don't perfectly stack, convert back, write to
   ``data/processed/ccod_ne.geojson`` via pyogrio.
4. **Manifest** — sidecar JSON with feature counts, top proprietors, etc.

Quirks / fallbacks:
    * CSV is utf-8 with embedded commas in quoted address fields — use
      pandas default csv parser which handles this fine.
    * Some postcodes are blank or malformed; drop these silently in step 1
      unless the County match alone is enough to keep them. Records that
      survive step 1 but lack a usable postcode are dropped in step 3
      (counted in ``unmatched_postcodes``).
    * Edge-case rows have NE-area postcodes (NE/DH/TS/SR/DL1-3) but a
      non-NE County (e.g. an out-of-region freeholder with an NE PO box).
      We keep these — the postcode geocode pins them in NE.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
from shapely.geometry import Point

from etl.config import DATA_PROCESSED, DATA_RAW, TARGET_CRS

logger = logging.getLogger(__name__)

CCOD_CSV_PATH = Path("~/Downloads/CCOD_FULL_2026_05.csv").expanduser()
RAW_FILTERED_NAME = "ccod_ne_raw.csv"
GEOCODE_CACHE_NAME = "postcode_geocode.json"
OUT_NAME = "ccod_ne.geojson"
MANIFEST_NAME = "ccod_ne.manifest.json"

POSTCODES_IO_URL = "https://api.postcodes.io/postcodes"
POSTCODES_IO_BATCH = 100
POSTCODES_IO_TIMEOUT_S = 30

JITTER_METRES = 30.0
RANDOM_SEED = 42

NE_COUNTIES = {
    "TYNE AND WEAR",
    "COUNTY DURHAM",
    "NORTHUMBERLAND",
    "STOCKTON-ON-TEES",
    "MIDDLESBROUGH",
    "REDCAR AND CLEVELAND",
    "DARLINGTON",
    "HARTLEPOOL",
}

# Renamed (snake_case) output columns; the source CSV uses Title Case with
# spaces and parens. _SOURCE_TO_OUT maps source -> output column name.
_SOURCE_TO_OUT: dict[str, str] = {
    "Title Number": "title_number",
    "Tenure": "tenure",
    "Property Address": "property_address",
    "District": "district",
    "County": "county",
    "Postcode": "postcode",
    "Proprietor Name (1)": "proprietor_name_1",
    "Company Registration No. (1)": "company_registration_no_1",
    "Proprietorship Category (1)": "proprietorship_category_1",
    "Date Proprietor Added": "date_proprietor_added",
}
# Concatenated proprietor-1 address (3 source fields).
_PROP1_ADDR_COLS = [
    "Proprietor (1) Address (1)",
    "Proprietor (1) Address (2)",
    "Proprietor (1) Address (3)",
]
# Additional proprietors 2-4 (name + category each).
_ADDITIONAL_PROP_PAIRS = [
    ("Proprietor Name (2)", "Proprietorship Category (2)"),
    ("Proprietor Name (3)", "Proprietorship Category (3)"),
    ("Proprietor Name (4)", "Proprietorship Category (4)"),
]


def _is_ne_postcode(pc: str | None) -> bool:
    """True if the postcode looks like it falls in NE England."""
    if not pc:
        return False
    pc = str(pc).strip().upper()
    if not pc:
        return False
    if pc.startswith(("NE", "DH", "TS", "SR")):
        return True
    m = re.match(r"^DL(\d+)", pc)
    return bool(m and int(m.group(1)) in (1, 2, 3))


def _normalise_postcode(pc: str | None) -> str | None:
    """Uppercase + collapse whitespace; return None for empty."""
    if pc is None:
        return None
    s = str(pc).strip().upper()
    if not s:
        return None
    # Collapse internal whitespace to a single space.
    s = re.sub(r"\s+", " ", s)
    return s


def _filter_ccod_to_ne(src: Path, out_path: Path) -> pd.DataFrame:
    """Stream the CCOD CSV in chunks and keep NE rows. Cache to ``out_path``."""
    if out_path.exists() and out_path.stat().st_size > 0:
        logger.info("Using cached filtered CCOD: %s", out_path)
        return pd.read_csv(out_path, dtype=str, low_memory=False)

    if not src.exists():
        raise FileNotFoundError(f"CCOD CSV missing: {src}")

    logger.info("Streaming %s in 50K chunks…", src)
    keep_chunks: list[pd.DataFrame] = []
    total_rows = 0
    columns: list[str] = []
    chunk_iter = pd.read_csv(src, chunksize=50_000, dtype=str, low_memory=False)
    for i, chunk in enumerate(chunk_iter):
        if not columns:
            columns = list(chunk.columns)
        total_rows += len(chunk)
        county = chunk["County"].fillna("").astype(str).str.strip().str.upper()
        county_mask = county.isin(NE_COUNTIES)
        pc_mask = chunk["Postcode"].fillna("").map(_is_ne_postcode)
        keep = chunk[county_mask | pc_mask]
        if len(keep):
            keep_chunks.append(keep)
        if (i + 1) % 10 == 0:
            logger.info(
                "  chunk %d: scanned=%d kept=%d (running)",
                i + 1,
                total_rows,
                sum(len(c) for c in keep_chunks),
            )

    df = pd.concat(keep_chunks, ignore_index=True) if keep_chunks else pd.DataFrame(columns=columns)
    logger.info("Filter result: scanned=%d kept=%d", total_rows, len(df))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    logger.info("Cached filtered rows -> %s", out_path)
    # Stash the scanned-row count on the dataframe via attrs for downstream stats.
    df.attrs["total_scanned"] = total_rows
    return df


def _load_geocode_cache(cache_path: Path) -> dict[str, dict | None]:
    """Load the postcode -> {lon,lat}|null cache; return {} if absent."""
    if cache_path.exists() and cache_path.stat().st_size > 0:
        try:
            with cache_path.open("r") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read geocode cache %s: %s", cache_path, exc)
    return {}


def _save_geocode_cache(cache_path: Path, cache: dict[str, dict | None]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".part")
    with tmp.open("w") as fh:
        json.dump(cache, fh)
    tmp.replace(cache_path)


def _geocode_postcodes(postcodes: list[str], cache_path: Path) -> dict[str, dict | None]:
    """Resolve a list of postcodes to {lon, lat} via postcodes.io (cached)."""
    cache = _load_geocode_cache(cache_path)
    pending = [pc for pc in postcodes if pc not in cache]
    if not pending:
        logger.info("Geocode cache hit for all %d postcodes", len(postcodes))
        return cache

    logger.info(
        "Geocoding %d new postcodes (%d already cached) in batches of %d",
        len(pending),
        len(cache),
        POSTCODES_IO_BATCH,
    )

    new_count = 0
    batch_count = 0
    for i in range(0, len(pending), POSTCODES_IO_BATCH):
        batch = pending[i : i + POSTCODES_IO_BATCH]
        try:
            resp = requests.post(
                POSTCODES_IO_URL,
                json={"postcodes": batch},
                timeout=POSTCODES_IO_TIMEOUT_S,
            )
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Bulk geocode batch failed (%s); marking as null", exc)
            for pc in batch:
                cache.setdefault(pc, None)
            continue

        for entry in payload.get("result", []) or []:
            queried = entry.get("query")
            result = entry.get("result")
            if queried is None:
                continue
            if isinstance(result, dict):
                lon = result.get("longitude")
                lat = result.get("latitude")
                if lon is not None and lat is not None:
                    cache[queried] = {"lon": float(lon), "lat": float(lat)}
                    new_count += 1
                else:
                    cache[queried] = None
            else:
                cache[queried] = None

        # Defensive: if API skipped any postcodes, mark them null so we
        # don't retry forever.
        for pc in batch:
            cache.setdefault(pc, None)

        batch_count += 1
        if batch_count % 25 == 0:
            logger.info(
                "  geocoded %d batches (%d/%d pending)",
                batch_count,
                min(i + POSTCODES_IO_BATCH, len(pending)),
                len(pending),
            )
            _save_geocode_cache(cache_path, cache)
        # Be polite — postcodes.io has no documented rate limit but spamming
        # is rude; ~25ms sleep keeps total wall time tiny.
        time.sleep(0.025)

    _save_geocode_cache(cache_path, cache)
    logger.info("Geocoded %d postcodes (cache now has %d entries)", new_count, len(cache))
    return cache


def _build_additional_proprietors(row: pd.Series) -> str:
    """Join proprietors 2-4 as 'name (cat); name (cat)'; empty string if none."""
    parts: list[str] = []
    for name_col, cat_col in _ADDITIONAL_PROP_PAIRS:
        name = row.get(name_col)
        if pd.isna(name) or not str(name).strip():
            continue
        cat = row.get(cat_col)
        cat_str = str(cat).strip() if cat is not None and not pd.isna(cat) else ""
        if cat_str:
            parts.append(f"{str(name).strip()} ({cat_str})")
        else:
            parts.append(str(name).strip())
    return "; ".join(parts)


def _build_prop1_address(row: pd.Series) -> str:
    """Concatenate the 3 sub-fields of Proprietor 1 Address with comma."""
    parts: list[str] = []
    for col in _PROP1_ADDR_COLS:
        val = row.get(col)
        if val is None or pd.isna(val):
            continue
        s = str(val).strip()
        if s:
            parts.append(s)
    return ", ".join(parts)


def _jitter_in_bng(gdf: gpd.GeoDataFrame, *, metres: float = JITTER_METRES) -> gpd.GeoDataFrame:
    """Apply a deterministic ±metres jitter to each point, in BNG, then back to 4326."""
    if gdf.empty:
        return gdf
    bng = gdf.to_crs("EPSG:27700")
    rng = np.random.default_rng(RANDOM_SEED)
    n = len(bng)
    dx = rng.uniform(-metres, metres, size=n)
    dy = rng.uniform(-metres, metres, size=n)
    new_geom = [
        Point(geom.x + dx_i, geom.y + dy_i)
        for geom, dx_i, dy_i in zip(bng.geometry, dx, dy, strict=True)
    ]
    bng = bng.set_geometry(new_geom, crs="EPSG:27700")
    return bng.to_crs(TARGET_CRS)


def build_ccod_ne() -> Path:
    """Run the full CCOD NE ETL. Returns the path to the processed GeoJSON."""
    DATA_RAW.mkdir(parents=True, exist_ok=True)
    DATA_PROCESSED.mkdir(parents=True, exist_ok=True)

    raw_path = DATA_RAW / RAW_FILTERED_NAME
    cache_path = DATA_RAW / GEOCODE_CACHE_NAME

    # --- Step A: filter --------------------------------------------------
    df = _filter_ccod_to_ne(CCOD_CSV_PATH, raw_path)
    total_filtered = len(df)
    if total_filtered == 0:
        raise RuntimeError("No NE rows survived the CCOD filter — aborting")

    # Normalise postcodes for geocoding.
    df["__postcode_norm"] = df["Postcode"].map(_normalise_postcode)

    # --- Step B: geocode -------------------------------------------------
    unique_pcs = sorted({pc for pc in df["__postcode_norm"].dropna() if pc})
    logger.info("Unique postcodes to geocode: %d", len(unique_pcs))
    geocode = _geocode_postcodes(unique_pcs, cache_path)

    matched = sum(1 for pc in unique_pcs if geocode.get(pc))
    logger.info(
        "Geocode matched: %d / %d unique postcodes (%.1f%%)",
        matched,
        len(unique_pcs),
        100.0 * matched / max(len(unique_pcs), 1),
    )

    # --- Step C: build GeoDataFrame --------------------------------------
    lons: list[float | None] = []
    lats: list[float | None] = []
    for pc in df["__postcode_norm"]:
        entry = geocode.get(pc) if pc else None
        if isinstance(entry, dict):
            lons.append(entry.get("lon"))
            lats.append(entry.get("lat"))
        else:
            lons.append(None)
            lats.append(None)
    df["__lon"] = lons
    df["__lat"] = lats

    has_geom = df["__lon"].notna() & df["__lat"].notna()
    unmatched_postcodes = int((~has_geom).sum())
    logger.info("Records with geocode: %d, unmatched: %d", int(has_geom.sum()), unmatched_postcodes)
    df = df[has_geom].copy()

    # Project / rename columns.
    out_records: list[dict] = []
    for _, row in df.iterrows():
        rec: dict = {}
        for src_col, out_col in _SOURCE_TO_OUT.items():
            val = row.get(src_col)
            if val is None or pd.isna(val):
                rec[out_col] = None
            else:
                s = str(val).strip()
                rec[out_col] = s if s else None
        rec["proprietor_address_1"] = _build_prop1_address(row)
        rec["additional_proprietors"] = _build_additional_proprietors(row)
        out_records.append(rec)

    out_df = pd.DataFrame(out_records)
    geometry = [Point(lon, lat) for lon, lat in zip(df["__lon"], df["__lat"], strict=True)]
    gdf = gpd.GeoDataFrame(out_df, geometry=geometry, crs=TARGET_CRS)

    # Jitter to break stacks of records sharing a postcode.
    logger.info("Applying ±%.0fm jitter (BNG round-trip, seed=%d)…", JITTER_METRES, RANDOM_SEED)
    gdf = _jitter_in_bng(gdf, metres=JITTER_METRES)

    # --- Step D: write GeoJSON via pyogrio ------------------------------
    out_path = DATA_PROCESSED / OUT_NAME
    if out_path.exists():
        out_path.unlink()
    gdf.to_file(out_path, driver="GeoJSON", engine="pyogrio")
    file_size = out_path.stat().st_size
    feature_count = len(gdf)
    logger.info("Wrote %s (%d features, %d bytes)", out_path, feature_count, file_size)

    # --- Step E: manifest ------------------------------------------------
    cat_counts = gdf["proprietorship_category_1"].fillna("(unknown)").value_counts().to_dict()
    top_proprietors = (
        gdf["proprietor_name_1"].fillna("(unknown)").value_counts().head(10).reset_index()
    )
    top_proprietors.columns = ["name", "count"]
    top_list = [
        {"name": str(r["name"]), "count": int(r["count"])} for _, r in top_proprietors.iterrows()
    ]
    success_rate = 100.0 * matched / len(unique_pcs) if unique_pcs else 0.0

    manifest = {
        "name": "CCOD — Commercial and Corporate Ownership (NE England)",
        "source": "HM Land Registry CCOD",
        "source_url": (
            "https://www.gov.uk/government/publications/"
            "hm-land-registry-corporate-and-commercial-ownership-data"
        ),
        "license": ("OGL 3.0 (free for non-commercial; £/yr for commercial use)"),
        "last_updated": "2026-05",
        "feature_count": int(feature_count),
        "total_filtered": int(total_filtered),
        "unmatched_postcodes": int(unmatched_postcodes),
        "geocode_success_rate": f"{success_rate:.1f}%",
        "unique_postcodes": int(len(unique_pcs)),
        "unique_postcodes_matched": int(matched),
        "file_size_bytes": int(file_size),
        "proprietorship_category_breakdown": cat_counts,
        "top_10_proprietors_by_record_count": top_list,
        "notes": (
            "Excludes individual (private) owners — only UK-registered companies. "
            "Points are jittered ±30m in BNG (seed=42) so co-postcoded records don't stack."
        ),
    }
    manifest_path = DATA_PROCESSED / MANIFEST_NAME
    with manifest_path.open("w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("Wrote manifest %s", manifest_path)

    return out_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build_ccod_ne()
