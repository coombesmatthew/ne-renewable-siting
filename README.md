# NE England Renewable Siting & Headroom Map

*A screening tool for renewable development and acquisition in the North East of England.*

---

## Why this exists

Fuse Energy is vertically integrated — generate, trade, supply. The Operations Manager role explicitly mentions "M&A of energy generation sites (wind turbines, solar farms, and more)." That puts two questions on the desk every day:

1. **Where could we build new generation?** — site identification given resource, grid, and planning constraints.
2. **Where could we acquire existing generation?** — what's already in the pipeline in our area of interest.

This tool answers both for the North East of England — the region inside Northern Powergrid's licence area, with some of the best onshore wind in England and meaningful brownfield/agricultural land for solar. The thing the data keeps showing is that **grid headroom**, not land or resource, is the binding constraint for most projects. The map makes that constraint legible: distance to a substation with the right kind of headroom is a first-class attribute on every parcel.

## Live demo

🌐 **<https://ne-renewable-siting-production.up.railway.app>**

First request after a cold start may take ~30 seconds while the FastAPI worker loads ~230 MB of GeoPackage data into memory; subsequent requests are instant. The chat assistant ("💬" bottom-right) uses Claude Haiku 4.5 with six tools — try "Find me 5 parcels with wind > 8 m/s near a 33 kV substation, no AONB or SSSI".

A screenshot of the main map is at [`docs/screenshots/main.png`](docs/screenshots/main.png) *(placeholder).*

## What you can do with it

- **Filter 33,363 NE parcels** by 11 attributes — area, mean PVOUT, mean wind speed at 100 m, distance to a substation with generation headroom, distance to a substation with any headroom, and 7 constraint-overlap booleans (AONB, National Park, Green Belt, SSSI, flood, listed building, scheduled monument).
- **Browse the existing REPD pipeline** (572 NE projects) by technology, planning status, and capacity — the build-vs-buy lens, in one panel.
- **See the Northern Powergrid grid hierarchy** — 185 substation-area polygons, classified by voltage tier (GSP / BSP / Primary), with firm capacity, generation headroom, demand headroom, and constraints exposed on click.
- **Click any parcel, substation or REPD project** for the full attribute breakdown in a side panel.
- **Search by parcel ID** for direct lookup.
- **Ask the AI assistant** — it uses Claude Haiku with 6 tools (`find_parcels`, `get_parcel`, `search_substations`, `search_repd`, `sample_renewables_at`, `search_ownership`) and answers questions about whatever is currently on the map. It's deliberately a constrained map assistant, not a freelancing chatbot.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Browser                                                │
│  ┌───────────────────────────────────────────────────┐  │
│  │  Vite + MapLibre GL JS + pmtiles.js               │  │
│  │  Layer toggles, filter panel, click-for-details,  │  │
│  │  AI chat widget                                   │  │
│  └───────────────────────────────────────────────────┘  │
│        │                                  │             │
│        │ HTTP range reads                 │ HTTPS       │
│        ▼                                  ▼             │
│  ┌──────────────────────┐   ┌──────────────────────┐    │
│  │ Cloudflare R2        │   │ FastAPI on Railway   │    │
│  │ PMTiles (static)     │   │ /api/parcel/*        │    │
│  │ - parcels            │   │ /api/substation/*    │    │
│  │ - substations        │   │ /api/repd/*          │    │
│  │ - repd               │   │ /api/ownership/*     │    │
│  │ - constraints        │   │ /api/chat (Claude)   │    │
│  │ - solar / wind raster│   │ raster sampling      │    │
│  │ - ccod / built-up    │   │ data store in-memory │    │
│  └──────────────────────┘   └──────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

The frontend reads PMTiles directly from Cloudflare R2 over HTTP range requests — there's no tile server. The FastAPI backend handles dynamic queries (parcel attribute lookup, REPD search, ownership search, raster point-sampling) and proxies the AI chat to Claude. All ETL is one-shot and offline (`etl/run_all.py`); the runtime hits no third-party data APIs.

## Data sources

All free and open. License compatibility checked: OGL 3.0 or CC-BY 4.0 throughout.

| Layer | Source | Format | License | Notes |
|---|---|---|---|---|
| NE England polygon (12 LADs) | ONS LAD Dec 2024 BGC | GeoJSON | OGL 3.0 | Dissolved clip mask. |
| Local authority boundaries | ONS LAD Dec 2024 BGC | GeoJSON | OGL 3.0 | 12 features. |
| INSPIRE Index Polygons (≥2 ha) | HM Land Registry | GML → GeoJSON | OGL 3.0 | 33,363 parcels across 12 LADs. Filtered from ~1.19 M raw to drop residential gardens. **No owner names** — INSPIRE does not include them. |
| NPg substation headroom | Northern Powergrid Opendatasoft | GeoJSON | OGL 3.0 | 185 polygons with firm capacity, gen/dem headroom, fault levels. |
| NPg Embedded Capacity Register | Northern Powergrid Opendatasoft | GeoJSON | OGL 3.0 | 697 connected/queued generation points (≥1 MW + <1 MW combined). |
| DESNZ REPD Q1 2026 | gov.uk | CSV → GeoJSON | OGL 3.0 | 572 NE projects. Solar PV (295), Battery (101), Wind Onshore (99), plus bio/AD/EfW/hydro/H2. |
| Global Solar Atlas — PVOUT | World Bank / Solargis | GeoTIFF | CC-BY 4.0 | ~925 m raster, kWh/kWp/yr. |
| Global Wind Atlas — 100 m | DTU / World Bank | GeoTIFF | CC-BY 4.0 | Mean wind speed at 100 m AGL, m/s. |
| Constraint — National Landscape (AONB) | planning.data.gov.uk | GeoJSON | OGL 3.0 | 2 features. |
| Constraint — National Park | planning.data.gov.uk | GeoJSON | OGL 3.0 | 3 features. |
| Constraint — Green Belt | planning.data.gov.uk | GeoJSON | OGL 3.0 | 7 features. |
| Constraint — SSSI | planning.data.gov.uk | GeoJSON | OGL 3.0 | 250 features. |
| Constraint — Flood Zones 2 & 3 | planning.data.gov.uk (EA Flood Map for Planning) | GeoJSON | OGL 3.0 | 12,186 features. Clipped from the 2.58 GB national dataset. |
| Constraint — Listed Buildings | planning.data.gov.uk (Historic England) | GeoJSON | OGL 3.0 | 12,432 features. |
| Constraint — Scheduled Monuments | planning.data.gov.uk (Historic England) | GeoJSON | OGL 3.0 | 1,412 features. |
| ONS Built-up Areas 2022 | ONS | GeoJSON | OGL 3.0 | Used as cartographic context. |
| HM Land Registry CCOD | HM Land Registry | CSV → GeoJSON | OGL 3.0 (commercial use is paid) | 120,398 NE corporate-owned properties geocoded to postcode centroid + ~30 m jitter. **Only covers UK companies** — individual owners are not in CCOD. |
| Postcode geocoding | postcodes.io / ONS | API | OGL 3.0 | Used for the CCOD layer (postcode-centroid lookups). |

Total live layers: 15. Total features in the vector layers covered by the manifest: ~62 K (parcels, substations, REPD, constraints) plus ~120 K CCOD points. Source data totals ~556 MB on disk; PMTiles outputs are ~30–80 MB combined.

## Methodology

The unit of analysis is the **real land parcel**, not an arbitrary hex. Each retained parcel carries raw measurements — not a synthetic 0–100 score:

- mean PVOUT (kWh/kWp/yr) and mean wind speed at 100 m (m/s) sampled from the rasters inside the polygon.
- distance in metres (BNG) to the nearest substation with **generation** headroom and to the nearest substation with **any** (gen or dem) headroom.
- 7 boolean overlaps with constraint layers (AONB, NP, GB, SSSI, flood, listed, scheduled).
- area, centroid, LAD code/name.

The frontend composes a MapLibre filter expression directly over those raw fields. A solar shortlist is `area > 5 ha AND mean_pvout > 950 AND dist_substation_gen < 5000 AND NOT intersects_aonb AND NOT intersects_sssi AND NOT intersects_flood`. A wind shortlist is the same shape with the wind attribute swapped in and tighter constraint thresholds. **Why no synthetic score?** A weighted score forces a single answer about which weights matter, and hides them in the build. Surfacing the inputs makes the methodology explainable and the filter editable — that's the bit that signals you understand the domain.

The deep dive — what each attribute means, how it's computed, voltage tier semantics, the postcode-jitter approach for CCOD, and a candid limits section — lives in [`docs/methodology.md`](docs/methodology.md).

## Local dev

Prereqs: [`uv`](https://github.com/astral-sh/uv) for Python, `node` ≥ 20 for the frontend, an `ANTHROPIC_API_KEY` for the chat endpoint.

**Terminal 1 — backend (port 8000):**

```bash
git clone <repo>
cd fuse-applications
uv sync
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
uv run uvicorn backend.main:app --reload
# → http://localhost:8000/api/health
```

**Terminal 2 — frontend (port 5173, falls back to 5174 if busy):**

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

The frontend reads PMTiles from R2 (URLs in [`frontend/public/tile_urls.json`](frontend/public/tile_urls.json)) and hits the backend at `http://localhost:8000`. CORS is open for any `localhost:*` origin in dev.

To rebuild the data (one-shot ETL, ~30 min, requires the source raster downloads in `data/raw/`):

```bash
uv run python -m etl.run_all
```

## Deploy

Single-container deployment to Railway.

- **Build:** `frontend/` is built (`npm run build` → `frontend/dist/`) and served as static assets by FastAPI in production.
- **Backend:** `backend/main.py` runs under uvicorn.
- **Env vars:** `ANTHROPIC_API_KEY` (required for `/api/chat`).
- **PMTiles** are NOT shipped in the container — their R2 URLs are baked into `frontend/public/tile_urls.json` and read directly by the browser.
- **CORS:** prod is same-origin so the dev allowlist is irrelevant.
- **Cold start** is ~30–60 s on first hit (Python startup + GeoJSON load into the in-memory data store). Warm responses are sub-100 ms.

## Out of scope (deliberately)

- **Ownership of individuals.** CCOD only covers UK-registered companies. Most agricultural land in NE England is owned by individuals and would require Land Registry's paid product. The map states this clearly in the footer and the AI is system-prompted to say so when asked.
- **Hydrogen siting logic.** Fuse mention hydrogen; including it properly needs electrolyser siting (water access, demand offtake) — its own project.
- **Detailed financial modelling.** No LCOE, no IRR. This is a screening layer, not a feasibility model.
- **Synthetic per-parcel score.** See methodology — exposed inputs only.
- **UK-wide coverage.** NE England only. The ETL is parameterised by a list of 12 LAD codes; extending to another DNO area is mechanically straightforward but not done here.

## Limits / known issues

- **INSPIRE doesn't include owner names.** Parcel boundaries only.
- **CCOD covers only ~30% of NE properties.** Individual owners — most agricultural land — are absent.
- **Postcode-level geocoding for CCOD.** Each record sits at its postcode centroid + ~30 m random jitter. Stacked points at one postcode are real (multiple titles share it) but their exact within-postcode location is approximate.
- **~79 K NE CCOD records have no usable postcode** in the source CSV and aren't rendered on the map (they survive in `/api/ownership/by_proprietor` search results but with no geometry).
- **REPD coordinates are sometimes the planning office, not the actual site.** Spot-check before relying on a single point.
- **Solar raster is ~925 m.** Per-parcel kWh/kWp/yr is fine for ranking, not for yield estimation.
- **Substation distance is "as the crow flies".** No HV-feeder topology, no cable-route weighting.
- **Railway cold start.** ~30–60 s on the first hit after the container scales to zero.
- **Constraint layers are point-in-time** and are dated in their sidecar manifests (`data/processed/constraints/*.manifest.json`).
- **No agricultural land classification (ALC).** Grade-3b vs grade-1/2 is a major siting filter for utility-scale solar in England — Defra's ALC layer is on the deferred list.

## License

Code is MIT — see [LICENSE](LICENSE). Each data layer retains its source license (OGL 3.0 or CC-BY 4.0); attributions are listed below and surfaced in the application footer.

## Acknowledgements

Northern Powergrid Open Data Portal · DESNZ (REPD) · ONS Geoportal · HM Land Registry (INSPIRE Index Polygons, CCOD) · World Bank / Solargis (Global Solar Atlas) · DTU / World Bank (Global Wind Atlas) · Environment Agency · planning.data.gov.uk (MHCLG, Natural England, Historic England) · postcodes.io.
