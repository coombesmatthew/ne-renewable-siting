# syntax=docker/dockerfile:1.7
#
# Multi-stage build for NE Renewable Siting Tool.
#   Stage 1: build the vite/MapLibre frontend -> dist/
#   Stage 2: python:3.12-slim runtime with FastAPI + geopandas/rasterio,
#            pre-converts the big GeoJSONs to GeoPackage for ~5-10x
#            faster cold-start.
#
# The data-conversion + build-tool teardown happens in a single RUN so
# the .geojson originals and GDAL build packages don't bloat earlier
# layers (Docker layers are additive — `rm` in a later layer doesn't
# shrink the parent).

# ---------- Stage 1: build frontend ----------
FROM node:20-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --silent
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: backend + data ----------
FROM python:3.12-slim AS runtime

# System deps. We install gdal-bin (runtime) up front. The compiler
# toolchain + libgdal-dev are installed temporarily inside the data
# conversion RUN so they don't bloat the final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gdal-bin \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app

# Python project metadata + source dirs (hatchling build-editable needs
# README.md and the package dirs to exist when ``uv sync`` runs).
COPY pyproject.toml uv.lock README.md ./
COPY backend/ ./backend/
COPY etl/ ./etl/

# Install Python deps. Build-essential is needed for some wheels (e.g.
# fiona) on arm64; remove it in the same layer so we don't ship it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgdal-dev \
    && uv sync --frozen --no-dev \
    && apt-get purge -y --auto-remove build-essential libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Frontend dist from stage 1
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# Data files — only what the backend actually needs at runtime.
# Stage them in /tmp/data-src so the conversion RUN can replace big
# GeoJSONs with GeoPackage equivalents in a single layer.
COPY data/data_manifest.json ./data/data_manifest.json
COPY data/ne_england.geojson ./data/ne_england.geojson
COPY data/processed/parcels.manifest.json ./data/processed/parcels.manifest.json
COPY data/processed/ccod_ne.manifest.json ./data/processed/ccod_ne.manifest.json
COPY data/processed/npg_headroom.manifest.json ./data/processed/npg_headroom.manifest.json
COPY data/processed/npg_substations_points.manifest.json ./data/processed/npg_substations_points.manifest.json
COPY data/processed/repd.manifest.json ./data/processed/repd.manifest.json
COPY data/raw/solar_pvout.tif ./data/raw/solar_pvout.tif
COPY data/raw/wind_speed_100m.tif ./data/raw/wind_speed_100m.tif

# Convert big GeoJSONs to GeoPackage. The geojson sources are bind-mounted
# from the build context (BuildKit), so the originals never become a
# layer in the final image — only the resulting .gpkg files persist.
# If a conversion fails we copy the .geojson out of the bind mount so
# data_store.py's fallback path still has something to read.
RUN --mount=type=bind,source=data/processed,target=/tmp/data-src,readonly \
    set -eu \
    && for f in parcels_attributed ccod_ne npg_headroom npg_substations_points repd; do \
         src="/tmp/data-src/${f}.geojson"; \
         if [ ! -f "$src" ]; then \
           echo "[docker] WARN: $src missing, skipping"; continue; \
         fi; \
         if ogr2ogr -f GPKG "/app/data/processed/${f}.gpkg" "$src"; then \
           echo "[docker] converted ${f}.geojson -> ${f}.gpkg"; \
         else \
           echo "[docker] WARN: ${f} conversion failed, falling back to geojson"; \
           cp "$src" "/app/data/processed/${f}.geojson"; \
         fi; \
       done

# Railway sets PORT at runtime.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uv run uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
