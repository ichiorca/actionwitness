# Development guide

## Requirements

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Node.js and npm
- Docker for the release-image path
- A WebMCP-capable browser only for the live agent path; all other workflows and
  tests remain usable without it

## Python workspace

From the repository root:

```bash
uv sync
uv run pytest -q
uv run pytest tests/architecture -q
uv run ruff format --check .
uv run ruff check .
```

Additional executable boundaries:

| Command | Purpose |
|---|---|
| `uv run python scripts/core_only_isolation.py` | Install and test only `actionwitness-core` in a clean environment |
| `uv run python scripts/store_only_isolation.py` | Install and exercise only the Buggy Store in a clean environment |
| `uv run python scripts/scan_for_secrets.py` | Scan tracked files for secret-shaped values |
| `uv run python -m actionwitness_service.api.registry_export` | Regenerate the shared API-name registry |
| `uv run buggy-store` | Run the standalone target on port 8001 |

## Frontend workspace

From `apps/actionwitness_service/frontend`:

```bash
npm ci
npm run typecheck
npm run lint
npm test
npm run build
```

`npm run build` bundles the application; it does not replace the strict
`npm run typecheck` or lint gates.

The optional browser lane is documented under
`apps/actionwitness_service/frontend/e2e/`. It uses Playwright against the
composed service and target. The browser's WebMCP host object is substituted in
automation because stock Chromium does not expose the experimental surface by default.

## Buggy Store frontend

From `examples/buggy_store/frontend`:

```bash
npm ci
npm run typecheck
npm run lint
npm test
npm run build
npm run dev
```

The store is intentionally independent of the assurance application and has no
direct WebMCP registration.

## Run all three development processes

After `uv sync`, use separate terminals:

```bash
# target
uv run buggy-store

# API and application service
uv run uvicorn actionwitness_service.api.app:create_app --factory --port 8000

# React workspace
cd apps/actionwitness_service/frontend
npm ci
npm run dev
```

## Browser support

The browser adapter resolves `document.modelContext` first and
`navigator.modelContext` as a fallback, by capability rather than user-agent
string. Direct access to either object is forbidden outside
`apps/actionwitness_service/frontend/src/webmcp/adapter.ts` and enforced by an
architecture test.

The pinned challenge browser path uses Chrome with WebMCP testing enabled. The
workspace capability bar reports whether the API is available and which host
object was resolved. If unavailable, do not infer browser support from version or
branding; use the human controls or the deterministic tests.

## Repository boundaries

```text
packages/actionwitness_core       deterministic, synchronous, target-neutral core
apps/actionwitness_service        FastAPI application, persistence, orchestration, UI
integrations/buggy_store          target adapter and independent observer
integrations/google_evals         imported webmcp-evals report adapter
integrations/self_target          built-in self target driven through the public API
integrations/shopify              optional development-store and audit integration
examples/buggy_store              standalone failure-injectable target
tests                             architecture, unit, integration, adapter, eval lanes
```

Every importing distribution declares its own dependencies. The core imports no
FastAPI, HTTPX, browser, environment, commerce, demo, or integration package.
Architecture tests enforce those edges.

## Adding a target adapter

Implement the public protocols exposed by `actionwitness_core`:

- execution through the recorded target-tool boundary;
- independent authoritative observation;
- fixture reset and scenario description when the target supports replay;
- stable source classification and bounded target identity.

Do not add target branches to the core or generic UI. A tool response and an
authoritative observation must remain distinct model types and stored records.

## More orientation

- [Architecture](ARCHITECTURE.md)
- [Code maps](CODEMAPS/)
- [Deployment](DEPLOYMENT.md)
- [Security policy](../SECURITY.md)
- [Submission evidence](SUBMISSION_EVIDENCE.md)
