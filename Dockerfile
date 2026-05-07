# Dockerfile — STUB (to be implemented in Chunk 5)
#
# Planned multi-stage build:
#   Stage 1 (node:20-alpine): build frontend with vite -> dist/
#   Stage 2 (python:3.12-slim): install uv, run `uv sync --no-dev`,
#                               copy backend + dist + source TIFFs (data/raw/*.tif),
#                               expose uvicorn on $PORT.
#   Target final image size: < 600 MB.
#
# Placeholder for now — see plan/adaptive-dreaming-curry.md, Chunk 5.
