# Draft upstream issue — stable per-trial identifiers in the `webmcp-evals` JSON report

**Status: drafted, not filed.** Filing is an outward-facing act on somebody
else's repository, and ADR-0005 reserves it for the operator's go-ahead. This
file is the text, ready to paste; nothing here has been sent anywhere.

- **Target:** `GoogleChromeLabs/webmcp-tools`, package `webmcp-evals`
- **Verified against:** v0.0.4, commit `fe33c1b` (released 2026-08-28), read live
  on 2026-09-01 — `src/report/report.ts` and `src/types/evals.ts`
- **Asked for by:** spec §25.3, "a public upstream issue proposing stable trial
  IDs in the JSON reporter, linked from the README as a first-consumer
  contribution"
- **Our position:** a consumer's request, not a defect report. The reporter does
  what it was built to do; this is about what a second system needs in order to
  cite it.

## Before filing

Re-read the two files above at current HEAD first. This draft is accurate as of
`fe33c1b`, and a report shape that has since changed would make the issue read as
though nobody looked. If the shape has moved, update this file and ADR-0005
together — the pin and the issue have to describe the same thing.

---

## Title

Report: emit a stable per-trial identifier so results can be cited by external consumers

## Body

Hello — this comes from building a downstream consumer of the JSON reporter, and
it is a request rather than a bug report. Everything below is from reading
`src/report/report.ts` and `src/types/evals.ts` at v0.0.4 (`fe33c1b`).

### What the report gives a consumer today

```ts
TestResult  = { test, response, outcome: "pass" | "fail" | "error",
                trajectory?, browserConsoleErrors?, runIndex?, stepIndex? }
TestResults = { results: TestResult[], testCount, passCount, errorCount, failCount }
```

There is no per-trial identifier. The nearest thing to an address is the pair
`(test.name, runIndex)`, and both halves are optional — `runIndex` on
`TestResult`, and `name` on the `Eval` type that `test` refers to. So a report may
legitimately contain trials that cannot be addressed at all, and it may contain
several trials that share whatever address they do have.

### Why that matters to a consumer

We correlate each trial with an independently observed outcome recorded on our
side, and then publish both layers together. To do that we have to say *which*
trial an observation belongs to.

Without a stable identifier, the available options are ordering, timestamps, or
matching on the prompt text. All three are heuristics, and a heuristic presented
as a correlation is worse than no correlation — it produces a confident report
that is wrong in exactly the cases anyone would care about (a suite where two
trials share a name, a re-run whose order differs, a partial report).

So we bind explicitly and fail closed instead: a trial is addressable only when
`test.name` and `runIndex` are both present *and* the pair is unique within the
report. Anything else is reported to the operator as unbound, for manual
selection. That is a correct outcome and we are not asking you to change it — but
it means part of an otherwise good report can be uncitable for a reason the
person who ran it cannot see or fix.

### What would resolve it

An opaque per-trial `id` on `TestResult`, unique within a report and stable
across re-serialization of the same run:

```ts
TestResult = { id: string, test, response, outcome, ... }
```

Properties that would make it useful, in rough priority order:

1. **Unique within the report.** This is the whole ask; everything else is a
   refinement.
2. **Present on every trial**, including ones with no `name` and no `runIndex`.
   The trials that currently lack an address are precisely the ones a consumer
   cannot work with.
3. **Stable for the same run** — regenerating the report from the same results
   yields the same ids, so a consumer can re-import without duplicating.

Deterministic derivation would be a bonus rather than a requirement: something
like a hash of `(suite, test index, runIndex, stepIndex)` lets two consumers of
the same report agree without coordinating. A UUID per trial satisfies (1)–(3)
and would already solve our problem.

A smaller alternative, if adding a field is unwelcome: make `runIndex` always
present in report output (defaulting to `0`) and document `test.name` as required
for reported evals. That closes most of the gap without changing the shape,
though it still leaves same-name collisions.

### Compatibility

Adding a field is additive — existing consumers ignore unknown keys, and ours
validates a pinned schema and would simply start using the new field after we
re-pin. If it helps, we are happy to pin the version that introduces it and
report back on how it behaves against real reports.

Happy to send a PR if the shape above is roughly what you would want. Thanks for
publishing the reporter in the first place — the schema being readable is what
made this analysis possible at all.

---

## Notes for whoever files this

- Link the filed issue back into `README.md` (the Tier 2 import section) and into
  `docs/adr/0005-external-evaluator-binding.md`'s follow-up, replacing the
  "not yet filed" note in both.
- Do not describe our importer's refusals as a problem upstream caused. They are
  our fail-closed choice, and §25.3 frames this as a first-consumer
  contribution — the tone above is deliberate and worth keeping.
- Nothing in this text contains a store origin, a credential, a workspace
  identifier, or any content from a real report.
