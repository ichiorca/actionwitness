# service · api — `apps/actionwitness_service/src/actionwitness_service/api`

> **Paths below are relative to** `apps/actionwitness_service/src/actionwitness_service/api`.

The HTTP boundary. Everything crossing it is untrusted: bodies, headers, path
parameters, and cookies. Routes validate with Pydantic models that forbid unknown
fields, then hand off to `application/` — they do not orchestrate.

**Start at** `app.py` (composition root) and `errors.py` (the error contract).

## Composition and contract

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `app.py` | 606 | `create_app`, lifespan, `/healthz`, exception handlers, router mounting | The one place the environment is read. Migrations run in the lifespan before the first request — never lazily. `/healthz` probes the database **live** (a cached startup value answers `ok` for a database that has since vanished) and returns 503 when the database is unreadable or a production deployment has no valid public origin. |
| `errors.py` | 484 | `ApiError`, closed `ApiErrorCode` registry, `error_from_core` | Each code's HTTP status and **retryability** live in the registry, so a call site cannot widen retryability. A 500 never carries a traceback to the client. Adding a code means adding a registry row. |
| `dependencies.py` | 104 | `WorkspaceDependency`, `DatabaseDependency`, `LocksDependency`, `ArtifactsDependency`, `SettingsDependency`, `RegistryDependency` | `DatabaseDependency` exposes raw `fetch_one`/`execute`. Resist it: SQL belongs in `application/`, and no route currently contains any. |
| `composition.py` | 206 | Static mounts and the `/demo` proxy (§29.1) | The proxy reaches the store over the same lifespan-owned client the adapter uses, so tests exercise the composed path. |

## Middleware and policy

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `middleware.py` | 348 | Origin policy, rate limiting, workspace cookie | Exemptions match on **segment boundaries**, not string prefixes — a bare `startswith` made `/healthz-anything` unmetered. Middleware order is reverse-registration; the logger is outermost so refusals are logged. |
| `origins.py` | 163 | `OriginPolicy`, origin normalization | Equality after normalization, never prefix or suffix. Input that cannot be parsed at all — out-of-range port, unclosed IPv6 authority — must **refuse**, not raise: `urlsplit` is lazy, so `.port` throws long after the split succeeded. `scoped_origins` is §20.1's one exception — one extra origin bound to one path prefix, matched on segment boundaries — and `app.py` populates it only when a Shopify store is configured. |
| `security_headers.py` | 132 | CSP and friends | Paired with `tests/architecture/test_bundle_shape.py`, which fails if the bundle needs a directive the policy does not grant. |
| `event_stream.py` | 296 | SSE run timeline | Since-cursor paging; drains fully before stopping on a terminal status. Re-polls the database on its own interval — see the pooling note in [`platform.md`](platform.md). |
| `registry_export.py` | 115 | Generated error/enum registry for the frontend | Regenerate rather than hand-edit the frontend copy. |

## Routes

One module per domain, all mounted under `/api/v1`.

| File | Lines | Surface | Watch for |
|---|---|---|---|
| `routes/runs.py` | 572 | Arm, invoke, confirm, verify, events, report | The invocation route is the recorded-identity seam: the browser calls a WebMCP tool, the server records the invocation. |
| `routes/benchmarks.py` | ~1130 | Suites, imports, frozen variants, bindings, finalize, replay, repeated trials, correlation | Import and finalize are **three-phase**: read → write artifact with no transaction open → commit (ADR-0003). Do not fold the write back inside the transaction. `POST /frozen-variants` runs FR-100's whole sequence — validate → screen → approve → freeze — inside one transaction because every step is pure CPU; a Pydantic refusal there forwards `msg` only, never the rejected variant text. `POST /intent-variants` is the *generate* half and is a separate endpoint on purpose — it persists nothing and approves nothing, and its body has no `reviewer` field, so one call can never both write the variants and approve them. Its model client is built and closed inside the request (`_live_proposer`), reading the credential from the environment at the moment of use; a test injects `app.state.live_variant_client`. `POST /repeated-trials` and `POST /replay` both run **outside** the workspace lock: each repetition opens its own transactions and mints its own eval workspace, and ADR-0003 forbids holding a lock across that wait. `GET /correlation` recomputes on every request — until finalization these numbers are a view, and a cached one goes stale the moment another repetition lands. |
| `routes/audits.py` | 349 | `POST /audits`, `/current`, `/packs`, `/current/evidence`, `/current/report`, `/current/cancel` | No endpoint accepts a collection of origins — a list is a scan queue with a friendlier name, and `tests/architecture/test_audit_guardrails.py` asserts the shape. The evidence body is size-capped **before** parsing. |
| `routes/workspace.py` | 359 | `GET /workspace` (whose payload carries `capabilities`, `modules`, and the adapter-published `supported_scenario_modes` / `supported_fault_profiles`), `POST /reset`, `PUT /scenario-mode`, `PUT /failure-profile` | `capabilities` (registered targets) and `modules` (configured modules) must agree; they once disagreed about Shopify. The UI offers exactly the modes/profiles the payload names — nothing client-side invents one. |
| `routes/evals.py` | 239 | Regression cases and runs | |
| `routes/contracts.py` | 171 | Templates, instantiate, select | Selection sets contract **and** target in one `UPDATE` (FR-024). |
| `routes/shopify.py` | 630 | §15.7's five endpoints: create, redeem, `observations/before`, verify, status | **Two callers, two authorizations.** The two UI routes take the workspace cookie; the three bridge routes take a bearer credential plus the exact configured `Origin` and accept no workspace id from anywhere (FR-006). Mounted only when `settings.shopify is not None` — unlike `audits.py` it is absent rather than refusing, because 009-T12's cut-hygiene gate reads mounted paths. Bridge handlers catch `ApiError` and re-emit it so a refusal carries the CORS headers; `create_app`'s handler cannot add them. The cart body is size-capped **before** parsing (FR-112). `_REPORT_PATH` duplicates `API_PREFIX` because importing it here would be a cycle — a test fetches the path to keep the duplicate honest. |
