# tests — lanes and gates

`--strict-markers` is on, so a marker that is not declared in `pyproject.toml` is
an error rather than a silently unselected test. Adding a lane means adding its
marker first. `filterwarnings = ["error"]`: a warning fails the suite unless it is
asserted with `pytest.warns` or ignored with a stated reason.

## Lanes

| Directory | Files | Marker | What belongs here |
|---|---|---|---|
| `tests/unit` | 34 | `unit` | Engine operators, policies, hashing, canonicalization, classification. Pure, fast. `test_live_variant_client.py` is here rather than in `integration` because it drives the model client over an `httpx.MockTransport` — no app, no database, and no route to Google. |
| `tests/integration` | 86 | `integration` | Run lifecycle, confirmation, replay — **through the real app** with a temp database. The largest lane and where journeys belong. |
| `tests/architecture` | 16 | `architecture` | Forbidden-import, layering, and repository-reference gates. See below. |
| `tests/adapters` | 8 | `adapters` | Adapter protocol conformance, including a non-commerce fake that proves the protocols are not cart-shaped. |
| `tests/contracts` | 5 | `contracts` | Outcome-contract parsing, validation, limits. |
| `tests/evals` · `tests/benchmarks` | 1 · 1 | `evals` · `benchmarks` | Guard files only; the real tests live in `integration`/`unit`. |
| `tests/guidance` | 3 | `guidance` | Human–agent guidance states (§12.13). `test_guidance_derivation.py` checks the pure projection; `test_guidance_reachability.py` checks the server can actually reach each phase, which totality alone does not prove. |
| `tests/shopify` | 4 | `shopify` | Fail-closed configuration parsing, plus §15.7's bridge driven **over HTTP only** — no test here imports an application module, because what was missing was the door rather than the logic. `test_shopify_bridge_routes.py` holds the trial that works; `test_shopify_bridge_refusals.py` holds every way one is refused, and keeps a *refusal* (nothing captured) apart from a *failed verdict* (real evidence of a wrong cart). `test_shopify_contract_template.py` holds FR-023's generic-path door: the template listed and instantiable when the module is on, absent when off, and the locked variant/currency refused from any request body. `conftest.py` hangs its helpers off one `trial` fixture because `tests/` is not a package. |
| `tests/browser` | 1 | `browser` | Manual smoke checklists — **never automated in CI** (§26.4). |

The Playwright lane lives separately at
`apps/actionwitness_service/frontend/e2e/` (14 spec files, 78 tests) and is
deliberately outside every release gate — §26 makes it conditional. It is the
only layer exercising real WebMCP, so run it by hand before a release. The
workspace splits Workflow from Administration behind a left rail, and a hidden
region leaves the accessibility tree — so specs switch views through the page
object's `showWorkflow()` / `showAdministration()` before touching a panel on
the other view.

## The gates — `tests/architecture`

These are the tests that fail a build for reasons a reviewer would otherwise have
to catch by eye.

| File | Refuses to let you |
|---|---|
| `test_import_boundaries.py` | Import an app, integration, demo, or commerce module into `actionwitness_core`; or import the assurance stack into the demo target. AST-based, not grep. |
| `test_webmcp_adapter_isolation.py` | Touch the browser tool API outside `apps/actionwitness_service/frontend/src/webmcp/adapter.ts` — either host object, since the adapter resolves `document` or `navigator`. Comments are stripped first (the gate is about access, not mentions); the allowlist carries a reason per entry. |
| `test_audit_guardrails.py` | Give an audit module a network client, or an audit request model a collection of origins. |
| `test_harness_tool_surface.py` | Change the published harness tool set without noticing. |
| `test_product_copy_claims.py` | Restate a third-party report as our own finding, drop an attribution or date, or promise more than an audit delivers. |
| `test_documentation_references.py` | Cite a superseded spec version, omit a required command from the README, or point a reader at a path a clone does not carry. |
| `test_readme_commands.py` | Document a command that does not exist. |
| `test_codemaps.py` | Let these maps name a path that does not exist, or drift out of the routing table. |
| `test_core_only_install.py` · `test_store_only_install.py` | Claim independent installability without proving it — the paired `scripts/*_isolation.py` build a fresh virtualenv and install exactly one distribution. |
| `test_release_artifact_hygiene.py` | Ship secrets, local paths, build debris, lose the single-worker pin — or ship an image whose harness venv omits an integration the service imports (`google_evals` broke only in the image, once; `shopify` ships too, so enabling the audit module on the artifact is not one env var away from the same 500). |
| `test_bundle_shape.py` | Ship a bundle needing a CSP directive the policy does not grant. |
| `test_evaluator_pin.py` | Drift off the pinned evaluator version. |
| `test_test_lanes.py` | Put a test in a lane whose marker it does not carry. |
| `test_exit_gate_traceability.py` | Claim an exit criterion with nothing mapped to it. |
| `test_frontend_command_surface.py` | Let the documented frontend commands and the real scripts disagree. |

## Conventions

- **Arrange–Act–Assert**, and the test name states the behavior, not the method.
- **Assert through public entry points.** Integration tests drive the real
  FastAPI app against a temp database; they do not reach into services.
- **Independent oracle.** A test that proves a mutation happened reads the
  *target's own* state, never the harness's response. This is the product's
  thesis applied to its own suite.
- **Counterfactuals are mandatory** for any accusation. A classifier that flags
  everything passes a one-sided test and is worthless.
- **No wall-clock, no network, no order dependence.** Concurrency tests use real
  `asyncio.Event` barriers rather than sleeps; fixtures use `tmp_path`. The
  stray `*.sqlite3` files at the repo root are gitignored dev debris, not
  fixtures.
- **Never weaken an assertion to get green.** If a test's premise genuinely
  changed, change the premise explicitly and say why in the test.

## Running them

```bash
uv run pytest -q                      # everything
uv run pytest tests/architecture -q   # the gates alone (fast)
uv run pytest -m unit -q              # one lane
```
