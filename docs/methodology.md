# Methodology — NE England Renewable Siting & Headroom Map

This document is the deeper-dive companion to the project [`README.md`](../README.md). It explains how each layer is computed, why we deliberately avoid a synthetic site score, how the frontend filter expression is composed, how the AI tools map to the data, and where the honest limits lie.

---

## 1. Why no synthetic 0–100 site score

A common temptation when building a renewable-siting tool is to fold every input — solar, wind, distance to grid, designations, flooding — into a single 0–100 number per parcel. We deliberately do **not** do that here:

- **Transparency.** Every weight in a composite score is a value judgement. Surfacing the raw measurements (mean PVOUT, m/s wind, metres to substation, boolean overlaps) lets the reader form their own opinion of which parcels matter, and audit individual numbers against external sources.
- **Frontend filters.** The UI exposes range sliders and toggles directly over the raw fields (e.g. "wind ≥ 7 m/s and within 5 km of any-headroom substation, exclude Green Belt"). A pre-baked score wouldn't support that.
- **Editable thresholds.** Real siting decisions are sensitive to local policy (LPA stance on grade-3b agricultural land, how strict a developer is on slopes, etc.). The user gets to pick — we just hand them the inputs.
- **Different technologies have different weights.** The "right" weighting for solar is not the right weighting for onshore wind, and neither is right for battery (which is a pure grid-headroom + planning play). One score per parcel collapses these into a single answer; raw inputs let each technology's filter live separately.

---

## 2. The parcel attribute layer

The canonical parcel layer is `data/processed/parcels.geojson` — 33,363 INSPIRE Index Polygons ≥ 2 ha across the 12 NE England LADs, filtered from ~1.19 M raw INSPIRE features. Wave 5C reads this file, attaches the columns below, and writes `data/processed/parcels_attributed.geojson` alongside it.

Geometries are repaired with `shapely.make_valid` before any spatial work. GeometryCollections are reduced to their first polygonal part so `rasterio.mask.mask` and `geopandas.sjoin` keep working downstream.

### Attribute table

| Column | Source | Computation |
|---|---|---|
| `parcel_id` | `parcels.geojson` | INSPIRE per-LA id, namespaced with the LAD code. |
| `area_ha` | geometry | Area in hectares from the BNG-projected geometry. |
| `centroid_lon`, `centroid_lat` | geometry | Centroid in EPSG:4326. |
| `lad_code`, `lad_name` | spatial join | LA the parcel sits in, per ONS LAD Dec 2024. |
| `mean_pvout_kwhkwp` | `data/raw/solar_pvout.tif` (Global Solar Atlas, kWh/kWp/yr, ~925 m pixels) | Mean of all in-polygon pixels (`rasterio.mask.mask`). Pixels with the float32-min sentinel (values < 1.0) are treated as nodata. Small parcels that miss every pixel centre fall back to `all_touched=True` so they still receive a representative value. |
| `mean_wind_speed_100m_ms` | `data/raw/wind_speed_100m.tif` (Global Wind Atlas v3 at 100 m, m/s) | Same masked-mean logic. Nodata is NaN. |
| `dist_substation_gen_headroom_m` | `data/processed/npg_headroom.geojson` | Euclidean distance (BNG / EPSG:27700) from the parcel centroid to the nearest NPg substation polygon centroid where `genhr > 0`. Computed via `scipy.spatial.cKDTree` on the BNG-projected substation centroids. |
| `dist_substation_any_headroom_m` | `data/processed/npg_headroom.geojson` | Same, but the substation set is filtered to those with **any** headroom (`genhr > 0` *or* `demhr > 0`). |
| `nearest_substation_name` | `data/processed/npg_headroom.geojson` | `name` of the substation matching `dist_substation_any_headroom_m`. |
| `intersects_aonb` | `constraints/national-landscape.geojson` | True if `geopandas.sjoin(predicate="intersects")` returns any match. |
| `intersects_national_park` | `constraints/national-park.geojson` | Same. |
| `intersects_green_belt` | `constraints/green-belt.geojson` | Same. |
| `intersects_sssi` | `constraints/site-of-special-scientific-interest.geojson` | Same. |
| `intersects_flood` | `constraints/flood_zones.geojson` | Same. EA Flood Map for Planning, clipped to NE England. |
| `intersects_listed` | `constraints/listed-building.geojson` | Precomputed. Listed buildings are a mix of points, polygons and multi-polygons; the test passes if any match. |
| `intersects_scheduled` | `constraints/scheduled-monument.geojson` | Same. |

The `intersects_*` columns are stored as native `bool` so they round-trip through GeoJSON cleanly.

### Coordinate reference systems

- Parcels, raster inputs, and constraint GeoJSONs are kept in **EPSG:4326** for the masked-mean and intersect work — that's the canonical project CRS and matches the raster bounds.
- Substation distance is the **only** step that needs metres, so we reproject parcel centroids and substation centroids to **EPSG:27700 (BNG)** for that step alone.
- The "geographic CRS" warning emitted by GeoPandas when computing centroids in EPSG:4326 is acknowledged: at NE England latitudes the geographic-vs-projected centroid offset is sub-pixel for parcels of this size, and we only use these centroids for the lon/lat columns and as kd-tree query points after BNG reprojection — never directly for distance.

---

## 3. The frontend filter expression

The map is rendered with MapLibre GL JS. The parcel layer's `filter` is a single composed expression evaluated client-side against the PMTiles features. Sliders and toggles in the filter panel rebuild the expression on change.

For example, a "solar shortlist" filter is:

```js
[
  "all",
  [">", ["get", "area_ha"], 5],
  [">", ["get", "mean_pvout_kwhkwp"], 950],
  ["<", ["get", "dist_substation_gen_headroom_m"], 5000],
  ["!", ["get", "intersects_aonb"]],
  ["!", ["get", "intersects_sssi"]],
  ["!", ["get", "intersects_flood"]]
]
```

A wind shortlist is the same shape with `mean_wind_speed_100m_ms` swapped in and tighter constraint defaults. A battery shortlist drops resource entirely and tightens `dist_substation_any_headroom_m`. Because the filter is composed live in JavaScript, every threshold is editable and every slider position is reflected in the map immediately — no round-trip to the backend, no precomputed score table.

Parcels not matching the filter are styled out (faded or hidden) rather than removed, so the user retains spatial context.

---

## 4. Substation voltage tier semantics

The 185 NPg substation polygons span four voltage tiers (GSP, BSP, Primary, Secondary). They are nested: a Primary substation's catchment is a subset of its parent BSP's catchment, which is a subset of its parent GSP's catchment.

The substations layer carries a `tier` attribute and the headroom values are **cumulative within the tier**. That means:

- Selecting "GSP" in the voltage dropdown shows the broad regional network — useful for understanding macro-level constraints.
- Selecting "Primary" surfaces the finest-grained polygons and the actual headroom a small project would see.
- The default view shows all tiers as a stacked legend so the user can see the hierarchy.

The frontend dropdown filters on `tier` directly. Click any substation to see its full attribute set: `firm_cap`, `gentot`, `genhr`, `demhr`, `worst_case_constraint_gen_colour`, fault levels, parent GSP/BSP, etc.

---

## 5. Listed buildings + scheduled monuments precomputation

`constraints/listed-building.geojson` has 12,432 features and `constraints/scheduled-monument.geojson` has 1,412. At runtime these layers are too dense to spatially join against 33,363 parcels per filter change, and the constraints themselves are static. So the `intersects_listed` and `intersects_scheduled` flags are precomputed once in the ETL (`etl/attributes.py`) and baked into the parcel attribute table.

This trades ~70 KB of additional GeoJSON payload (two booleans × 33,363 features) for sub-millisecond filter performance on the client. The constraints layers themselves are still served as PMTiles for the user to toggle on visually — the precomputed flags are just for the parcel filter.

---

## 6. The CCOD ownership layer

The HM Land Registry CCOD (Commercial and Corporate Ownership Data) is a 1.56 GB national CSV (4.4 M rows). The ETL filters to the 8 NE counties (TYNE AND WEAR, COUNTY DURHAM, NORTHUMBERLAND, STOCKTON-ON-TEES, MIDDLESBROUGH, REDCAR AND CLEVELAND, DARLINGTON, HARTLEPOOL) and to records with NE-area postcode prefixes (`NE`, `DH`, `TS`, `SR`, `DL1`–`DL3`). The union yields **202,431 records**.

### Postcode geocoding + jitter

Each record has a postcode but not a coordinate. We reverse-geocode unique postcodes with the **postcodes.io bulk endpoint** (100 per request, no auth, free), then join lat/lon back to records.

Each record then receives a uniform random jitter of **±~30 m in BNG** before being written out. Reasoning:

- Postcode centroids are accurate to ~50 m. Pretending we know the within-postcode location of a title is dishonest.
- Without jitter, all titles sharing a postcode stack on a single pixel and become unclickable at high zoom.
- ±30 m is a sub-postcode jitter — visually disambiguates stacked points, doesn't create false geographic precision.

After geocoding and dropping records whose postcode failed to resolve, **120,398** features end up on the map. ~82 K records have no usable postcode in the source CSV; they're kept in the data store for proprietor-name search but have no geometry.

Output: `data/processed/ccod_ne.geojson`. Tiled into `ccod.pmtiles` with clustering (`--cluster-distance=12 --cluster-densest-as-needed`) so low zooms render as cluster circles labelled with record count, then expand into individual points at high zoom.

---

## 7. How the AI tools map to data

The chat endpoint is implemented with the Anthropic SDK using **Claude Haiku** and tool-use. The tool definitions live in `backend/services/claude_tools.py`. Each tool is a thin wrapper around an existing data-store query.

| Tool | What it does | Data backing |
|---|---|---|
| `get_parcel(parcel_id)` | Returns the full attribute payload for one parcel. | `parcels_attributed.geojson` indexed by `parcel_id` in `data_store.py`. |
| `find_parcels(filters, limit)` | Returns parcels matching attribute thresholds (e.g. `mean_wind_speed_100m_ms ≥ 7 AND dist_substation_any_headroom_m ≤ 5000 AND NOT intersects_aonb`). | Same data store; predicate evaluated in-process. |
| `search_substations(q OR voltage_tier OR ...)` | Returns substations matching name substring or filter. | `npg_headroom.geojson`. |
| `search_repd(tech, status, capacity_min/max, ...)` | Returns REPD projects with the requested filters. | `repd.geojson` from DESNZ Q1 2026. |
| `sample_renewables_at(lng, lat)` | Point-samples PVOUT and wind from the source rasters. | `data/raw/solar_pvout.tif`, `wind_speed_100m.tif` via `raster_sampler.py`. |
| `search_ownership(proprietor_name OR postcode OR title_number, limit)` | Searches CCOD for corporate ownership. | `ccod_ne.geojson` indexed by postcode and proprietor name. |

The system prompt clamps the assistant to "you are an assistant for a renewable siting map for NE England" and explicitly instructs it to mention the individual-owner caveat whenever ownership comes up — the failure mode to avoid is the bot saying "no owner found" when in fact a private individual likely owns the land and CCOD just doesn't see them.

---

## 8. Honest limits

- **INSPIRE coverage.** HM Land Registry's INSPIRE Index Polygons cover registered titles only. Roughly 12% of England-and-Wales land is unregistered (Crown Estate, ancient commons, much of the Forestry Commission estate, etc.) and is **not** present in the parcel layer. Anyone using this demo to identify candidate sites should sanity-check coverage in their area of interest.
- **2 ha threshold.** We drop everything below 2 ha at ingestion. For utility-scale solar that's reasonable; for community wind it might exclude viable single-turbine plots.
- **Solar raster resolution.** Global Solar Atlas tiles are coarse (~925 m pixels) for parcels of this size. The `all_touched=True` fallback means a single overlapping pixel can be the only sample — fine for ranking parcels, but don't read the per-parcel kWh/kWp/yr value as a precision yield estimate.
- **Substation distance is "as the crow flies".** No grid-route weighting or HV-feeder topology is considered; this is a quick proximity proxy, not a connection cost estimate.
- **REPD coordinates** are sometimes the planning office rather than the site. The data store reflects the source; spot-check before treating any single point as a precise site location.
- **Constraint layers are point-in-time.** AONBs, National Parks, Green Belt, SSSIs, listed buildings, scheduled monuments and EA flood zones change. Each GeoJSON has a sidecar manifest (`data/processed/constraints/*.manifest.json`) with the source date.
- **No agricultural land classification (ALC).** Grade-3b vs grade-1/2 is a major siting filter for utility-scale solar in England. The Defra ALC layer is on the deferred list.
- **CCOD only covers UK companies.** Individual owners — most agricultural land — are not in this dataset. The footer and the AI both surface that caveat.
- **CCOD postcode resolution.** ±30 m random jitter is the granularity. Stacked points at one postcode reflect multiple titles, not a known multi-title location.
- **CCOD postcode misses.** ~82 K NE corporate-owned titles have no usable postcode in the source CSV and aren't on the map (they survive in proprietor-name search results but with no geometry).
- **Raster point-sampling tool** doesn't deduplicate against `mean_pvout_kwhkwp` — it queries the raster fresh, which can return a slightly different value to the parcel-level mean for points near pixel boundaries. Both are honest; the parcel-level mean is the better summary, the point-sample is for clicked-but-not-filtered locations.
