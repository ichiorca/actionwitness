# Checked-in evaluator fixtures (Tier 2 — AC-16, FR-101)

This directory holds the redacted, version-pinned `webmcp-evals` JSON report
fixture(s) used by CI and by the offline benchmark fallback. Requirements:

- generated from the frozen benchmark manifest against the pinned evaluator version;
- at least three scenarios × three trials, including call-level passes whose
  deterministic outcome fails (classified `silent_outcome_defect` downstream)
  and one deliberate `error` trial, which the import reports as excluded
  rather than guessed at;
- redacted before commit. The `recorded_fixture` / `silent_outcome_defect`
  labels are applied by the import and correlation path when the suite is
  created — an evaluator report carries neither, and a suite built from this
  file must never be labelled `live_model_run`.

## What is checked in

`tier2_three_scenarios.json` — nine trials across three scenarios, satisfying
AC-16. The three scenarios are three *configurations of one intent* rather than
three unrelated journeys, because one contract judges a whole suite today
(008 deviation D3). They are:

| Scenario | Target | Call level | Outcome |
|---|---|---|---|
| against the faulty build | `pre_fix` + discount fault | 3 pass | 3 fail — the `silent_outcome_defect` |
| against the corrected build | `post_fix` | 3 pass | 3 pass |
| discount step omitted | `post_fix` | 2 fail, 1 error | 2 fail, 1 excluded |

The scenario mode and failure profile are **not** in this file: an evaluator
report says what a model called, not what it called it against. They are declared
per scenario when the benchmark suite is created (§24.7 step 1).

The trajectory element shape (`{name, arguments}`) is this project's assumption
rather than a verified upstream contract — see deviation D1 in
`specs/008-evaluator-import/plan.md` before regenerating this file from a real
evaluator run.
