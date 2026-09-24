#!/usr/bin/env bash
# API start command for Render (see render.yaml).
#
# 1. Apply database migrations.
# 2. Start the API.
#
# Fails closed: if the migration fails, the script exits non-zero before uvicorn starts,
# so the deploy fails and Render keeps serving the previous successful deploy (if any).
# Alembic runs all pending migrations in a single transaction (PostgreSQL has transactional
# DDL), so a failure leaves the schema unchanged rather than partially migrated.
#
# Running migrations at startup is only safe while exactly ONE API instance starts at a
# time (the free staging setup). Revisit before scaling out (docs/DECISIONS.md, ADR-013).
set -euo pipefail

cd "$(dirname "$0")/.."

: "${PORT:?PORT must be set}"

echo "start.sh: applying database migrations"
uv run --no-sync alembic upgrade head

echo "start.sh: starting API on port ${PORT}"
# --no-proxy-headers: uvicorn must not rewrite the client address from X-Forwarded-For
# (client-controlled). The app derives the rate-limit client IP itself using the configured
# TRUSTED_PROXY_HOPS (app/common/rate_limit.py).
exec uv run --no-sync uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --no-proxy-headers
