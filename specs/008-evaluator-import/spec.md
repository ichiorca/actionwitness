# 008 — Import and correlate the external evaluator report (M7)

**Source:** `docs/BUILD_ORDER.md` §7/M7 · functional spec v1.9 §15.6, §16.4, §25.3, §26.5
**Goal:** complete the Tier 2 product differentiator without a live model
dependency — a checked-in evaluator report imports, binds one-to-one to
deterministic outcome evidence, replays in isolation, and produces an immutable
dual-layer benchmark artifact.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M7; nothing here is invented.

## Scope (implementation areas)

- **ADR-0005 and pinning** — complete ADR-0005 and pin the supported
  `webmcp-evals` reporter fixture/schema and normalizer version.
- **Persistence** — benchmark suite/trial persistence, and source/derived
  artifact relationships.
- **Import limits and validation** — enforce the 1 MiB import and 100-trial
  limits *before* parsing; validate the exact allowlisted schema; redact before
  persistence and hashing.
- **Normalization** — normalize only the required call-selection, arguments,
  order, error, trajectory, and reproducibility fields. Preserve unsupported
  metadata as `null`.
- **Binding** — require explicit one-to-one binding. Do not guess from order,
  timestamps, or similar text.
- **Replay** — isolated imported-trajectory replay through the registered
  adapter and a deterministic confirmation provider.
- **Metrics** — coverage, exclusions, the 2x2 matrix, zero-denominator
  behavior, four-decimal presentation strings, per-scenario/profile breakdowns,
  and the five required metrics, all calculated from integer counts.
- **Finalization** — atomically finalize one immutable benchmark artifact that
  references, but never rewrites, source evaluator and outcome artifacts.
- **Surfaces** — import/binding/replay/finalize APIs and `BenchmarkPanel` with
  source kind, mode, coverage, evidence links, and interpretation guardrails.
- **Fixtures** — check in at least three scenarios with three trials each,
  including a call-level pass / outcome fail trial labeled
  `silent_outcome_defect`.

## Acceptance criteria / exit gate

1. The complete fixture path runs with Node unavailable and no LLM or Shopify
   credentials.
2. Malformed, oversized, unsupported, ambiguous, duplicate, and cross-workspace
   inputs fail closed.
3. Matrix cells sum to eligible trials; eligible plus excluded equals total;
   errors are a disclosed subset of excluded.
4. Source kinds, correlation modes, scenario modes, and failure profiles are
   never pooled.
5. AC-16 passes and the Tier 2 gate is green together with M6.

## Non-goals

- No live-model execution; Tier 3 adds configured live execution that produces
  the same importable artifact (010/M9).
- No release hardening, deployment, or submission readiness (M8).
