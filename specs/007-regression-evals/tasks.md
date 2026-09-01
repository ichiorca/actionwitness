# 007 — tasks

Cite the T-ID in every commit that advances it.

- [ ] T1 — Core eval models in `actionwitness_core.evals`: versioned
      `RegressionEvalCase`, fixture, trajectory expectation, environment
      expectation, interaction strategy, eval report; frozen, schema-versioned,
      kernel-disciplined. Replace the evals-lane tripwire with real §24
      model coverage in the same change.
- [ ] T2 — Case generation from failed/warning-bearing terminal runs only:
      embedded source-contract-hash verification, safe fixture minimization
      retaining policy-relevant repeated IDs, deterministic replacement of
      sensitive replay-required values, final hash calculated last.
- [ ] T3 — Idempotency: regenerating from the same source run returns the
      existing case; a changed source is impossible (immutable) and a changed
      generator version is a new case version, never a rewrite.
- [ ] T4 — Tier 2 eval-table migrations through the 004 ordered runner;
      repositories stay insert-only/append-only.
- [ ] T5 — Internal eval workspaces: created isolated, swept by cleanup,
      invisible to and unreachable from user workspaces (two-client tests).
- [ ] T6 — Fixture restore + allowlisted replay through the registered managed
      adapter; a call outside the allowlist refuses and fails the eval run
      with a structured error, never skips silently.
- [ ] T7 — Scenario mapping in the integration/application layer: `current` →
      `post_fix`, `reproduce_source` → the immutable source scenario and
      fault; core never interprets mode names.
- [ ] T8 — Interaction providers: recorded approval, recorded denial,
      no-confirmation; no inferred consent, and a replay requiring a decision
      the recording lacks fails closed.
- [ ] T9 — Comparison semantics: overall result + exact critical
      classification set; eval status separate from business outcome; tests
      for the unrelated-classification and additional-classification cases.
- [ ] T10 — Immutable eval report persisted first, mutable eval-target state
      cleaned after; a cleanup failure leaves the report intact and visible.
- [ ] T11 — API routes and `EvalPanel`, server-state-driven enablement,
      polling and stale-response behavior per the 006 patterns.
- [ ] T12 — CLI `eval validate` and `eval run`: JSON report, exit codes
      0/1/2, runnable from a clean checkout via `uv run actionwitness`.
- [ ] T13 — Exit gate: reproduce_source recreates the exact source failure
      (exit 0), unrelated/additional critical classification exits 1,
      `current` passes (exit 0), invalid definition/harness failure exits 2;
      AC-08/12/15 through API and CLI; extend the traceability map to 007.
