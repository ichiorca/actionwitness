# Browser gate — operator checklist (spec 006 / BUILD_ORDER M5)

> **Scope note (2026-09-04).** This checklist is the browser-gate record for
> the workspace/confirmation milestone and its attestations below are
> historical — do not re-date them. The project has since implemented and
> tested far more; where the rest is verified is summarized in
> [Beyond this gate](#beyond-this-gate--where-the-rest-of-the-product-is-verified)
> at the end, and the living release procedure is
> [`docs/release-checklist.md`](release-checklist.md). (This file was
> previously `docs/tier-1-gate-checklist.md`; specs that cite that name refer
> to this record.)

Two of M5's exit criteria are about a **real browser**, and one milestone
acceptance criterion is about a **deployed URL**. None can be discharged by
`pytest` or `vitest`, and none should be faked by a test that pretends
otherwise:

- `tests/browser/` is an opt-in lane, and the architecture gate
  `test_no_criterion_is_covered_by_a_test_in_a_disabled_lane` forbids an
  exit-gate criterion being covered from it. That gate is correct and is not
  being changed.
- AC-01 ("a user opens the live URL") depends on the deployment M8 builds.
  BUILD_ORDER already carves out AC-10 for the same reason.

So these three are **operator-attested**: run them by hand, record the result
below, and date it. Everything else in the milestone is covered by automated
tests, named in `tests/architecture/test_exit_gate_traceability.py`.

## What is already automated

Do not re-check these by hand; they run on every `uv run pytest -q` and
`npm run test`:

| Criterion | Covered by |
|---|---|
| 3 — one action code across banner, controls, status tool, `next_action`, history | `tests/integration/test_006_exit_gate.py` |
| 4 — no order before approval, consumed once, refusals create none | `tests/integration/test_journey_b.py` |
| 5 — StrictMode, cleanup, polling, error normalization, refresh, a11y | `src/webmcp/adapter.test.ts`, `src/state/useRunTimeline.test.ts`, `src/components/panels.test.tsx` |
| AC-03, AC-04, AC-11, AC-19, AC-20 (API) | `tests/integration/test_005_exit_gate.py` |
| AC-06 (server side), AC-21 (server side) | `tests/integration/test_journey_b.py`, `test_006_exit_gate.py` |

## Before you start

```
uv run pytest -q
uv run pytest tests/architecture -q
cd apps/actionwitness_service/frontend && npm ci && npm run typecheck && npm run lint && npm run test && npm run build
```

Then run the service and the store, and open the workspace.

## Criterion 1 — a compatible browser completes Journeys A and B

Chrome with `#enable-webmcp-testing` enabled (the build ADR-0002 records).

- [ ] The page loads with no credentials and the capability bar says browser
      agent support is **available**, with a non-zero tool count.
- [ ] DevTools shows `get_workspace_status` registered natively, and the
      hook-registered harness tools present.
- [ ] **Journey A**: select `one_mug_save20_no_checkout`, choose `pre_fix` with
      `discount_reported_but_not_applied`, arm, drive `search_catalog`,
      `update_cart`, `apply_discount` **through the agent**, then verify.
      - [ ] The verdict is **failed** with `false_success_or_state_mismatch`.
      - [ ] The report shows trajectory pass, execution pass, business outcome
            fail, model selection `not_evaluated`.
- [ ] Reset, switch to `post_fix`, rerun the same journey.
      - [ ] The verdict passes, and the comparison panel shows the original
            critical classification resolved.
- [ ] **Journey B**: select `confirmed_checkout_only`, arm, `update_cart`, then
      `proceed_to_checkout` through the agent.
      - [ ] The dialog appears and the agent's call is visibly **still waiting**.
      - [ ] No option is preselected; `Tab` cycles within the dialog and does
            not escape it.
      - [ ] Approve once. The order is created, the agent's call resolves, and
            the timeline shows the approval before the completion.
      - [ ] Repeat and **deny** instead: no order, and the run still verifies
            without being marked failed for the refusal.

Attested by: operator (Claude-driven session, operator-directed; three
defects found and fixed during the run — see specs/006-ui-webmcp-confirmation/
plan.md gate-run entry)  Date: 2026-09-01  Build: Chrome 151.0.0.0 stable,
`#enable-webmcp-testing` Enabled

## Criterion 2 — an unsupported browser completes the manual equivalent

Any browser without WebMCP (Firefox or Safari today), or Chrome with the flag
off.

- [ ] The capability bar says browser agent support is **not available** and
      states that every step can still be done by hand — it must not read as an
      error the user has to fix.
- [ ] Journeys A and B both complete using only the page's own controls.
- [ ] The guidance banner names the next action at every step, including on a
      first visit with nothing configured.
- [ ] The confirmation dialog appears, is operable by keyboard, and the decision
      records — the human path does not depend on a tool being registered.

Attested by: operator (Claude-driven session, operator-directed; see the 006
plan's criterion 2 entry for the box-4 qualification and one queued decision)
Date: 2026-09-01  Build: Chrome 151.0.0.0 stable, `#enable-webmcp-testing`
**Disabled** (document.modelContext and navigator.modelContext both absent)

### Manual target actions — the FR-126 documented developer control

The harness lifecycle (configure, arm, verify, decide, reset) has visible
buttons. Target actions have two manual equivalents: the standalone storefront
(share the workspace by setting its `buggy-store.workspace-id` localStorage key
to the harness workspace id), and the recorded invocation route itself — the
same FastAPI operation every tool uses:

    POST /api/v1/runs/{run_id}/target-tools/{tool}:invoke   {"arguments": {...}}
    POST /api/v1/runs/{run_id}/confirmations/{id}/decision  {"decision": "approve_once" | "deny"}

A confirmation raised this way is owned by no tab: the dialog renders its
read-only branch, and the decision goes through the endpoint above. A resumed
protected action repeats the same tool call with the same request_id.

## AC-01 — working live application

Deferred to M8 with AC-10, which BUILD_ORDER already schedules there. Recheck
against the deployed URL once 009 lands.

**009 has landed.** This row is discharged as criterion 3 of
`docs/release-checklist.md`, which carries the full credential-free procedure
(private window, WebMCP flag off as well as on, storefront, and a check that
nothing in the page, network log, or `/healthz` carries a credential). Run it
there, then tick and date this row too.

- [x] The live URL loads without credentials and reports WebMCP support status.

Attested by: operator (Claude-driven session, operator-directed; both support
states verified on https://actionwitness.onrender.com — "not available … every
step below can still be done by hand" in a WebMCP-less fresh Chromium context,
"available — 4 tools registered" in Chrome with the flag on; full procedure and
evidence in docs/release-checklist.md criterion 3)
Date: 2026-09-03 (UTC)  Build: Playwright Chromium (1.62.1) / Chrome stable,
#enable-webmcp-testing Enabled

## Beyond this gate — where the rest of the product is verified

The later feature set is implemented and tested; this section exists so a
reader of this gate record does not conclude the project stopped here. Nothing below
adds a hand-check to this file — each item names where its verification
actually lives.

**External evaluator import and the dual-layer benchmark.** Fully
automated, no operator attestation: `tests/integration/test_008_exit_gate.py`
holds AC-16 (import, normalization, explicit binding, correlation, dual-layer
reporting from the checked-in redacted fixture), and
`tests/integration/test_010_exit_gate.py` holds the live-pipeline criteria that
CI can prove without a credential — `live_model_run` labelling, exported
parameters recorded without invention, the credential boundary, and the matrix
counts. Both run inside `uv run pytest -q`.

**Three configuration-gated modules** — enabled on the live deployment where
their server-side configuration is present; each reports its current state at
`GET /api/v1/workspace`:

- **External audit (Storefront Witness).** Implemented and tested; the
  operator journey is the workspace's Audit → External surface view, and the
  guardrails (single asserted origin, no harness request to the audited site,
  sealed re-verified reports) are asserted in the architecture and integration
  lanes.
- **Live model benchmark (AC-17).** The pipeline is implemented end to end and
  CI proves it from the recorded fixture; the **live-credential run itself is
  still an open operator gate** (`specs/010-live-model-benchmark/tasks.md`
  T11). The run procedure is
  `integrations/google_evals/scenarios/README.md`; until it is executed,
  nothing may claim a live model run occurred —
  `test_gate_6_ac_17_needs_a_live_run_this_suite_cannot_perform` records the
  gap deliberately.
- **Shopify development-store integration (toward AC-18).** Implemented and
  exercised end to end against the authorized development store: pairing,
  theme-bridge same-session `/cart.js` observation, native Shopify WebMCP
  tools on the agent side, and a status panel projected from
  integrity-checked evidence (`tests/shopify/`,
  `tests/integration/test_self_target.py`). The formal 011 close-out (ticking
  its tasks and retiring `test_gate_6_shopify_work_has_not_started`) follows
  AC-17 per BUILD_ORDER's ordering and is recorded in
  `specs/011-shopify-cart-proof/` when it happens.

## If something here fails

A failure on this checklist is an exit-gate failure, not a note. Record it in
`specs/006-ui-webmcp-confirmation/plan.md` under the deviations ledger, and fix
it before declaring this gate green.
