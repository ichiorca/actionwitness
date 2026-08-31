# syntax=docker/dockerfile:1
# SKELETON — completed during hardening (spec §29.1, delivery Sep 1–2).
# Multi-stage build per §29.1:
#   1. build the harness and Buggy Store frontend assets independently;
#   2. install actionwitness_core, actionwitness_service, and enabled integrations as separate distributions;
#   3. install and start the standalone Buggy Store package;
#   4. copy each application's assets into its own static directory.

FROM node:22-slim AS frontend-build
WORKDIR /build
# TODO(M4): copy apps/actionwitness_service/frontend and examples/buggy_store/frontend; npm ci && npm run build for each.

FROM python:3.12-slim AS runtime
WORKDIR /app
# TODO(M4): install uv; copy workspace; uv sync --frozen --no-dev; copy built frontend assets
#           into per-application static directories; run DB seed on startup if absent.
# One service instance, one Uvicorn worker (SQLite MVP — §17, LD-11). Bind the platform port.
# TODO(M4): CMD ["uvicorn", "actionwitness_service.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "$PORT"]
