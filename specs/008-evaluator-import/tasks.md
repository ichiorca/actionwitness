# 008 — tasks

Cite the T-ID in every commit that advances it.

- [x] T1 — Complete ADR-0005 and pin the supported `webmcp-evals` reporter
      fixture/schema and normalizer version. The architecture lane's ADR record
      tests currently fail on this record; they pass as part of this task.
- [x] T2 — Benchmark suite and trial persistence, with the source/derived
      artifact relationships. A derived artifact references its sources; a
      source artifact is never rewritten.
- [x] T3 — Enforce the 1 MiB import and 100-trial limits **before** parsing,
      validate the exact allowlisted schema, and redact **before** persistence
      and hashing.
- [x] T4 — Normalize only the required call-selection, arguments, order, error,
      trajectory, and reproducibility fields. Preserve unsupported metadata as
      `null`.
- [ ] T5 — Require explicit one-to-one trial binding, validated and saved before
      the suite becomes ready. Never guess from order, timestamps, or similar
      text.
- [ ] T6 — Isolated imported-trajectory replay through the registered adapter
      and a deterministic confirmation provider, reusing 007's eval-workspace
      and interaction machinery rather than forking it.
- [ ] T7 — Calculate coverage, exclusions, the 2x2 matrix, zero-denominator
      behavior, four-decimal presentation strings, per-scenario/profile
      breakdowns, and the five required metrics from integer counts.
- [ ] T8 — Atomically finalize one immutable benchmark artifact that references,
      but never rewrites, the source evaluator and outcome artifacts.
- [ ] T9 — The §15.6 import/binding/replay/finalize APIs.
- [ ] T10 — `BenchmarkPanel` with source kind, mode, coverage, evidence links,
      and interpretation guardrails.
- [ ] T11 — Check in at least three scenarios with three trials each, including
      a call-level pass / outcome fail trial labeled `silent_outcome_defect`.
- [ ] T12 — The exit gate: the complete fixture path with Node unavailable and
      no LLM or Shopify credentials; malformed, oversized, unsupported,
      ambiguous, duplicate, and cross-workspace inputs fail closed; matrix cells
      sum to eligible trials and eligible plus excluded equals total, with
      errors a disclosed subset of excluded; source kinds, correlation modes,
      scenario modes, and failure profiles are never pooled; AC-16 passes.
      Extend the architecture lane's exit-gate traceability map to 008.
