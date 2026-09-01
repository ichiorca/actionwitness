# ADR-0005 — External evaluator version and binding

- **Status:** Accepted
- **Date:** 2026-09-01
- **Implementing change:** 008-evaluator-import (M7); consumed read-only by 010 (M9)

> Drafted from live upstream verification (2026-09-01), not from memory;
> operator accepted the pin the same day. 008-T1 consumes this record.

## Context

Spec §25.3 and §9.9 import the Google `webmcp-evals` reporter's JSON into the
Tier 2 dual-layer benchmark. The evaluator is experimental and unversioned in
places, so what exactly is pinned — package version, report schema, trial
addressing — decides whether AC-16's fixture path stays reproducible.

Verified against `GoogleChromeLabs/webmcp-tools` HEAD (2026-09-01):

- The scaffold's study pin `d39eae4` is now 5 commits behind HEAD. Upstream
  released **`webmcp-evals v0.0.4`** on 2026-08-28 (commit `fe33c1b`).
- The diff `d39eae4..HEAD` touches only backend/evaluator internals
  (`backends/vercel.ts`, `localEvaluator.ts`, package files). **The report
  writer (`src/report/report.ts`) and the result types (`src/types/evals.ts`)
  are unchanged** — the studied schema and the released version agree.
- The trial shape, from the live source:

      TestResult  = { test, response, outcome: "pass"|"fail"|"error",
                      trajectory?, browserConsoleErrors?, runIndex?, stepIndex? }
      TestResults = { results: TestResult[], testCount, passCount,
                      errorCount, failCount }

- **Sharper than §25.3 records it:** not only is there no stable per-trial
  identifier — `runIndex` is *optional*, and `test.name` is *optional* on the
  `Eval` type. A report may contain trials with no usable address at all.

## Decision (pending operator approval)

1. **Pin `webmcp-evals` v0.0.4, commit `fe33c1b`**, for both the Tier 2 report
   schema and any Tier 3 lockfile-installed live execution. A released tag
   with a report shape byte-compatible with the studied `d39eae4` beats
   pinning an untagged commit (provenance) and beats HEAD (floating).
2. **Report schema pin:** the `{config, results}` document with the
   `TestResults`/`TestResult` shapes above, labeled `reporter_schema:
   "webmcp-evals/0.0.4"` in normalized artifacts. Unknown top-level fields
   refuse; unknown per-trial metadata normalizes to `null` (§12's
   preserve-as-null rule).
3. **Normalizer version 1** (`integrations/google_evals`), recorded in every
   normalized artifact beside the reporter schema.
4. **Binding is explicit and fails closed on weak addressing** (FR-091): a
   trial is addressable as `(test.name, runIndex)` only when BOTH are present
   and the pair is unique within the report; any absent or duplicate address
   makes that trial bindable by explicit operator selection only, and never by
   order, timestamps, or text similarity. `imported_trajectory_replay` is the
   supported correlation mode; ambiguous `executed_browser` binding stays
   disabled (BUILD_ORDER ADR-0005 row).
5. **Limits before parsing:** 1 MiB report, 100 trials (FR-090); every field
   untrusted; redaction precedes persistence and hashing.
6. **Mode posture:** import supports the browser- and local-mode reports;
   `smoke` mode is a diagnostic and is never presented as the probabilistic
   side of the benchmark (§25.3).

## Consequences

- Positive: the fixture path is reproducible against a released version; the
  importer's refusals are decidable from the pinned types rather than from
  guesses; 010 inherits the same pin for live execution.
- Negative: v0.0.5+ may change the report shape at any time — the pin means
  deliberately NOT following until a superseding record re-verifies; the
  optional-address finding means some upstream reports may be partially
  unbindable, which is upstream's limitation surfaced honestly, not ours.

## Rejected alternatives

- **Pin `d39eae4`** — the studied commit, but untagged; identical schema with
  worse provenance.
- **Track HEAD** — forbidden by the constitution's pinning rules and §25.3.
- **Vendor the reporter types** — needless; the shapes are recorded here and
  asserted by the importer's own schema validation.

## Follow-up

§25.3 asks for a public upstream issue proposing stable trial IDs in the JSON
reporter, linked from the README as a first-consumer contribution. The
optional-`name`/`runIndex` finding above strengthens that proposal. Filing is
an outward-facing act reserved for the operator's go-ahead.
