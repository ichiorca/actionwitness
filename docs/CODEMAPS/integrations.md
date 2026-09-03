# integrations and the demo target

Where target-specific vocabulary is allowed to exist. An integration implements
the public protocols in `packages/actionwitness_core/src/actionwitness_core/ports/__init__.py`;
the core never imports one, and the generic UI never imports one.

## `integrations/buggy_store` — the demo target adapter

Bridges core protocols to the storefront's versioned HTTP API.

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `adapter.py` | 513 | `ManagedTargetAdapter`: tool dispatch, descriptor, scenario reporting | Talks to `/demo/api/v1` over the lifespan-owned client (ADR-0001). Never imports `buggy_store` itself — the boundary is HTTP, on purpose. |
| `observation.py` | 122 | `ObservationProvider`: the independent state read | **This is the second channel.** It must never be satisfied from a tool response; that is the whole product. |
| `templates.py` | 740 | The built-in contract templates, as data | Largest here, and deliberately data rather than code — the integration understands what `target.cart.total` means, so re-authoring the templates in the service would put commerce vocabulary in a target-neutral layer. |
| `tools.py` | 217 | Tool specs the adapter publishes | |
| `environments.py` | 64 | Environment/profile selection | |

## `integrations/google_evals` — external evaluator import (Tier 2)

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `reader.py` | 366 | `read_report`, `ImportLimits`, `ReportRejected` | Size is checked **before** parsing (`_check_size`) — the precedent every import path follows. |
| `normalize.py` | 297 | Report → `NormalizedTrial` | Never guesses a binding from position, similarity, or timestamps (FR-091). |
| `live.py` | 249 | Live-evaluator screening | `screen_for_credential_material` refuses an import carrying credentials rather than redacting it — a secret in a persisted artifact is an incident, not a validation failure. |
| `pins.py` | 63 | The pinned evaluator version | `tests/architecture/test_evaluator_pin.py` guards it; see ADR-0005 and the drafted upstream issue that accompanies it. |

## `integrations/shopify` — split status, read carefully

**Half of this package is live and half is a stub.** The distinction matters.

| File | Lines | Status | Notes |
|---|---|---|---|
| `audit.py` | 253 | **Live** | `ExternalAuditAdapter` (`ExternalTargetAdapter`: `normalize` + `validate_origin`, and no `execute` — the absence is the interface), `normalize_cart`, `MAX_CART_PAYLOAD_BYTES`, `PROVENANCE`. Imported by `apps/actionwitness_service/src/actionwitness_service/application/audit_workflow.py`. `normalize` **checks** provenance rather than recording it: a caller that could label its own payload could label a tool result a session read. |
| `pack.py` | 222 | **Live** | The ten-tool contract packs, `match_pack`, `NEVER_INVOKED_TOOLS`. `match_pack` returns every match and picks none — FR-161 makes selection the operator's. |
| `adapter.py` | 4 | **Stub** | Docstring only. The dev-store target is not built. |
| `observation.py` | 5 | **Stub** | Docstring only. |

`apps/actionwitness_service/src/actionwitness_service/api/routes/shopify.py` is an
empty, unmounted router, and every task in
`specs/011-shopify-cart-proof/tasks.md` is unticked — a state
`tests/integration/test_010_exit_gate.py` actively enforces. Configuring
`SHOPIFY_*` reports `disabled` with a reason naming the build; it does not turn a
feature on.

`shopify_bridge/` at the repo root is a README-only placeholder.

## `examples/buggy_store` — the demo storefront

An independently runnable target that **imports nothing from the assurance
stack** — enforced by `tests/architecture/test_import_boundaries.py` and by
`scripts/store_only_isolation.py`, which installs it alone into a fresh
virtualenv.

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `service.py` | 781 | Cart, discount, checkout logic | Where the deliberate bugs live. |
| `repository.py` | 517 | Its own SQLite persistence | Separate database from the harness. |
| `api.py` | 353 | `/demo/api/v1` | The only route between harness and target. |
| `failure_injection.py` | 172 | Fault profiles, including the false-success one | The demo's whole point: a tool that reports success and changes nothing. |
| `models.py` · `money.py` · `catalog.py` · `confirmations.py` · `errors.py` · `migrations.py` | 272 · 72 · 118 · 153 · 174 · 173 | Domain, money, catalogue, its own confirmation flow, errors, schema | Money is `Decimal` here too. |
| `frontend/src/` (`App.tsx` 341 · `api.ts` 311 · `styles.css` 328 · `main.tsx` 48, plus `App.test.tsx`) | ~1,490 | The storefront's **own** React UI — the page a person uses with no agent, no harness, no WebMCP (AC-09) | Deliberately shares **nothing** with the harness frontend, its stylesheet included: §26.7 requires this app to run with the assurance packages absent, and a shared design system would be exactly the dependency it must not have. |

## Adding an adapter

Implement the protocols, register in `apps/actionwitness_service/src/actionwitness_service/application/adapter_registry.py`, and keep
the vocabulary inside the integration. The README's "Writing an adapter" section
is the walkthrough; `tests/adapters/` holds the conformance suite, including a
non-commerce fake that exists to prove the protocols are not cart-shaped.
