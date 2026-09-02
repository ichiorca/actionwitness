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

# One worker, not a default (§29.1, ADR-0003). See the Dockerfile header.
#
# Started in the background rather than through `exec`, so this shell stays
# PID 1. An earlier version installed the trap below and then `exec`ed here,
# which replaced the shell — traps belong to the shell, so the handler was
# destroyed microseconds after being installed and could never fire. The store
# was then a child of uvicorn, a process with no supervision logic: `docker
# stop` signalled PID 1 only, the store received nothing, and the kernel killed
# it when the namespace tore down. Keeping the shell means signals reach both
# processes and both are reaped here.
/opt/harness/bin/uvicorn \
    actionwitness_service.api.app:create_app \
    --factory \
    --host 0.0.0.0 \
    --port "${PORT}" \
    --workers 1 \
    --proxy-headers \
    --forwarded-allow-ips "${HARNESS_TRUSTED_PROXIES:-127.0.0.1}" &
harness_pid=$!

# Forwarded to both, so each gets to run its own shutdown: uvicorn drains its
# requests, and the store closes its SQLite connection instead of being killed
# with the namespace.
terminate() {
    kill -TERM "${harness_pid}" 2>/dev/null || true
    kill -TERM "${store_pid}" 2>/dev/null || true
}
trap terminate TERM INT

# `wait` returns as soon as a trapped signal has been handled, while the process
# it was waiting for is still shutting down — so it is retried until the harness
# has genuinely exited, and its real status is what this container exits with.
harness_status=0
while kill -0 "${harness_pid}" 2>/dev/null; do
    wait "${harness_pid}" || harness_status=$?
done

# The harness is gone, so the store has nothing left to serve. If the *store*
# died first the harness was deliberately left running — an observation the
# harness cannot make must surface as a non-pass through a 502 on
# /demo/api/v1, never as a pass by omission (constitution §5) — so this is
# reached only on the way out.
kill -TERM "${store_pid}" 2>/dev/null || true
wait "${store_pid}" 2>/dev/null || true

exit "${harness_status}"
