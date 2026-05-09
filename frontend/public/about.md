# About this tool

## What this is

A **screening map for renewable-energy siting and acquisition in North East England**. It pulls together the data you'd otherwise have to assemble across 8+ sources — grid headroom, resource potential, planning constraints, ownership, the existing project pipeline — into one place, so you can answer two questions a renewable-energy Operations Manager faces every week:

1. **Where could we *build* new generation?** Filter the ~33,000 land parcels by resource (solar / wind), grid headroom by voltage class, distance from constraints, and parcel area to surface candidates worth visiting.
2. **Where could we *acquire* existing generation?** Browse the DESNZ Renewable Energy Planning Database — every UK renewable project from "planning submitted" through "operational" — and the corporate ownership register to find who owns what.

The tool is a **screening layer**, not a feasibility model. It's designed to compress days of desk-research into minutes; the next step is always to dig into the specific parcel / substation / project.

## What each layer shows

| Layer | What it shows | Source |
|---|---|---|
| **Parcels (≥2 ha)** | 33,363 land parcels across 12 NE local authorities, each tagged with mean solar PVOUT, mean wind speed at 100m, distance to the nearest substation at each voltage class (11 / 20 / 33 / 66 / 132 kV), area, and distance to seven planning constraints. Filterable in real time. | HM Land Registry INSPIRE |
| **Substations (GSP / BSP / Primary)** | All 185 Northern Powergrid substations with their **gen-headroom** and **dem-headroom** in MVA. Click for the full breakdown. The grid hierarchy matters: a 20 MW solar farm needs a 33 kV+ substation; a 1 MW community array can connect at 11 kV. | Northern Powergrid heatmap |
| **REPD projects** | 572 NE England renewable projects, split by tech (solar 295 / wind 99 / battery 101 / hydro / other) and color-coded by status (operational, under construction, planning granted). | DESNZ Q1 2026 |
| **Constraints** | Where you *can't* build (or have to fight harder): AONB, National Park, SSSI, Green Belt, flood zones, listed buildings, scheduled monuments. | planning.data.gov.uk |
| **Built-up areas** | Urban footprints — useful as visual context for "this isn't agricultural land". | ONS BUA 2022 |
| **Solar PVOUT raster** | Long-term-average yearly photovoltaic output, kWh/kWp/year, ~250 m grid. | Global Solar Atlas |
| **Wind speed raster** | Mean wind speed at 100 m AGL, m/s. | Global Wind Atlas (DTU) |
| **Ownership (CCOD)** | 120,398 NE properties owned by UK-registered companies, with proprietor name, company reg, type, and HQ address. | HM Land Registry CCOD |

## Limitations to be honest about

- **No individual landowners.** CCOD only covers UK companies (~30% of NE properties). The remaining ~70% — including most agricultural land — is owned by individuals and is not in any free dataset. The map will show "no ownership found" on plenty of fields that absolutely *do* have an owner.
- **Postcode-level geocoding for ownership.** CCOD records geocode to their postcode centroid + ~30 m random jitter. ~80,000 records have no postcode at all and aren't on the map.
- **Parcel data has no owner names.** INSPIRE Index Polygons give you the parcel boundary but not the owner — that's a paid Land Registry product.
- **REPD coordinates are sometimes the planning office, not the actual site.** Spot-check before acting on a specific project's lat/lon.
- **Substation headroom is a snapshot.** It changes as projects connect and as DNO investment unlocks new capacity. The map shows what NPg published; treat it as directionally correct, not real-time.
- **Listed buildings are points** in the source data — you see ~12,400 dots across NE, not actual building footprints.
- **No detailed financial modelling.** No LCOE, no IRR, no curtailment risk. The tool flags candidates; you build the model.
- **Cold start ~30 sec.** First request after the server idles takes a moment to wake up and load the data.

## What's deliberately out of scope

- **Hydrogen.** Different siting logic (water access, demand offtake) — its own project.
- **OCOD** (overseas-owned UK property) — could be added as a sibling layer; not blocking.
- **Synthetic 0–100 site scores.** Earlier drafts had these. Dropped in favour of exposing the raw measurements — the user composes their own filter, the methodology stays explainable.
- **UK-wide coverage.** NE England only. Other DNOs (UKPN, SPEN, etc.) publish similar headroom data; extending is mechanical, not conceptual.

## Future improvements (in rough priority order)

1. **Per-parcel ownership join.** Cross-reference INSPIRE polygon centroids against CCOD postcodes so clicking a parcel surfaces likely corporate owners (with a clear "this excludes individuals" caveat).
2. **Distance-to-grid-line layer.** Currently we show distance to substations — but cable run distance through accessible terrain is often the binding cost.
3. **Slope / DEM-derived solar suitability.** SRTM 30 m or OS Terrain 50 to penalise steep / north-facing parcels.
4. **Co-location flagging.** Highlight parcels where solar + battery would fit together (good resource + grid headroom in both directions).
5. **Time-series headroom forecasts.** NPg's DFES gives 5–10 year capacity projections. Toggle "today" vs "2030" headroom.
6. **Substation-shed analysis.** For each substation with headroom, draw the catchment of land within X km that's also unconstrained — that's the *actually-usable* headroom, not the nameplate number.
7. **Save / share filter sets.** URL hash encoding so a user can share "my solar shortlist" with one link.
8. **Other DNOs.** UKPN, SPEN, NGED, ENW, SSEN — same pattern, different APIs.

## Built with

Vite + MapLibre GL JS + pmtiles.js (frontend); FastAPI + GeoPandas + rasterio (backend); Anthropic Claude Haiku 4.5 with tool use (chat assistant); tippecanoe → PMTiles → Cloudflare R2 (tile hosting); Railway (deploy). Source on [GitHub](https://github.com/coombesmatthew/ne-renewable-siting).
