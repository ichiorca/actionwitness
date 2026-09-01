# 006 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

## The three facts that shape this milestone

**First: this is the Tier 1 gate, and it is the first milestone judged as a
deployed application rather than as a component.** 002–005 could be proven with
`pytest`. Two of this milestone's six exit criteria — "a compatible browser
completes Journeys A and B through real WebMCP tools" and "an unsupported
browser completes the manual equivalent" — cannot be. See the operator decision
at the top of the ledger below; it wants answering *before* T1, not at T13.

**Second: the frontend is a scaffold, and it is a scaffold that throws.**
`src/webmcp/adapter.ts` currently raises on every registration call, and
`App.tsx` renders a placeholder that says so. Nothing about the browser layer is
half-built and inheritable; T5 onward is new construction against ADR-0002's
pinned `use-webmcp-tool@0.2.0` + `webmcp-types@0.1.5`. The 001 spike proved the
hook path and the native path work and cleans up under StrictMode; that spike is
the reference, not the implementation.

**Third: the backend confirmation surface does not exist yet either.** §15.3's
`POST /runs/{run_id}/confirmations/{confirmation_id}/decision` and its `DELETE`
have been deliberately absent since 005 — `runs.py` says so in its own module
docstring. The `confirmation_requests` table has existed since 004, the
`requires_confirmation` policy is already in the checkout templates, and the
adapter already publishes `proceed_to_checkout`. So T1–T3 are filling in a
socket every surrounding piece already has a plug for, which is the good case.

This ordering is deliberate: **the server-side confirmation lifecycle lands
before any of it is visible.** A confirmation flow debugged through a browser is
a confirmation flow whose consent semantics were decided by whatever made the
modal work.

---

1. **The confirmation lifecycle** — created in a *short* transaction, bound to
   workspace, run, invocation, authoritative state-binding hash, bounded
   consequence summary, and expiry. The binding is the whole security property:
   §14 makes the cookie the authorization boundary and the `confirmation_id`
   merely an identifier, so an approval that was not bound to the exact material
   state can be replayed against a different cart.

   "Short" is load-bearing, and it is 005's constraint again: the transaction
   that creates the confirmation must close before the human is asked anything.
   A transaction held open across a human decision holds SQLite's write lock for
   up to 60 seconds and stalls every other workspace (ADR-0003).

2. **Decision and cancellation endpoints.** On approval, the revalidation and
   the consumption and the order creation are **one transaction**. Splitting
   them is the defect: an approval consumed in one transaction and an order
   created in the next can produce an order with no consent record, or a
   consumed approval with no order, and both are unrecoverable from evidence.
   FR-066's single-use consumption is what this stage exists to make true.

   The agent cannot approve its own request. That is not a UI rule — the
   constitution says an agent "cannot create, broaden, or approve its own
   consent" — so it is enforced where the decision is recorded.

3. **Denial, expiry, and cancellation as safe blocks.** §14.8 is precise: the
   invocation is recorded as *safely blocked* rather than failed. The
   distinction matters to the verdict — a blocked checkout is the system working
   correctly, and classifying it as a failure would make Journey B's success
   look like a failure and teach a reader to ignore the layer.

4. **The read surfaces the UI needs.** `GET /runs/{run_id}` is the last §15.3
   endpoint still missing. `get_run_findings` needs a bounded projection: §11.4
   gives it 4,000 characters against every other tool's 1,500, a default `limit`
   of 3, 120-character truncation of each `expected` and `actual`, and an
   explicit untruncated total — because a finding an agent cannot read is
   equivalent to a finding that was never produced.

5. **The local WebMCP adapter.** Every constraint here is already written down;
   the work is honouring them: a safe no-op when `document.modelContext` is
   absent, StrictMode double-mount cleanup with no duplicate registration,
   reconciliation through `getTools()` and `toolchange` (FR-003), the
   normalized `isError: true` envelope, and per-invocation `signal` forwarding.

   **All direct WebMCP access stays in this file.** That is a constitutional
   invariant, not a preference, and it is what makes the rest of the UI testable
   without a browser.

6. **Native `get_workspace_status`.** Direct registration rather than the hook,
   per §11.1 and ADR-0002's rule-3 split. It is the tool AC-21 leans on hardest:
   its result must agree with the banner, the enabled controls, the previous
   tool's `next_action`, and the action history.

7. **The hook-registered harness tools.** `enabled` is decided from *server*
   state. A tool that decided its own availability from browser state would
   offer an action the server will refuse, which is precisely the disagreement
   005's guidance projection exists to prevent. `create_outcome_contract`
   (declarative form), `create_regression_eval`, `run_regression_eval`, and
   `propose_assertions` are **not** in this milestone — see the ledger.

8. **The Buggy Store bridge tools.** They call the generic harness target route
   and never Buggy Store service objects or its API directly from React (§11.2).
   005 built that route and proved it records evidence either side of dispatch;
   this stage must not acquire a second path to the target.

9. **The panels.** Authoritative state from FastAPI on startup and refresh —
   browser storage may hold only recoverable drafts and preferences, never
   verdicts, approvals, or run state.

10. **Paged polling by event sequence.** 005's T12 endpoint, consumed. The two
    properties its own tests already pin apply here as client behaviour:
    `has_more: false` is not "the run ended", and an obsolete response must be
    ignored rather than rendered.

11. **The confirmation dialog.** Focus trap and restoration, no preselected
    approval, `aria-live` handoffs, a text alternative for every status. The
    "no preselected approval" rule is a safety property wearing accessibility
    clothing: a focused default approve button is a consent flow that a stray
    Enter key completes.

12. **Journey B end to end**, then **13. the Tier 1 gate.**

---

## Deviations and decisions worth an operator's eye

### Before T1 — how are exit criteria 1 and 2 evidenced? (**operator decision**)

Exit criteria 1 and 2 are about *a real browser*, and criterion 6 says the Tier 1
ACs are green "in the deployed application". Neither is something `pytest` or
`vitest` can assert:

- `tests/browser/` is an **opt-in lane**, and the architecture gate
  `test_no_criterion_is_covered_by_a_test_in_a_disabled_lane` explicitly forbids
  an exit-gate criterion being covered from it. That gate was written in 004 and
  is correct; it means the browser criteria cannot be discharged the way every
  previous milestone's were.
- AC-01 ("a user opens the live URL") depends on the deployment that M8 builds.
  BUILD_ORDER already carves out AC-10 as closing in M8; AC-01 has the same
  shape and no such carve-out.

Three options, none of which an agent should pick alone:

1. **Operator-attested**: a written manual checklist, run by the operator in
   Chrome with `#enable-webmcp-testing` and in a browser without WebMCP, its
   result recorded in the repo. Deterministic tests cover everything below the
   browser boundary; the gate records who attested and when.
2. **A real-browser lane promoted out of opt-in** (Playwright against a running
   service), accepted as a required suite with the flakiness and CI cost that
   implies, and the architecture gate amended to permit it.
3. **Split the gate**: 006 closes on everything testable, and criteria 1, 2, and
   AC-01 are formally deferred to M8 alongside AC-10.

Option 1 is the smallest change and matches what BUILD_ORDER already does for
AC-10. **This needs deciding before T1**, because it determines whether T13 is a
test file or a checklist, and because writing the milestone's tests without
knowing which is the wrong order.

### Scope — four §11.1 tools are deliberately not in this milestone

BUILD_ORDER M5 says "the hook-based harness/Buggy Store tools", which reads as
all of §11.1. Four are excluded, each for a reason outside this spec:

| Tool | Why not here |
|---|---|
| `create_outcome_contract` | Declarative HTML form registration, which the runway assigns to 012 |
| `create_regression_eval` | Eval case generation is 007 |
| `run_regression_eval` | Fixture replay is 007 |
| `propose_assertions` | Requires proposal mode, which 005 declared and refused pending an operator decision |

If the operator reads M5 as requiring all eleven, T7 grows and 007/012 shrink.
**Flagged rather than assumed**, because the tool table is the surface AC-22
measures "every capability is reachable by tool" against.

### Carried in from 005, and now load-bearing

- **`runs.fault_active` is never populated.** `ComparisonPanel` is specified to
  show the matched pre/post comparison, and §23.1 gives that comparison a
  `fault_active`. The panel will render a field that is always `false`. The
  005 plan carries the decision (protocol change, new optional method, or accept
  it); M5 is where it becomes visible to a user rather than only to a test.
- **Proposal mode** — see the table above.
- **FR-039's lease enforcement surface.** 005 built `require_no_lease()` with no
  caller. The store panel is the natural one: a human mutating the cart directly
  during a run is exactly what the lease refuses, and Journey B puts a human in
  front of that store. Whether M5 owns it is the operator's call.
- **Criterion 3's `RUN_ALREADY_VERIFYING` vs `RUN_TIMELINE_SEALED`.** Unchanged
  from 005; the UI must render whichever the server sends, so the wording
  question surfaces in copy here.

### The frontend has no lint and no type-check in any gate

`package.json` declares `typecheck`, `test`, and `build` — no lint script, and
no evidence any gate runs `typecheck`. The constitution requires a dedicated
strict type-check ("a Vite production build alone does not count") *and* that
configured lint checks pass. Today the frontend satisfies neither claim, and it
has been cheap to ignore because there are ~200 lines of TypeScript. After this
milestone there will not be.

**Proposed, needing no operator decision unless it is wrong:** T5 adds an
ESLint configuration and wires `typecheck` and `lint` into the same architecture
lane that already asserts the Python gates, so the frontend's quality bars are
enforced from the moment there is a frontend. Recorded here because it is scope
this spec does not name.

### `get_run_findings` needs a projection the API does not have

§11.4's budget rules (4,000 characters, `limit` default 3, 120-character
truncation with an explicit marker, untruncated totals) describe a *bounded
findings* shape. 005 serves the full report and nothing smaller. T4 adds it
server-side rather than truncating in TypeScript, because §23.3 limits tool
output size and a browser-side truncation would put the budget rule in the layer
the constitution says must not own business logic.

### Journey B needs an order, and 005 never created one

Every 005 journey stops before checkout (§6.1 is explicit that Journey A does
not check out). Journey B is the first to create an order, so T12 is also the
first exercise of the store's `/store/checkout` path through the harness, of the
`requires_confirmation` policy in a real evaluation, and of a contract whose
`expected_tools` includes `proceed_to_checkout`. Expect the first genuine
integration surprises there rather than in T1–T3.
