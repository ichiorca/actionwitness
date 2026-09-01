# 010 — Tier 3 priority 1: configured live LLM benchmark (M9)

**Source:** `docs/BUILD_ORDER.md` §7/M9 · functional spec v1.9 §12.11 (FR-099–FR-101), §25.3, §26.5, AC-17
**Goal:** run the pinned evaluator against one explicitly configured model and
import the result through the *same* M7 pipeline — proving the Tier 2 path was
never fixture-shaped, while the deterministic engine still makes no model call.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M9; nothing here is invented.

**Entry condition (BUILD_ORDER):** M6, M7, and AC-16 are green.

## Scope (implementation areas)

- **Configured backend** — a pinned `webmcp-evals` configuration against one
  explicitly configured LLM backend. The configured model is used *only* to
  generate candidate intent variants and to run the evaluator; it is never
  called from the deterministic engine.
- **Credentials** — supplied only through a developer environment or
  deployment secret, and kept only in the evaluator process environment. Never
  through the browser, a WebMCP argument, a committed file, or an uploaded
  benchmark manifest.
- **Intent generation** — from one canonical contract intent, up to six
  paraphrased, ambiguous, and adversarial variants. Python schema-validates
  length and character limits and rejects variants containing secrets or
  instructions to bypass confirmation.
- **Human review and freezing** — explicit human approval, then variants are
  frozen into the content-hashed benchmark manifest before trials begin.
  Generation is not rerun between repetitions.
- **Live trials** — at least three scenarios with at least three completed
  live trials each, imported through the same M7 path.
- **Recorded parameters** — exported model and evaluator parameters persisted
  exactly; unsupported values remain `null`, never invented.
- **Artifacts** — the live evaluator report and its model/configuration
  metadata persisted as immutable benchmark sources; the `live_model_run`
  artifact finalized and precomputed before the demo is recorded.
- **Offline fallback** — the checked-in report fixture keeps the matrix UI and
  deterministic verification reproducible without credentials, quota, or
  network, and stays labeled `recorded_fixture`.

## Acceptance criteria / exit gate

1. The suite is labeled `live_model_run`, and a fixture-backed run is never
   represented as a live execution.
2. Actual exported evaluator and model parameters are recorded without
   inventing missing values.
3. Each eligible trial is bound exactly, producing the dual-layer matrix and
   silent-outcome-defect evidence through the AC-16 pipeline.
4. The developer-provided credential is retained only in the evaluator process
   environment.
5. The same import/correlation path still passes in CI from the checked-in
   redacted fixture, labeled `recorded_fixture`.
6. AC-17 passes. If it does not, Shopify work (011/M10) does not start.

## Non-goals

- No Shopify work (011/M10) and no WebMCP/policy polish (012/M11).
- No model call from the deterministic contract engine, ever.
- No new correlation mode, matrix, or metric: this milestone supplies a
  different *source* to the M7 pipeline, not a second pipeline.
