# tests — lanes and gates

`--strict-markers` is on, so a marker that is not declared in `pyproject.toml` is
an error rather than a silently unselected test. Adding a lane means adding its
marker first. `filterwarnings = ["error"]`: a warning fails the suite unless it is
asserted with `pytest.warns` or ignored with a stated reason.

## Lanes

| Directory | Files | Marker | What belongs here |
|---|---|---|---|
| `tests/unit` | 30 | `unit` | Engine operators, policies, hashing, canonicalization, classification. Pure, fast. |
| `tests/integration` | 81 | `integration` | Run lifecycle, confirmation, replay — **through the real app** with a temp database. The largest lane and where journeys belong. |
| `tests/architecture` | 16 | `architecture` | Forbidden-import, layering, and repository-reference gates. See below. |
| `tests/adapters` | 8 | `adapters` | Adapter protocol conformance, including a non-commerce fake that proves the protocols are not cart-shaped. |
| `tests/contracts` | 5 | `contracts` | Outcome-contract parsing, validation, limits. |
| `tests/evals` · `tests/benchmarks` | 1 · 1 | `evals` · `benchmarks` | Guard files only; the real tests live in `integration`/`unit`. |
| `tests/guidance` | 2 | `guidance` | Human–agent guidance states (§12.13). |
| `tests/shopify` | 1 | `shopify` | Fail-closed configuration parsing for the unshipped module. |
| `tests/browser` | 1 | `browser` | Manual smoke checklists — **never automated in CI** (§26.4). |

The Playwright lane lives separately at
`apps/actionwitness_service/frontend/e2e/` (14 specs, 78 assertions) and is
deliberately outside every release gate — §26 makes it conditional. It is the
only layer exercising real WebMCP, so run it by hand before a release.

## The gates — `tests/architecture`

These are the tests that fail a build for reasons a reviewer would otherwise have
to catch by eye.

| File | Refuses to let you |
|---|---|
| `test_import_boundaries.py` | Import an app, integration, demo, or commerce module into `actionwitness_core`; or import the assurance stack into the demo target. AST-based, not grep. |
| `test_webmcp_adapter_isolation.py` | Touch `document.modelContext` outside `apps/actionwitness_service/frontend/src/webmcp/adapter.ts`. |
| `test_audit_guardrails.py` | Give an audit module a network client, or an audit request model a collection of origins. |
| `test_harness_tool_surface.py` | Change the published harness tool set without noticing. |
| `test_product_copy_claims.py` | Restate a third-party report as our own finding, drop an attribution or date, or promise more than an audit delivers. |
| `test_documentation_references.py` | Cite a superseded spec version, or omit a required command from the README. |
| `test_readme_commands.py` | Document a command that does not exist. |
| `test_codemaps.py` | Let these maps name a path that does not exist, or drift out of the routing table. |
| `test_adr_records.py` | Land an architectural decision with no ADR, or an ADR missing from the docket. |
| `test_core_only_install.py` · `test_store_only_install.py` | Claim independent installability without proving it — the paired `scripts/*_isolation.py` build a fresh virtualenv and install exactly one distribution. |
| `test_release_artifact_hygiene.py` | Ship secrets, local paths, build debris, or lose the single-worker pin. |
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
