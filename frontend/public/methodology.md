# Methodology — North East renewable siting demo

This document describes how the per-parcel attributes attached in
`etl/attributes.py` (Wave 5C) are computed, why we chose to surface raw
measurements rather than a single composite "score", and what the known
limitations are.

## Why no synthetic score

A common temptation when building a renewable-siting tool is to fold every
input — solar, wind, distance to grid, designations, flooding — into a
single 0-100 number per parcel. We deliberately do **not** do that here:

* **Transparency.** Every weight in a composite score is a value judgement.
  Surfacing raw measurements (mean PVOUT, m/s wind, metres to substation,
  boolean overlaps) lets the reader form their own opinion of which parcels
  matter, and audit individual numbers against external sources.
* **Frontend filters.** The UI exposes range sliders and toggles directly
  over the raw fields (e.g. "wind ≥ 7 m/s and within 5 km of any-headroom
  substation, exclude green belt"). A pre-baked score wouldn't support that.
* **Editable thresholds.** Real siting decisions are sensitive to local
  policy (LPA stance on grade-3b agricultural land, how strict a developer
  is on slopes, etc.). The user gets to pick — we just hand them the inputs.

## Inputs

The canonical parcel layer is `data/processed/parcels.geojson` (33,969
INSPIRE Index Polygons ≥ 2 ha across the 12 NE England LADs, as built in
Wave 5A). Wave 5C reads this file, attaches the columns below, and writes
`data/processed/parcels_attributed.geojson` alongside it.

Geometries are repaired with `shapely.make_valid` before any spatial work.
GeometryCollections are reduced to their first polygonal part so
`rasterio.mask.mask` and `geopandas.sjoin` keep working downstream.

## Attributes

| Column | Source | Computation |
|---|---|---|
| `centroid_lon`, `centroid_lat` | `parcels.geojson` geometry | Centroid in EPSG:4326. |
| `mean_pvout_kwhkwp` | `data/raw/solar_pvout.tif` (Global Solar Atlas, kWh/kWp/yr, ~925 m pixels) | Mean of all in-polygon pixels (`rasterio.mask.mask`). Pixels with the float32-min sentinel (values < 1.0) are treated as nodata. Small parcels that miss every pixel centre fall back to `all_touched=True` so they still receive a representative value. |
| `mean_wind_speed_100m_ms` | `data/raw/wind_speed_100m.tif` (Global Wind Atlas v3 at 100 m, m/s) | Same masked-mean logic. Nodata is NaN. |
| `dist_substation_gen_headroom_m` | `data/processed/npg_headroom.geojson` | Euclidean distance (BNG / EPSG:27700) from the parcel centroid to the nearest NPg substation polygon centroid where `genhr > 0`. Computed via `scipy.spatial.cKDTree` on the BNG-projected substation centroids. |
| `dist_substation_any_headroom_m` | `data/processed/npg_headroom.geojson` | Same, but the substation set is filtered to those with **any** headroom (`genhr > 0` *or* `demhr > 0`). |
| `nearest_substation_name` | `data/processed/npg_headroom.geojson` | `name` of the substation matching `dist_substation_any_headroom_m`. |
| `intersects_aonb` | `data/processed/constraints/national-landscape.geojson` | True if `geopandas.sjoin(predicate="intersects")` returns any match. |
| `intersects_national_park` | `data/processed/constraints/national-park.geojson` | Same. |
| `intersects_green_belt` | `data/processed/constraints/green-belt.geojson` | Same. |
| `intersects_sssi` | `data/processed/constraints/site-of-special-scientific-interest.geojson` | Same. |
| `intersects_flood` | `data/processed/constraints/flood_zones.geojson` | Same. Flood zones are EA Flood Map for Planning, clipped to NE England in Wave 5B. |

The `intersects_*` columns are stored as native `bool` so they round-trip
through GeoJSON cleanly.

## Coordinate reference systems

* Parcels, raster inputs, and constraint GeoJSONs are all kept in EPSG:4326
  for the masked-mean and intersect work — that's the canonical project
  CRS and matches the raster bounds.
* Substation distance is the **only** step that needs metres, so we
  reproject parcel centroids and substation centroids to EPSG:27700 (BNG)
  for that step alone.
* The "geographic CRS" warning emitted by GeoPandas when computing
  centroids in EPSG:4326 is acknowledged: at NE England latitudes the
  geographic-vs-projected centroid offset is sub-pixel for parcels of this
  size and we only use these centroids for the lon/lat columns, never for
  distance.

## Known limitations

* **INSPIRE coverage.** HM Land Registry's INSPIRE Index Polygons cover
  registered titles only. Roughly 12% of England-and-Wales land is
  unregistered (Crown Estate, ancient commons, much of the Forestry
  Commission estate, etc.) and is **not** present in the parcel layer.
  Anyone using this demo to identify candidate sites should sanity-check
  the dataset coverage in their area of interest.
* **2 ha threshold.** We drop everything below 2 ha at ingestion (Wave 5A).
  For small-scale solar that's reasonable; for community wind it might
  exclude viable single-turbine plots.
* **Solar raster resolution.** Global Solar Atlas tiles are coarse (~925 m
  pixels) for parcels of this size. The fallback to `all_touched=True` for
  small parcels means a single overlapping pixel can be the only sample —
  fine for ranking parcels, but don't read the per-parcel kWh/kWp/yr value
  as a precision yield estimate.
* **Substation distance is "as the crow flies".** No grid-route weighting
  or HV-feeder topology is considered; this is a quick proximity proxy,
  not a connection cost estimate.
* **Constraint layers are point-in-time.** AONBs, National Parks, Green
  Belt, SSSIs, and EA flood zones change. Each of these GeoJSONs is dated
  in its own sidecar manifest — refer to those for the source date.
* **No agricultural land classification (ALC).** Grade-3b vs grade-1/2 is
  a major siting filter for utility-scale solar in England. The Defra ALC
  layer is on the deferred list and not yet attached.
