# 007 — tasks

Cite the T-ID in every commit that advances it.

- [x] T1 — The core eval vocabulary: versioned `RegressionEvalCase`, fixture,
      trajectory, environment expectation, interaction strategy, and eval-report
      models, with their enums registered. Replaces the deliberate tripwire in
      `tests/evals/test_evals_lane.py` with real §24 coverage in the same change.
- [ ] T2 — Generate a case from a terminal failed or warning-bearing run, and
      only from one. Idempotent: the same source run yields the same case, and a
      proposal run is refused with `PROPOSAL_RUN_NOT_ELIGIBLE`.
- [ ] T3 — Case content per §24.2: embed the source contract verbatim and verify
      its stored hash first, minimize the fixture (retaining the complete
      canonical state when the contract carries `no_undeclared_changes`), drop a
      read-only call only when it is irrelevant to every check and later
      mutation, and preserve repeated request IDs.
- [ ] T4 — Redaction and hashing order: replace sensitive but replay-required
      values with deterministic type-valid fixtures, validate against the
      repository's public JSON Schema, and calculate the content hash **last**,
      after every other field is final.
- [ ] T5 — The isolated eval workspace (§17.1 `kind: eval`) and fixture restore
      through the registered `ManagedTargetAdapter` — never through a second
      path to the target.
- [ ] T6 — Replay the allowlisted trajectory through that adapter, capturing
      state and events under the `eval` actor so a replayed run classifies
      identically to its source.
- [ ] T7 — The interaction providers: `recorded_approval`, `recorded_denial`,
      and `no_confirmation`. No mode may synthesize an approval the case did not
      contain.
- [ ] T8 — Environment profiles: `current` → `post_fix` with no active fault,
      `reproduce_source` → the immutable source scenario and failure profile.
      The mapping lives in the integration/application layer, never in the core.
      `current` is always the default.
- [ ] T9 — Expectation matching: compare overall result **and** the exact
      critical classification set. Eval status is expectation matching, not
      whether the business outcome string is literally `passed`.
- [ ] T10 — Non-replayable evidence (§24.3a): replay the recorded `surface`
      baseline and deltas as events, and list any policy that still cannot be
      evaluated in `non_replayable_policies` — excluded from both classification
      sets and named in the report, so a passing eval cannot hide an unevaluated
      policy.
- [ ] T11 — The canonical eval report (FR-088) and §15.4's API routes, plus
      `EvalPanel`. Cleanup of mutable eval-target state happens **after** the
      immutable report is persisted.
- [ ] T12 — The CLI: `eval validate` and `eval run --environment --report-dir`,
      structured argument parsing, a compact printed result, and FR-088's exit
      codes 0/1/2, replacing the scaffold that exits 2 for everything.
- [ ] T13 — The exit gate: idempotent, redacted, schema-valid, source-preserving
      generation; `reproduce_source` exits 0 on an exact match; an unrelated or
      additional critical classification exits 1; `current` exits 0; an invalid
      definition or harness execution exits 2; AC-08, AC-12, and AC-15 through
      both API and CLI. Extend the architecture lane's exit-gate traceability
      map to 007.
