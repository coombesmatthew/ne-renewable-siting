"""Single source of truth for NE England LAD codes, bbox, paths, and CRS."""

from pathlib import Path

# Twelve North East England local authority district codes (ONS, Dec 2024).
NE_LAD_CODES: list[str] = [
    "E06000047",  # County Durham
    "E06000005",  # Darlington
    "E06000001",  # Hartlepool
    "E06000002",  # Middlesbrough
    "E06000004",  # Stockton-on-Tees
    "E06000003",  # Redcar and Cleveland
    "E06000057",  # Northumberland
    "E08000037",  # Gateshead
    "E08000021",  # Newcastle upon Tyne
    "E08000022",  # North Tyneside
    "E08000023",  # South Tyneside
    "E08000024",  # Sunderland
]

NE_LAD_NAMES: dict[str, str] = {
    "E06000047": "County Durham",
    "E06000005": "Darlington",
    "E06000001": "Hartlepool",
    "E06000002": "Middlesbrough",
    "E06000004": "Stockton-on-Tees",
    "E06000003": "Redcar and Cleveland",
    "E06000057": "Northumberland",
    "E08000037": "Gateshead",
    "E08000021": "Newcastle upon Tyne",
    "E08000022": "North Tyneside",
    "E08000023": "South Tyneside",
    "E08000024": "Sunderland",
}

# Rough WGS84 bbox covering NE England: minlon, minlat, maxlon, maxlat.
# Northumberland up north, Tees Valley down south.
NE_BBOX: tuple[float, float, float, float] = (-2.7, 54.4, -0.7, 55.9)

# Paths
PROJECT_ROOT: Path = Path(__file__).parent.parent
DATA_RAW: Path = PROJECT_ROOT / "data" / "raw"
DATA_PROCESSED: Path = PROJECT_ROOT / "data" / "processed"
DATA_PMTILES: Path = PROJECT_ROOT / "data" / "pmtiles"
NE_POLYGON_PATH: Path = PROJECT_ROOT / "data" / "ne_england.geojson"
MANIFEST_PATH: Path = PROJECT_ROOT / "data" / "data_manifest.json"

# Standard CRS for all processed outputs.
TARGET_CRS: str = "EPSG:4326"
