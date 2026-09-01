# Tier 1 gate — operator checklist (spec 006 / BUILD_ORDER M5)

Two of M5's exit criteria are about a **real browser**, and one Tier 1
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

Attested by: ______________________  Date: ____________  Build: ____________

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

Attested by: ______________________  Date: ____________  Build: ____________

## AC-01 — working live application

Deferred to M8 with AC-10, which BUILD_ORDER already schedules there. Recheck
against the deployed URL once 009 lands.

- [ ] The live URL loads without credentials and reports WebMCP support status.

Attested by: ______________________  Date: ____________  Build: ____________

## If something here fails

A failure on this checklist is an exit-gate failure, not a note. Record it in
`specs/006-ui-webmcp-confirmation/plan.md` under the deviations ledger, and fix
it before declaring the Tier 1 gate green.
