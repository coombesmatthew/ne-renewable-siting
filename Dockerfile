# Multi-stage build for NE Renewable Siting Tool.
#   Stage 1: build the vite/MapLibre frontend -> dist/
#   Stage 2: convert big GeoJSONs -> GeoPackage (so the runtime image
#            doesn't carry a 461 MB geojson layer that gets `rm`-ed later)
#   Stage 3: python:3.12-slim runtime with FastAPI + geopandas/rasterio.
#
# Note: Railway's builder doesn't support BuildKit `--mount=type=bind`,
# hence the dedicated conversion stage (instead of bind-mounting the
# context). Only the resulting .gpkg files are COPYd into the runtime
# image — Docker layer history doesn't see the original .geojsons.

# ---------- Stage 1: build frontend ----------
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --silent
COPY frontend/ ./
RUN npm run build


# ---------- Stage 2: convert GeoJSONs -> GeoPackage ----------
# Lightweight stage with just GDAL — no Python, no node.
FROM debian:bookworm-slim AS data-convert
RUN apt-get update && apt-get install -y --no-install-recommends gdal-bin \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY data/processed/parcels_attributed.geojson \
     data/processed/ccod_ne.geojson \
     data/processed/npg_headroom.geojson \
     data/processed/npg_substations_points.geojson \
     data/processed/repd.geojson \
     ./
# Convert each with a graceful fallback. If a single conversion fails we
# leave the .geojson alongside so data_store.py's loader can fall back.
RUN set -eu; \
    mkdir -p /out; \
    for f in parcels_attributed ccod_ne npg_headroom npg_substations_points repd; do \
        if [ ! -f "/src/${f}.geojson" ]; then \
            echo "[convert] WARN: ${f}.geojson missing, skipping"; continue; \
        fi; \
        if ogr2ogr -f GPKG "/out/${f}.gpkg" "/src/${f}.geojson"; then \
            echo "[convert] ${f}.geojson -> ${f}.gpkg"; \
        else \
            echo "[convert] WARN: ${f} conversion failed, copying geojson"; \
            cp "/src/${f}.geojson" "/out/${f}.geojson"; \
        fi; \
    done


# ---------- Stage 3: backend runtime ----------
FROM python:3.12-slim AS runtime

# Runtime GDAL is needed for rasterio/pyogrio. build-essential +
# libgdal-dev are only needed during `uv sync` to compile some wheels —
# purge in the same RUN so they don't persist as a layer.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager).
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Python project metadata + source. hatchling build-editable needs
# README.md and the package dirs to exist when uv sync runs.
COPY pyproject.toml uv.lock README.md ./
COPY backend/ ./backend/
COPY etl/ ./etl/

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgdal-dev \
    && uv sync --frozen --no-dev \
    && apt-get purge -y --auto-remove build-essential libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Frontend dist from stage 1.
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Data manifests + small support files.
COPY data/data_manifest.json ./data/data_manifest.json
COPY data/ne_england.geojson ./data/ne_england.geojson
COPY data/processed/parcels.manifest.json ./data/processed/parcels.manifest.json
COPY data/processed/ccod_ne.manifest.json ./data/processed/ccod_ne.manifest.json
COPY data/processed/npg_headroom.manifest.json ./data/processed/npg_headroom.manifest.json
COPY data/processed/npg_substations_points.manifest.json ./data/processed/npg_substations_points.manifest.json
COPY data/processed/repd.manifest.json ./data/processed/repd.manifest.json
COPY data/raw/solar_pvout.tif ./data/raw/solar_pvout.tif
COPY data/raw/wind_speed_100m.tif ./data/raw/wind_speed_100m.tif

# Pre-converted .gpkg files from stage 2. Only converted artefacts (not
# the source .geojsons) end up in this image's history.
COPY --from=data-convert /out/ ./data/processed/

# Railway sets PORT at runtime.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uv run uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
