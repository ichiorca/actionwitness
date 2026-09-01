# 014 — plan

Round-2 costing: ~1–1.5 days. The frontend owns capture (it is the only place
`getTools()` exists); the server owns evidence and evaluation.

1. **Capture module** (`src/webmcp/`): serialize each descriptor to the
   canonical subset (name, description, readOnlyHint, canonical inputSchema),
   hash client-side is NOT trusted — the raw captured definitions post to a
   new recorded route and the SERVER canonicalizes and hashes (constitution:
   browser input is untrusted; a client-computed hash would be self-report).
2. **Watch**: `toolchange` handler with a quiet-period debounce (ADR-0002:
   bursts are uncoalesced), re-capture, post. Stale-response discipline per
   006 patterns.
3. **Server**: `tool_surface_captured` / `tool_surface_changed` events in the
   existing append-only stream; delta computation server-side against the
   stored baseline; §9.5 kinds.
4. **Engine**: `stable_tool_surface` consumes the events; declared-churn
   configuration lists tool names whose presence is phase-dependent (the
   §11.5 table is the default declared set for the harness's own surface).
5. **Identity check**: invocation route compares the named tool against the
   baseline entry before dispatch; mismatch refuses with a structured error.
6. **Poisoned profile**: a demo-only script registering a look-alike tool;
   forbidden for external targets by the same gate as §13.3.

Risks: double-capture under StrictMode (reuse the 006 lifecycle discipline);
debounce vs missed deltas (record every capture, debounce only re-hash);
declared-churn config becoming a wildcard (allowlist names, never patterns).

**Timing**: post-submission unless the schedule collapses favorably; its
demo beat is strong but the FR-157 diff (013) outranks it on cost/benefit.
Prerequisite: none on 010–012; independent of Tier 3 gates.

## Deviations and decisions worth an operator's eye

_Per-task, anchored to spec sections._
