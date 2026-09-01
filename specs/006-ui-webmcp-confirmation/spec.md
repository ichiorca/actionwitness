# 006 — Tier 1 shared UI, WebMCP tools, and confirmation journey (M5)

**Source:** `docs/BUILD_ORDER.md` §7/M5 · functional spec v1.9 §6.2, §8.4, §11,
§14, §15.3, App. D
**Goal:** complete the deployed human-and-agent experience and Journey B.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M5; nothing here is invented.

> **This milestone is the Tier 1 gate.** Everything before it is a component;
> this is where the product is judged as a deployed application.

## Scope (implementation areas)

**Frontend**

- Implement the capability bar, `GuidanceBanner`, `ConfigPanel`,
  `ContractPanel`, `TargetPanel`, `RunTimeline`, `ConfirmationDialog`,
  `FindingsPanel`, and `ComparisonPanel`.
- Load authoritative state from FastAPI on startup/refresh and use paged polling
  by event sequence.
- Preserve the full human UI when WebMCP is unavailable.
- Register direct-native `get_workspace_status` and the hook-based
  harness/Buggy Store tools through the local adapter only.
- Use server state for `enabled` decisions; FastAPI remains authoritative when
  browser state is stale.
- Normalize compact success and `isError: true` results within the
  1,500-character result budget.

**Confirmation**

- Create the confirmation in a short transaction, bind it to
  workspace/run/invocation/state/consequence/expiry, and move guidance to the
  human approver.
- Keep the invoking page's tool promise pending without holding a server
  transaction.
- Approve/deny/cancel through the cookie-authorized endpoint; on approval,
  revalidate and consume once in the same transaction as order creation.
- Make denial, expiry, refresh, stale/reused approval, and cross-tab behavior
  fail safely with explicit recovery guidance.
- Implement keyboard focus management, no preselected approval, `aria-live`
  handoffs, and text alternatives for every status.

## Acceptance criteria / exit gate

1. A compatible browser completes Journeys A and B through real WebMCP tools.
2. An unsupported browser completes the manual equivalent and shows setup
   guidance.
3. UI banner, enabled controls, native status result, tool `next_action`, and
   action history share the same action code at every transition.
4. No order exists before approval; approval is consumed exactly once;
   denial/expiry/cancel create no order.
5. Frontend StrictMode, registration cleanup, polling, error normalization,
   refresh, and accessibility tests pass.
6. **Tier 1 gate:** AC-01, AC-03, AC-04, AC-06, AC-09, AC-11, AC-19, AC-20, and
   AC-21 are green in the deployed application. AC-10 repository usability is
   closed during M8 before the final release.

## Non-goals

- No eval case generation, fixture replay, or CLI exit codes (007).
- No evaluator report import, binding, or benchmark matrix (008).
- No Docker, single-origin mounting, or deploy rehearsal (009).
- No live LLM benchmark (010), Shopify pairing or theme bridge (011).
- No retry/confirmation injectors, declarative form tool, reconciliation, or SSE
  (012) — these are the Tier 3 WebMCP polish items.

## Implementation order (normative)

1. the confirmation lifecycle server-side → 2. the confirmation decision,
cancellation, and expiry endpoints → 3. the workspace read the UI loads from →
4. the local WebMCP adapter → 5. native `get_workspace_status` → 6. the
hook-registered harness tools → 7. the Buggy Store integration bridge tools →
8. the panels and the guidance banner → 9. paged polling by event sequence →
10. the confirmation dialog and its accessibility contract → 11. Journey B end
to end → 12. the Tier 1 gate.
