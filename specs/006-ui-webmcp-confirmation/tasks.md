# 006 — tasks

Cite the T-ID in every commit that advances it.

- [ ] T1 — The confirmation lifecycle server-side: create the request in a
      short transaction bound to workspace, run, invocation, authoritative
      state-binding hash, bounded consequence summary, and contract-configured
      expiry (default 60s), and move guidance to the human approver.
- [ ] T2 — The decision endpoint (`approve_once` / `deny`) and the cancellation
      endpoint, both cookie-authorized. On approval, revalidate and consume the
      approval exactly once **in the same transaction as order creation**.
- [ ] T3 — Denial, expiry, and cancellation as safe blocks rather than failures:
      no order, an invocation recorded as safely blocked, and explicit recovery
      guidance. A stale or reused approval fails closed.
- [ ] T4 — The read surfaces the UI loads from: `GET /runs/{run_id}` (§15.3) and
      the bounded findings projection `get_run_findings` needs (§11.4's 4,000
      character budget, default `limit` 3, untruncated totals reported).
- [ ] T5 — The local WebMCP adapter (`src/webmcp/adapter.ts`), replacing the
      throwing scaffold: safe no-op when unsupported, StrictMode double-mount
      cleanup without duplicate registration, reconciliation through
      `getTools()` and `toolchange` (FR-003), normalized success and
      `isError: true` results, and per-invocation `signal` forwarding.
- [ ] T6 — Native `get_workspace_status` through
      `document.modelContext.registerTool`, reporting target, scenario mode,
      active contract, run status, active actor, and the available next action.
- [ ] T7 — The hook-registered harness tools: `list_contract_templates`,
      `arm_outcome_contract`, `verify_outcome`, `get_run_findings`,
      `reset_workspace`, and `get_outcome_contract`. `enabled` comes from server
      state; FastAPI stays authoritative when browser state is stale.
- [ ] T8 — The Buggy Store integration bridge tools (`search_catalog`,
      `get_cart`, `update_cart`, `apply_discount`, `proceed_to_checkout`),
      registered by the integration bridge and dispatched only through the
      generic harness target-invocation endpoint.
- [ ] T9 — The panels: capability bar, `GuidanceBanner`, `ConfigPanel`,
      `ContractPanel`, `TargetPanel`, `RunTimeline`, `FindingsPanel`, and
      `ComparisonPanel`, loading authoritative state from FastAPI on startup and
      refresh.
- [ ] T10 — Paged polling by event sequence (§15.3's `after_sequence`/`limit`),
      with stale-response rejection, cancellation on unmount, and rehydration
      after a refresh.
- [ ] T11 — `ConfirmationDialog`: focus trap and restoration, no preselected
      approval, `aria-live` handoffs, a text alternative for every status, and
      the read-only "confirmation pending in another tab" banner.
- [ ] T12 — Journey B end to end: the agent calls `proceed_to_checkout`, the
      dialog appears, the tool promise stays pending, a human approves once, and
      verification confirms the order was created only after the approval event.
- [ ] T13 — The Tier 1 gate: exit criteria 1–5, the single action code shared by
      banner, enabled controls, native status result, tool `next_action`, and
      action history, and AC-01/03/04/06/09/11/19/20/21. Extend the architecture
      lane's exit-gate traceability map to 006.
