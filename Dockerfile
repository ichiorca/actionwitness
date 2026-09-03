# syntax=docker/dockerfile:1
#
# Release image — one Render web service, one origin (spec §29.1, 009-T1/T2).
#
# Four stages in the order §29.1 names them:
#   1. build the harness and Buggy Store frontend assets INDEPENDENTLY;
#   2. install actionwitness_core + actionwitness_service + enabled integrations
#      as separate distributions;
#   3. install the standalone Buggy Store package — into its OWN virtualenv;
#   4. copy each application's assets into its own static directory.
#
# Why two virtualenvs rather than one. §26.7 and the 003 exit gate require the
# Buggy Store to install and run with every assurance package absent. That claim
# is cheap to make in CI and easy to quietly break in the artifact: a single
# shared site-packages would put `actionwitness_core` one `import` away from the
# demo target, and the isolation would hold only because nobody had written the
# import yet. Separate prefixes make the boundary a property of the image.
#
# Why one worker. ADR-0003's `BEGIN IMMEDIATE` + busy-timeout model assumes a
# single writer process; §29.1 says "one service instance and one Uvicorn
# worker". `--workers 1` below is load-bearing and must not be "tuned up" —
# a second worker introduces a second writer against the same SQLite file and
# the lock model has no answer for it.


# --- stage 1: frontend assets ------------------------------------------------
# Both applications are built here but never share a node_modules: §29.1 builds
# them independently, and the storefront must not acquire a harness dependency
# by accident (AC-09 — it is the path that works when WebMCP is absent).
FROM node:22-slim AS frontend-build
WORKDIR /build

# Manifests first, so editing a component does not invalidate the install layer.
COPY apps/actionwitness_service/frontend/package.json \
     apps/actionwitness_service/frontend/package-lock.json \
     ./harness/
RUN cd harness && npm ci

COPY examples/buggy_store/frontend/package.json \
     examples/buggy_store/frontend/package-lock.json \
     ./store/
RUN cd store && npm ci

COPY apps/actionwitness_service/frontend/ ./harness/
RUN cd harness && npm run build \
 && rm -f dist/spike.html dist/assets/spike-*.js
# `spike.html` is the ADR-0002 decision harness, a second entry point that
# registers WebMCP tools of its own. `vite.config.ts` keeps it out of the product
# surface by making it a separate page; shipping that page would put it back.
# Removed here rather than by editing the build inputs, so the spike stays
# runnable with `npm run dev` where it belongs.

COPY examples/buggy_store/frontend/ ./store/
# The storefront is served under /demo in the composed deployment, so its asset
# URLs have to be built for that prefix. Passed on the command line rather than
# written into vite.config.ts: the same source still builds at / for `npm run
# dev` and for anyone running the storefront standalone.
RUN cd store && npm run build -- --base=/demo/


# --- stage 2: python dependency install --------------------------------------
FROM python:3.12-slim AS python-build

# Pinned to the version the workspace resolves with locally. An unpinned `uv`
# would silently change resolution behaviour between builds, which is the whole
# thing `--frozen` exists to prevent.
COPY --from=ghcr.io/astral-sh/uv:0.11.18 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src

# The workspace manifests and lockfile, then the sources. `--frozen` fails the
# build if uv.lock disagrees with the manifests rather than silently re-resolving.
COPY pyproject.toml uv.lock ./
COPY packages/actionwitness_core/pyproject.toml   ./packages/actionwitness_core/
COPY apps/actionwitness_service/pyproject.toml    ./apps/actionwitness_service/
COPY integrations/buggy_store/pyproject.toml      ./integrations/buggy_store/
COPY integrations/google_evals/pyproject.toml     ./integrations/google_evals/
COPY integrations/self_target/pyproject.toml      ./integrations/self_target/
COPY integrations/shopify/pyproject.toml          ./integrations/shopify/
COPY examples/buggy_store/pyproject.toml          ./examples/buggy_store/

COPY packages/actionwitness_core/src   ./packages/actionwitness_core/src
COPY apps/actionwitness_service/src    ./apps/actionwitness_service/src
COPY integrations/buggy_store/src      ./integrations/buggy_store/src
COPY integrations/google_evals/src     ./integrations/google_evals/src
COPY integrations/self_target/src      ./integrations/self_target/src
COPY integrations/shopify/src          ./integrations/shopify/src
COPY examples/buggy_store/src          ./examples/buggy_store/src
COPY README.md LICENSE ./

# §29.1 step 2 — the harness prefix: core, service, and the enabled integration,
# each as its own distribution.
#
# Third-party versions come from uv.lock via `uv export --frozen`, so the image
# resolves to exactly the tree the test suite ran against; `--no-dev` keeps
# pytest, ruff, and the rest of the development group out of the artifact. The
# workspace members are then installed `--no-deps` from their own manifests,
# which is what makes them three distributions rather than one flattened
# environment.
RUN uv export --frozen --no-dev --no-emit-workspace --no-hashes \
        --format requirements.txt \
        --package actionwitness-service \
        --package actionwitness-integration-buggy-store \
        --package actionwitness-integration-google-evals \
        --package actionwitness-integration-self-target \
        --package actionwitness-integration-shopify \
        -o /tmp/harness-requirements.txt \
 && uv venv /opt/harness \
 && VIRTUAL_ENV=/opt/harness uv pip install -r /tmp/harness-requirements.txt \
 && VIRTUAL_ENV=/opt/harness uv pip install --no-deps \
        ./packages/actionwitness_core \
        ./apps/actionwitness_service \
        ./integrations/buggy_store \
        ./integrations/google_evals \
        ./integrations/self_target \
        ./integrations/shopify

# §29.1 step 3 — the store prefix. Resolved from its own manifest alone, so the
# environment that ends up in the image is the same one
# `scripts/store_only_isolation.py` asserts in CI: no actionwitness package, and
# not even httpx, which reaches the harness only through the integration.
RUN uv export --frozen --no-dev --no-emit-workspace --no-hashes \
        --format requirements.txt \
        --package buggy-store \
        -o /tmp/store-requirements.txt \
 && uv venv /opt/store \
 && VIRTUAL_ENV=/opt/store uv pip install -r /tmp/store-requirements.txt \
 && VIRTUAL_ENV=/opt/store uv pip install --no-deps ./examples/buggy_store


# --- stage 3: runtime ---------------------------------------------------------
FROM python:3.12-slim AS runtime

# Non-root: nothing in this image needs to write outside /data, and a process
# that cannot write the image cannot rewrite its own application code.
RUN useradd --create-home --uid 10001 witness

WORKDIR /app

COPY --from=python-build /opt/harness /opt/harness
COPY --from=python-build /opt/store   /opt/store

# §29.1 step 4 — each application's assets in its own static directory. Nothing
# is shared: a file under /app/static/demo can only ever be reached at /demo.
COPY --from=frontend-build /build/harness/dist ./static/harness
COPY --from=frontend-build /build/store/dist   ./static/demo

COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Ephemeral is acceptable (§29.1) but the paths must exist and be writable by the
# runtime user whether or not a disk is mounted here.
RUN mkdir -p /data/artifacts && chown -R witness:witness /data /app

ENV HARNESS_STATIC_ROOT=/app/static \
    HARNESS_DATABASE_PATH=/data/actionwitness.sqlite3 \
    HARNESS_ARTIFACT_ROOT=/data/artifacts \
    BUGGY_STORE_DATABASE=/data/buggy-store.sqlite3 \
    BUGGY_STORE_PORT=8001 \
    PORT=8000 \
    PYTHONUNBUFFERED=1

USER witness
EXPOSE 8000

# `/healthz` is the platform health check (render.yaml). It reports liveness
# only and carries no configuration values.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/opt/harness/bin/python", "-c", \
         "import os,urllib.request;urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/healthz\").read()"]

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
