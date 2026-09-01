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

Each departure from the spec, anchored to the section it departs from, with what
was taken and why.

### D1 — the imported trajectory element shape is unverified upstream

**ADR-0005 / §24.7 step 4 / AC-16.** ADR-0005 verified the pinned reporter's
`TestResult` shape from live upstream source and recorded that `trajectory` is
an *optional array* — but it did not record what an element of that array looks
like. Nothing in the repository does.

**Taken:** the importer requires each step to be `{"name": str, "arguments":
object}` and keeps only those two fields (FR-086 makes that a safety property,
not a convenience). A step that does not match is not guessed at: the whole
trajectory becomes unusable, the trial is excluded as `missing_trajectory`, and
its call-level result still counts toward coverage. So an unrecognised real
shape degrades to honest coverage loss rather than to an invented replay.

**The risk the operator accepted (2026-09-01).** That element shape is now
load-bearing in six places — normalized (T4), executed (T6), counted (T7),
published in the hashed artifact (T8), accepted by the import route (T9), and
rendered in the panel (T10) — and T11's checked-in fixture is written to the
same assumption. AC-16 therefore goes green against a shape this project chose
rather than one observed in a real `webmcp-evals` report: the normalizer accepts
the fixture because both were written here. **The call-level half of AC-16 is
verified; the replay half is verified only against our own fixture.**

**What would settle it:** one real report from the pinned reporter, or the
upstream `trajectory` element type. If it disagrees, the change is confined to
`_trajectory` in `integrations/google_evals/normalize.py` and the fixture — the
core, the matrix, and the artifact format are unaffected, because nothing
downstream of normalization knows the reporter's field names.

### D2 — scenarios carry the target configuration, not the report

**§24.7 step 1 / §17.1.** Writing the fixture exposed a gap: a
`silent_outcome_defect` requires the replay to run against the *faulty* build,
and nothing carried a scenario mode or failure profile onto a trial. An
evaluator report says what a model called; it says nothing about the
configuration those calls ran against.

**Taken:** `ScenarioDefinition` joins the manifest (§24.7 step 1 names exactly
these fields), and `record_import` stamps each trial with its scenario's mode
and failure profile into §17.1's existing `benchmark_trials` columns. A scenario
the manifest does not describe leaves the trial `null` — never inferred — and
the replay then runs against the target default and says so.

Without this the suite would have measured the corrected implementation and
reported no silent defects, because none were provoked.

### D3 — one contract judges every scenario in a suite

**§24.7 step 1.** The spec gives each scenario its own contract hash.
`ScenarioDefinition` carries `contract_content_hash`, but the replay currently
judges every trial against the *workspace's selected contract*; per-scenario
contract resolution is not implemented.

**Consequence, and why the fixture is shaped as it is:** the three checked-in
scenarios are three configurations of one intent — faulty build, corrected
build, and omitted discount step — rather than three unrelated journeys. That
satisfies AC-16's "at least three scenarios with at least three completed
trials each" honestly, and it is also the reading §24.7 step 1 supports, since a
scenario there is an intent *plus* a configuration. A suite mixing genuinely
different intents would need per-scenario contracts first, and would otherwise
judge every trial against a contract most of them never targeted.
