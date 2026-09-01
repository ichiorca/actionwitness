# 008 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

## What makes this milestone different

**This is the milestone where the product's central claim becomes a number.**
005 made a false success visible, 006 made it operable, 007 made it portable.
M7 joins the two evaluation layers: a call-level evaluator says the model picked
the right tool with the right arguments, and ActionWitness says whether the
business state agrees. The cell where those disagree — call-level pass, outcome
fail — is the whole thesis, and the fixture set is required to contain one,
labeled `silent_outcome_defect`.

**Nothing here couples the two engines.** The constitution's primitives table is
explicit that this project does not reimplement tool-selection scoring, and §24.3
says the correlation engine "joins those separate results through immutable
references; it does not couple either engine's execution, rewrite either
artifact, or collapse their meanings into one accuracy score." A benchmark
artifact *references* its sources. If a stage finds itself editing an imported
evaluator artifact or an outcome report, it has left the milestone.

**Imported reports are untrusted input, and the order of operations is the
control.** §26.2 and the safety rails put size and schema validation *before*
parsing, and redaction *before* persistence and hashing. Those are sequence
requirements, not a checklist: a report that is parsed before its size is
checked has already spent the memory, and a hash computed before redaction
commits the unredacted bytes to the evidence chain permanently.

**Binding is explicit or it does not happen.** BUILD_ORDER is unusually direct:
"do not guess from order, timestamps, or similar text." A heuristic binding
would silently attribute one trial's outcome to another trial's call evidence,
which is exactly the error this product exists to catch — committed by the
product itself.

**Zero denominators are a real case, not an edge case.** A suite where every
trial is excluded has no ratio to report. The metrics stage must decide what
that renders as before any UI reads it, because a `0/0` rendered as `0.0000` is
a claim, and a wrong one.

**Reuse 007's replay; do not fork it.** "Isolated imported-trajectory replay
through the registered adapter and deterministic confirmation provider" is the
007 machinery under a different trajectory source. Eval workspaces, the adapter
resolution, and the interaction providers already exist. If the imported
trajectory needs the runner to change shape, surface it rather than copying the
runner.

**ADR-0005 is a prerequisite, not paperwork.** It is currently incomplete and
two architecture-lane tests fail on it today — they are the first thing T1
fixes, and until they pass the milestone's own gate cannot be trusted.

---

1. **ADR-0005 and the pinned reporter contract.** Complete the record, and pin
   the supported `webmcp-evals` reporter fixture/schema and normalizer version.
   Pinning is what makes "the exact allowlisted schema" a checkable statement
   rather than a preference.
2. **Benchmark persistence.** Suite and trial tables, plus the source/derived
   artifact relationships. Derived references source; source is never rewritten.
3. **Import limits and schema validation.** 1 MiB and 100 trials enforced
   *before* parsing; the exact allowlisted schema validated; redaction applied
   *before* persistence and hashing.
4. **Normalization.** Only the required call-selection, arguments, order, error,
   trajectory, and reproducibility fields. Unsupported metadata is preserved as
   `null` — recorded as unsupported rather than dropped, so a later reader can
   tell "absent" from "not understood".
5. **Explicit one-to-one binding.** Validated and saved before the suite becomes
   ready. No inference from order, timestamps, or text similarity.
6. **Imported-trajectory replay.** Eligible trials replay in isolated eval
   workspaces through the registered adapter and a deterministic confirmation
   provider — 007's machinery, not a second copy of it.
7. **Metrics from integer counts.** Coverage, exclusions, the 2x2 matrix,
   zero-denominator behavior, four-decimal presentation strings,
   per-scenario/profile breakdowns, and the five required metrics. Counts are
   integers; the four-decimal strings are presentation, computed last.
8. **Atomic finalization.** One immutable benchmark artifact, created
   atomically, referencing but never rewriting its sources.
9. **Surfaces.** Import/binding/replay/finalize APIs (§15.6) and
   `BenchmarkPanel` carrying source kind, mode, coverage, evidence links, and
   interpretation guardrails.
10. **Fixtures.** At least three scenarios with three trials each, including the
    `silent_outcome_defect` trial.
11. **The exit gate.** The fixture path with Node unavailable and no
    credentials; the fail-closed matrix; the counting identities; no pooling
    across source kinds, correlation modes, scenario modes, or failure profiles;
    AC-16.

---

## Deviations ledger (implementation)

_Empty until implementation begins. Record each departure from the spec here,
anchored to the section it departs from, with what was taken and why._
