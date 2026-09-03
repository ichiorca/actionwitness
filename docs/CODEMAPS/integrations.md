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

## `integrations/self_target` — ActionWitness as its own target (§12.20)

The dogfooding adapter. Drives and observes the harness itself, over `/api/v1`,
holding nothing but an injected HTTP client.

**The dependency list is the safety property.** This distribution declares
`actionwitness-core` and `httpx` and nothing else, so it cannot import a
repository, a service, or the database — FR-171's "no privileged access", made
structural. `tests/architecture/test_import_boundaries.py` gates both the
imports and the manifest. If you find yourself wanting one more dependency here,
that is the signal to fix the public protocol instead.

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `adapter.py` | ~270 | `SelfTargetAdapter`, `DESCRIPTOR`, the tool→route map, `observing()` | `observing()` returns a **new bound instance**; the client is shared, so mutating in place would let two concurrent self runs act on each other's target. An unbound adapter refuses rather than defaulting to the recording workspace. |
| `observation.py` | ~255 | `SelfObservationProvider`, the state projection, `workspace_header` | The projection is a small fixed set of facts, not a dump of the workspace response — otherwise the first cosmetic field added to `GET /workspace` starts failing self runs. `capture()` **refuses**: one workspace identifier is not enough (FR-172). |
| `templates.py` | ~430 | FR-173's built-in self contract pack | Every assertion path must resolve inside `observation.py`'s projection, and every `expected_tools` entry must be a published tool — contract validation checks the second, and only a test checks the first. |
| `tools.py` | ~120 | The six published tool specs and the effect map | `verify_outcome` is **deliberately absent**: a self run that could tell its observed workspace to verify would be driving the machinery recording it. Every schema sets `additionalProperties: false` and none accepts a workspace identifier — see the binding note below. |

The workspace a self run acts on and observes is chosen by the **server**, never
by the agent and never by a tool argument. See
`apps/actionwitness_service/src/actionwitness_service/application/self_witness.py`
for the guards; `docs/ARCHITECTURE.md` section 9 for why.

## `integrations/google_evals` — external evaluator import (Tier 2)

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `reader.py` | 366 | `read_report`, `ImportLimits`, `ReportRejected` | Size is checked **before** parsing (`_check_size`) — the precedent every import path follows. |
| `normalize.py` | 297 | Report → `NormalizedTrial` | Never guesses a binding from position, similarity, or timestamps (FR-091). |
| `live.py` | 249 | Live-evaluator screening and run description | `screen_for_credential_material` refuses an import carrying credentials rather than redacting it — a secret in a persisted artifact is an incident, not a validation failure. This module still calls nothing: it describes a run. |
| `generation.py` | ~430 | `LiveVariantClient` — the only code in the repo that calls a model. FR-100's *generate* step over Google's Gemini REST API | **The one object that ever holds the credential.** It goes in the `x-goog-api-key` header, never the URL, and no failure path forwards `str(exc)` or a vendor response body. The client is injected (ADR-0001), the answer is size-capped before parsing, and the *authored* payload is screened for credential keys **before** its shape is checked — a closed model would otherwise report an incident as a typo. Nothing here approves or freezes; the return type is deliberately not `IntentVariant`. |
| `pins.py` | 63 | The pinned evaluator version | `tests/architecture/test_evaluator_pin.py` guards it; see ADR-0005 and the drafted upstream issue that accompanies it. |

## `integrations/shopify` — two targets, read carefully

**One distribution, two features, and they are not the same target.**

* `adapter.py` + `observation.py` + `templates.py` — the **cart proof**
  (`shopify-development-store`, §12.12): one *configured* development store the
  project is authorized on, observed through the paired theme bridge.
* `audit.py` + `pack.py` — the **external-surface audit** (`external-audit`,
  §12.17): an origin the operator asserts authorization for, with a different
  consent story.

They share `audit.py`'s `cart.js` normalizer and its exact-origin rule, on
purpose: two ideas of what a cart is worth is how the two paths would drift.

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `audit.py` | ~275 | `ExternalAuditAdapter` (`ExternalTargetAdapter`: `normalize` + `validate_origin`, and no `execute` — the absence is the interface), plus the **shared** `normalize_cart`, `cart_amount`, `require_exact_origin`, `MAX_CART_PAYLOAD_BYTES`, `PROVENANCE` | Imported by `apps/actionwitness_service/src/actionwitness_service/application/audit_workflow.py` *and* by the cart-proof modules. `normalize` **checks** provenance rather than recording it: a caller that could label its own payload could label a tool result a session read. |
| `pack.py` | 222 | The ten-tool audit contract packs, `match_pack`, `NEVER_INVOKED_TOOLS` | `match_pack` returns every match and picks none — FR-161 makes selection the operator's. |
| `adapter.py` | ~180 | `ShopifyAdapter`, `TARGET_ID`, `TARGET_TYPE`, `DESCRIPTOR` | **`tool_specs()` is empty on purpose.** FR-114/AC-18 keep trajectory `not_evaluated`, and §10.2 then refuses any contract for this target that names a tool at all — so a `forbidden_tool: proceed_to_checkout` cannot be added "to be safe". No `execute`, no HTTP client, no Shopify credential (FR-118). |
| `observation.py` | ~310 | `ShopifyCartObservationProvider` (`shopify_cart_state`), `project_cart`, `require_within_payload_bound` | Calls `normalize_cart`; never copies it. `capture()` **refuses** — FR-112 puts the read in the shopper's browser session. `page.checkout_navigation_observed` is **required**: defaulting it to `false` would make "the bridge did not look" indistinguishable from "nothing navigated". There is no `order` key, because order state needs the Admin credential FR-118 forbids. |
| `templates.py` | ~290 | `shopify_exact_cart` (§13.5, FR-108), `expand`, `TEMPLATES` | Omits `expected_tools` (FR-114). `expand` takes `variant_id` and `expected_currency` from **server** configuration via `ShopifyAdapter.contract_parameters()`, spread last by the composition root — never from a request body; they are deliberately not template `parameters`, so the generic §25.2 form works with the template while a *caller* supplying either is refused by name (`extra="forbid"` at the route, required-from-configuration here). |

Configuring all four `SHOPIFY_*` variables now turns the module on:
`apps/actionwitness_service/src/actionwitness_service/application/adapter_registry.py`
registers the target from those settings, and `_SHOPIFY_ADAPTER_SHIPPED` in
`apps/actionwitness_service/src/actionwitness_service/config.py` is the switch that keeps `modules` and
`capabilities` from contradicting each other — flip it back if the registration
is ever removed. Every task in `specs/011-shopify-cart-proof/tasks.md` remains
unticked while AC-17 is unproven, a state
`tests/integration/test_010_exit_gate.py` actively enforces; that gate is about
the *live* proof against a real store, not about this code.

`shopify_bridge/` at the repo root is **no longer a placeholder**: it holds the
checked-in theme bridge (`shopify_bridge/actionwitness-bridge.js`), its theme
snippet, and hand-written types. It is plain, unbuilt JavaScript that runs at
the *storefront's* origin, so it is the one documented place outside
`apps/actionwitness_service/frontend/src/webmcp/adapter.ts` that touches
`modelContext` — a different document, in a
different browsing context, that the React rule has no reach into. It is
excluded from the release image (it is installed on a store, not served by the
harness), its logic is exercised by the frontend's
`apps/actionwitness_service/frontend/src/shopifyBridge.test.ts`, and
`tests/architecture/test_shopify_bridge_artifact.py` holds the properties that
must be true of the file itself: no storage, no checkout, no navigation.

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
