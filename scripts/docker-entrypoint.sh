#!/bin/sh
# Container entrypoint — starts both application processes behind one origin
# (spec §29.1; 009-T2/T3).
#
# Two processes, deliberately. §25.11 and the constitution forbid the harness
# from importing the demo target: co-location in one container "shall not bypass
# the versioned target API or adapter boundary". Running the store as its own
# process on loopback keeps the only route between them an HTTP call to
# /demo/api/v1, which is the same route the adapter uses in development and the
# same one the tests exercise. Importing `buggy_store` into the service would
# have been fewer moving parts and a different product.
#
# The store binds 127.0.0.1 and is never published: the only way in from outside
# the container is the harness's own /demo/api/v1 proxy, which is subject to the
# origin policy and the rate limiter like every other route.

set -eu

# Render supplies PORT; the defaults keep `docker run` with no environment working.
: "${PORT:=8000}"
: "${BUGGY_STORE_PORT:=8001}"
: "${BUGGY_STORE_DATABASE:=/data/buggy-store.sqlite3}"
: "${HARNESS_DATABASE_PATH:=/data/actionwitness.sqlite3}"
: "${HARNESS_ARTIFACT_ROOT:=/data/artifacts}"
: "${HARNESS_STATIC_ROOT:=/app/static}"

# The adapter's base URL is derived, never taken from the environment. An
# operator-supplied value here could point the "independent observation" at a
# host the tool under test also controls, which is the one thing the observation
# is required not to be (constitution §5).
BUGGY_STORE_BASE_URL="http://127.0.0.1:${BUGGY_STORE_PORT}"

export PORT BUGGY_STORE_PORT BUGGY_STORE_DATABASE BUGGY_STORE_BASE_URL
export HARNESS_DATABASE_PATH HARNESS_ARTIFACT_ROOT HARNESS_STATIC_ROOT

mkdir -p "${HARNESS_ARTIFACT_ROOT}"

# Step 3 of §29.1: start the standalone store. Its own lifespan runs the ordered
# migration runner (ADR-0003) and seeds each workspace on first contact, so a
# redeploy against an existing volume is a no-op and a redeploy against an empty
# one comes up seeded.
/opt/store/bin/buggy-store &
store_pid=$!

# If the store dies the harness stays up and /demo/api/v1 answers 502. That is
# the honest failure: an observation the harness cannot make must surface as a
# non-pass, never as a pass by omission (constitution §5).
trap 'kill -TERM "${store_pid}" 2>/dev/null || true' TERM INT

# One worker, not a default (§29.1, ADR-0003). See the Dockerfile header.
exec /opt/harness/bin/uvicorn \
    actionwitness_service.api.app:create_app \
    --factory \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips "${HARNESS_TRUSTED_PROXIES:-127.0.0.1}"
