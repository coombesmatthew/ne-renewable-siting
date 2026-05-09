# Multi-stage build for NE Renewable Siting Tool.
#   Stage 1: build the vite/MapLibre frontend -> dist/
#   Stage 2: python:3.12-slim runtime with FastAPI + geopandas/rasterio.
#
# Big data files (parcels_attributed.gpkg ~187 MB, ccod_ne.gpkg ~43 MB)
# are pulled from Cloudflare R2 at build time — too big to commit to git.
# Small files (manifests, NE polygon, source TIFFs, smaller GeoJSONs)
# travel in the build context.

# ---------- Stage 1: build frontend ----------
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --silent
COPY frontend/ ./
RUN npm run build


# ---------- Stage 2: backend runtime ----------
FROM python:3.12-slim AS runtime

# Runtime GDAL is required by rasterio/pyogrio. Install curl too — used
# to fetch backend-data .gpkg files from R2 during build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager).
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Python project metadata + source. hatchling editable build needs
# README.md and the package dirs to exist when uv sync runs.
COPY pyproject.toml uv.lock README.md ./
COPY backend/ ./backend/
COPY etl/ ./etl/

# build-essential + libgdal-dev only needed during uv sync to compile
# wheels; purge in the same RUN so they don't bloat the final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgdal-dev \
    && uv sync --frozen --no-dev \
    && apt-get purge -y --auto-remove build-essential libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Frontend dist from stage 1.
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Small data files (commit-friendly). The COPYs are explicit per-file so
# .gitignore exclusions don't matter — Docker only sees what we name.
COPY data/data_manifest.json ./data/data_manifest.json
COPY data/ne_england.geojson ./data/ne_england.geojson
COPY data/processed/parcels.manifest.json ./data/processed/parcels.manifest.json
COPY data/processed/ccod_ne.manifest.json ./data/processed/ccod_ne.manifest.json
COPY data/processed/npg_headroom.manifest.json ./data/processed/npg_headroom.manifest.json
COPY data/processed/npg_substations_points.manifest.json ./data/processed/npg_substations_points.manifest.json
COPY data/processed/repd.manifest.json ./data/processed/repd.manifest.json
COPY data/processed/npg_headroom.geojson ./data/processed/npg_headroom.geojson
COPY data/processed/npg_substations_points.geojson ./data/processed/npg_substations_points.geojson
COPY data/processed/repd.geojson ./data/processed/repd.geojson
COPY data/raw/solar_pvout.tif ./data/raw/solar_pvout.tif
COPY data/raw/wind_speed_100m.tif ./data/raw/wind_speed_100m.tif

# Big files from R2. Public bucket, no auth needed for GET.
# These two together are ~230 MB; downloads at build time once per image.
ARG R2_PUBLIC_URL=https://pub-fa01f308ec47440aae36ce1671c5a1e3.r2.dev
RUN curl -fsSL "${R2_PUBLIC_URL}/backend-data/parcels_attributed.gpkg" \
        -o /app/data/processed/parcels_attributed.gpkg \
    && curl -fsSL "${R2_PUBLIC_URL}/backend-data/ccod_ne.gpkg" \
        -o /app/data/processed/ccod_ne.gpkg \
    && ls -la /app/data/processed/

# Railway sets PORT at runtime.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uv run uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
