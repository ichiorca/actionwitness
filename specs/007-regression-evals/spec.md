# 007 — Generate, replay, and run deterministic regression evals (M6)

**Source:** `docs/BUILD_ORDER.md` §7/M6 · functional spec v1.9 §24.1–24.6, §9.7–9.8, §12.9
**Goal:** turn the Tier 1 failure into a portable CI gate — a failed run becomes
a self-contained, versioned eval case that replays deterministically and
reproduces the exact source classification, locally and in CI.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M6; nothing here is invented.

## Scope (implementation areas)

- **Core models** — versioned `RegressionEvalCase`, fixture, trajectory,
  environment expectation, interaction strategy, and eval-report models join
  `actionwitness_core.evals`. This replaces the scaffold; the evals-lane
  tripwire test exists precisely to force that replacement to arrive with
  real §24 coverage in the same change.
- **Case generation** — idempotent, and only from failed or warning-bearing
  terminal runs. The embedded source contract hash is verified; fixtures are
  minimized safely; policy-relevant repeated IDs are retained; sensitive
  replay-required values are replaced with deterministic fixtures; the final
  case hash is calculated last.
- **Replay** — internal eval workspaces (isolated via the 004 machinery);
  restore the fixture and replay only allowlisted calls through the same
  registered managed adapter the source run used.
- **Scenario mapping** — `current` → Buggy Store `post_fix` and
  `reproduce_source` → the immutable source scenario/fault, implemented in the
  integration/application layer, never the core.
- **Interaction strategies** — recorded approval, recorded denial, and
  no-confirmation providers, with no inferred consent.
- **Comparison** — overall result plus the exact critical classification set;
  eval status stays separate from the actual business outcome.
- **Surfaces** — API routes, `EvalPanel`, CLI `eval validate` / `eval run`,
  JSON report writing, exit codes 0/1/2. Mutable eval-target state is cleaned
  after the immutable report persists.

## Acceptance criteria / exit gate

1. Case creation is idempotent, redacted, public-schema-valid, and
   source-run preserving; a proposal run is refused with
   `PROPOSAL_RUN_NOT_ELIGIBLE`.
2. `reproduce_source` recreates the source overall failure and the exact
   critical classification set (set equality, not containment), and exits 0.
3. An unrelated or additional critical classification exits 1.
4. `current` passes against the corrected behavior and exits 0; an invalid
   definition or harness execution failure exits 2 (FR-088's codes,
   replacing the scaffold that exits 2 for everything).
5. Non-replayable evidence is honest: §24.3a `surface` replay, and any
   policy that cannot be evaluated is excluded from both classification
   sets AND named in `non_replayable_policies` in the report; the selected
   environment profile appears in the report.
6. AC-08, AC-12, and AC-15 pass through both API and CLI tests.
7. The evals-lane tripwire is replaced by real §24 coverage in the same
   change that lands core eval behavior (T1) — in this spec its firing is
   the expected signal, not scope creep.

## Non-goals

- No external evaluator import, benchmark suites/trials, or correlation
  (008/M7).
- No live-model execution (010/M9).
