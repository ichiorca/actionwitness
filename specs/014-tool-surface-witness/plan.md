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

### Needs an operator decision

- **T4 — the declared-churn allowlist is NOT shipped.** 014's scope names an
  allowlist of target tool names whose mid-run appearance is expected. Adding
  `declared_churn_tools` to `StableToolSurfacePolicy` changes
  `regression_eval_case_1_0.json`, a *published* artifact inside the protected
  eval corpus that this session must not republish (the L0 policy denies the
  path: "a maker must not edit its own grader"). The case 014's scope actually
  names — the 006 phase-driven harness tool set — is excused structurally by
  §9.11's partition, which is stronger than an allowlist. The gap is a *target*
  tool that legitimately churns, which no shipped contract has. **To land it:**
  add `declared_churn_tools: tuple[ToolName, ...] = ()` to the policy, sort it
  into `canonical_document()`, and republish the case schema.

- **T1 — §17.2's "normalise an absent value and its documented default to the
  same form" is deliberately unimplemented.** The explicit sorts are done. No
  default is normalised, because normalising one this module cannot name would
  erase a real difference — an absent `additionalProperties` is not `false`, and
  a schema that stopped forbidding extra properties got looser. Over-reporting a
  `schema_change` is visible and waivable; under-reporting is neither.

### Raised by this milestone

- **T1 — the server assigns the namespace, not the browser.** §9.11 applies
  stability policy to the target partition, so a page that could label its own
  tools would mark a poisoned look-alike `harness` and step outside the policy
  written to catch it. A name the harness publishes is `harness`; everything
  else is `target`, which fails safe. The harness tool names therefore exist on
  both sides of the boundary, held equal by
  `tests/architecture/test_harness_tool_surface.py`.

- **T1/T3 — one commit for two tasks.** The capture and the deltas it implies
  are appended in a single transaction, so a reader can never see a capture
  whose consequences have not landed. Splitting them in git would not split
  them in the timeline.

- **T4 — `PolicyEvidence.observed_surface_deltas` changed type** from delta
  *kinds* to whole `SurfaceDelta`s. The policy asks three questions of each
  delta — partition, tool, kind — and a bare kind answers only the last. Three
  call sites updated, including 007's exit gate, which now also asserts the
  replayed delta still names its tool.

- **T4 — `tool_surface_changed` has one payload shape, live or replayed.**
  `surface_evidence` reads named fields rather than validating the whole
  payload: a replayed event carries `recorded_sequence` beside the delta, and
  strict validation rejected it for the extra key and dropped the delta
  *silently* — turning a poisoned surface into a clean run. Both directions are
  tested: extra context is kept, an unknown kind is dropped.

- **T5 — a mismatch refuses the invocation, not just records it.** The agent
  chose the tool from a description that no longer describes it, so dispatching
  would spend a human's consent on something else. Three deliberate
  non-refusals are separated so adding a fourth is a deliberate act: no hash
  presented (§15.3 makes it optional), no baseline captured (§16.1 already
  fails the policy closed; refusing every call too would be a second penalty),
  and the tool absent from the baseline (an `added` delta for the surface
  policy, not a reason to block the call being made now).

- **T5 — fixed a latent bug the new path exposed.** `_start_or_trip` inferred
  its refusal from `sequence == 0` and always raised `EVENT_LIMIT_EXCEEDED`.
  Two paths now stop a start before it records one, so the refusal is carried
  out rather than guessed.

- **T2 — the StrictMode test caught a real bug.** The in-flight guard was a
  `useRef`, which survives unmount/remount: the second mount found the first
  mount's capture still in flight and skipped, while the first discarded itself
  on `live` — leaving the run with **no baseline at all**. Worse than
  double-capturing, and invisible without the test.

- **T6 — the store implements nothing for `tool_surface_poisoned`.** The
  injection is browser-side; no server can register a tool in somebody's page.
  The store accepts and records the profile so the page knows to inject, and its
  own behaviour stays correct — which is what §13.3 requires and what leaves
  every assertion green.

- **T6 — the demo-only guard lives at selection, not in the adapter.** An
  external target's adapter is exactly the code that must never be asked to do
  this, so a guard there would be trusting the thing it guards against.
  `checkout_without_confirmation` is named alongside it.

- **T6 — 013's honesty gate stopped pinning the whole implemented-profile set.**
  It grew twice for reasons unrelated to 013. The exact set stays pinned once,
  in the store's own test, where a profile without an injector should fail.

- **T7 — `_recorded_surface` had to learn 014's payload.** It read
  `payload["tools"]` and `partition`; the live capture writes
  `payload["surface"]["tools"]` and `namespace`. Left alone, a generated case
  would have carried an empty baseline and mislabelled every harness delta as a
  target one — a replay failing a policy its source run passed.
