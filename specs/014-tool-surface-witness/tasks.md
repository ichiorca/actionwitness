# 014 — tasks

Cite the T-ID in every commit that advances it.

- [ ] T1 — Recorded capture route + server-side canonicalization/hash of a
      posted tool surface; `tool_surface_captured` event with baseline.
- [ ] T2 — Frontend capture at arm + `toolchange` watch with debounced
      re-capture; every capture recorded, stale responses rejected.
- [ ] T3 — Server-side delta computation in the five §9.5 kinds;
      `tool_surface_changed` events.
- [ ] T4 — `stable_tool_surface` evaluates from events: declared-churn
      allowlist, undeclared delta → `tool_surface_mutation`, no-baseline →
      `observation_unavailable` (unchanged).
- [ ] T5 — Per-invocation identity check on the invocation route; mismatch
      refuses with a structured error.
- [ ] T6 — `tool_surface_poisoned` demo profile + look-alike registration;
      forbidden against external targets (§13.3 parity); side-by-side
      definition diff in the findings panel.
- [ ] T7 — Replay parity through the 007 surface-evidence path; exit gate;
      traceability map extended.
