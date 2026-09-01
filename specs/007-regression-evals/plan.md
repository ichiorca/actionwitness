# 007 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

## What makes this milestone different

**A failure this milestone can reproduce is the first thing the product
produces that outlives the session that found it.** 005 made a false success
visible; 006 made it operable by a person and an agent. M6 makes it *portable*:
a JSON file somebody can commit, hand to CI, and run next year against a
different build. Everything below serves that one property, and the failure mode
to fear is a case that passes for a reason other than the one it was generated
to catch.

**Eval status is not business outcome, and conflating them breaks the product.**
§24.3 is explicit: a `reproduce_source` run that faithfully reproduces a
recorded `failed` outcome has eval status **passed** and exit code **0**. The
intuition that "the run failed, so the eval failed" is wrong, and it is the kind
of wrong that looks like a bug report rather than a design decision. T9 exists
to make the distinction structural rather than a comment.

**There is a tripwire waiting.** `tests/evals/test_evals_lane.py` asserts that
`actionwitness_core.evals` has grown *no* public behaviour, and 004 and 005 both
treated it firing as a scope-creep alarm. In this milestone it is the opposite:
T1 is expected to make it fail, and the same change must replace it with real
§24 coverage. That is what the tripwire was for — it forces the coverage to
arrive with the behaviour rather than after it.

**The `evals/` directory on disk is not this milestone's.** It is a local
eval-gate corpus, kept out of the repository wholesale by operator instruction
and protected by `AGENTS.md`. Its own `.gitignore` entry says where M6's output
belongs instead: "the product's own regression evals (M6) live in persisted
artifacts and tests/, not here." Nothing in this milestone writes into it.

---

1. **The core eval vocabulary.** `RegressionEvalCase` and its parts, versioned,
   frozen, and validated on both write and read. The core owns the *shape* of a
   case and the *rules* for matching an expectation; it must not learn what
   `pre_fix` means — §24.4's mapping is target knowledge and belongs to the
   integration layer (T8).

2. **Generation from a terminal run**, and only from one. §24.2 loads immutable
   evidence: contract, initial snapshot, trajectory, policies, findings. A run
   still in flight has no final classification set to record as an expectation,
   so generating from one would embed a prediction rather than an observation.

   Idempotence is a property of the *content*, not of a database check: the same
   source run must produce a byte-identical case, which falls out of §17.2's
   canonical serialization if nothing in the case is derived from a clock or a
   fresh identifier.

3. **Case content.** The subtle rules, each with a reason worth keeping:

   - the source contract is embedded verbatim and its stored hash verified
     *before* case creation — a case built around a contract that had already
     drifted would reproduce nothing;
   - the fixture is minimized, **except** under `no_undeclared_changes`, which
     is defined over paths the contract does not name and therefore needs the
     complete canonical initial state;
   - a read-only call is dropped only when its presence, output, *and* ordering
     are irrelevant to every check and every later mutation;
   - repeated request IDs are preserved, because an idempotency failure is
     exactly a repeated ID and minimizing it away would delete the bug.

4. **Redaction, schema, and hash order.** Sensitive replay-required values become
   deterministic type-valid fixtures (`eval-user@example.invalid`). The hash is
   calculated **last**. Any field written after the hash makes the hash a lie
   about the document that carries it, which is the one defect a portable
   artifact cannot survive.

5. **The isolated eval workspace.** §17.1's `kind: eval`. The workspace is the
   isolation boundary, and a replay that reached into an interactive workspace
   would let a CI run mutate somebody's live demo.

6. **Trajectory replay** through the registered adapter, under the `eval` actor.
   005's classifier already treats `agent` and `eval` alike for exactly this
   reason (AC-15: a replayed run must classify identically to its source), so
   this stage should need no change to the engine — and if it does, that is a
   finding worth surfacing rather than patching around.

7. **Interaction providers.** `recorded_approval`, `recorded_denial`,
   `no_confirmation`. The rule that matters: **no mode synthesizes consent.** A
   missing-confirmation regression runs with `no_confirmation` precisely so that
   correct behaviour blocks the mutation — a provider that helpfully approved
   would turn the test into its own opposite.

8. **Environment profiles**, mapped in the integration layer. `current` is
   always the default and a generated case never silently forces
   `reproduce_source`; §24.4 requires the selected profile to appear in the
   report so a passing eval cannot hide which environment produced it.

9. **Expectation matching.** Overall result *and* the exact critical
   classification set — set equality, not containment. A superset would let an
   unrelated new failure ride along inside a passing eval, which is precisely
   the regression a regression suite exists to catch.

10. **Non-replayable evidence.** §24.3a's `surface` replay, and the
    `non_replayable_policies` list. The list is the honest half: a policy that
    cannot be evaluated is excluded from both sets *and named in the report*, so
    "passed" never quietly means "not checked".

11. **Report, routes, panel**, then **12. the CLI**, then **13. the gate.**
    Cleanup of mutable eval state happens after the report is persisted, in that
    order, for the same reason 005 wrote artifacts before their rows.

---

## Deviations and decisions worth an operator's eye

### Carried in from 006, still open

These do not block T1, but three of them touch this milestone:

- **`ExecutionContext.human_consent_granted`** — a public core protocol
  addition made in 006-T2, flagged for review. T7's interaction providers are
  its second caller, so if it is going to change, better before T7 than after.
- **`runs.fault_active` is never populated.** §24.2 step 9 records the source
  failure profile for provenance; `fault_active` would be the natural companion
  and is still unavailable. A generated case can record the profile without it.
- **Four §11.1 tools remain unimplemented**, two of which are this milestone's:
  `create_regression_eval` and `run_regression_eval`. T11 is where they land.
- **`ComparisonPanel` placeholder props** and **FR-039's unused
  `require_no_lease()`** are unrelated to M6 and stay open.

### The evals tripwire is expected to fire (**not scope creep**)

004 and 005 both carried an instruction that `tests/evals/test_evals_lane.py`
firing meant scope creep. In this milestone it means the opposite. T1 must
delete that placeholder and replace it with §24 coverage in the same commit —
leaving it passing would mean the core eval package still has no public
behaviour, which is the same as saying T1 did not happen.

### Where generated cases and reports live (**already decided**)

Worth restating because the directory name invites the wrong assumption. The
`evals/` tree on disk is a local development rig, gitignored wholesale by
operator instruction on 2026-08-31 and listed among `AGENTS.md`'s protected
paths. Its ignore comment settles the question this milestone would otherwise
have to ask: M6's regression evals "live in persisted artifacts and tests/".

So: generated cases are persisted through 005's artifact store and served by
§15.4's routes; committed example cases, if any, are test fixtures under
`tests/`; the CLI writes reports under `--report-dir`, defaulting to the
already-ignored `.evals/`. **No task here touches `evals/`**, and if one appears
to need to, that is an escalation rather than a judgement call.

### The public JSON Schema does not exist yet

T4 validates a case against "the repository's public JSON Schema" (§24.2 step
10), and there is no such file today. It has to be authored as part of T4 rather
than assumed — and it is a published artifact with compatibility expectations
(definition of done: "fixture formats ... are versioned and documented"), so its
location and versioning are worth a moment's thought rather than a default.

### AC-12 mentions a CLI exit code this milestone must not overreach on

AC-12 asks that a `current` replay "passes, and the CLI exits with code `0`",
which T12 covers. It also describes the `reproduce_source` path selecting the
source scenario "through the registered adapter" — note that this is the
*adapter's* scenario selection (006-T10's `prepare`), not a second mechanism.
If T8 finds itself writing new scenario plumbing, that is a signal it has
drifted into the core.

### Tier 2 gate ordering

The runway places the **Tier 2 gate after 008**, requiring M7 *and* M6 green.
So 007 finishing does not close a tier gate on its own, and a `[T2]` acceptance
criterion passing here (AC-08, AC-12, AC-15) is a milestone result rather than a
tier result. Worth stating because three Tier 2 ACs going green makes it easy to
believe the tier is done.

---

## Deviations ledger (implementation)

Recorded as they were taken, anchored to the spec section each departs from.
`spec.md` is operator-approved and was not edited; every entry below is a
decision made *under* it, not a change *to* it.

### D1 — `ReplayConfiguration.recorded_decisions` extends §24.1's `replay` block

**§24.1 / §24.5.** §24.5 names three confirmation strategies and forbids
inferring consent, but §24.1's illustrative `replay` block carries no place to
put the decision a `recorded_approval` is supposed to replay. Taken literally, a
case could name the strategy with nothing to honour it, and the only ways to
resolve that at run time are to synthesise an approval — which §14 and the
constitution forbid outright — or to fail every such replay.

**Taken:** added `recorded_decisions` to the replay configuration, carrying the
decisions the source run actually recorded. `recorded_approval` and
`recorded_denial` replay one; a strategy with no matching recorded decision
**fails closed** rather than proceeding. `no_confirmation` records no decision
at all, so correct behaviour blocks the mutation.

**Operator's eye:** this is an addition to the case format, which is a published
artifact. It is additive and schema-versioned (`1.0`), so nothing that reads a
case is broken by it, but §24.1's example block in the functional spec no longer
shows the whole structure.

### D2 — `evaluation_cases.source_run_id` carries no foreign key

**§17.1 / FR-082.** The natural reading of the schema makes `source_run_id` a
reference to `runs`. FR-082 makes a case portable — replayable from a clean
checkout, in a database that has never seen the run it was cut from — and the
CLI (T12) is exactly that situation. A foreign key asserts the opposite. The
only ways to satisfy it there are to fabricate a `runs` row, which manufactures
history the harness never observed, or to refuse to record the case, which would
leave the eval run referencing nothing.

**Taken:** the column stays as recorded provenance with no key through it.
FR-080's idempotence is unaffected — it rests on the `UNIQUE (source_run_id,
contract_content_hash, generator_schema_version)` constraint, which remains.
§24.1 keeps a case self-contained, so no code reads through the column.

### D3 — replay observes after every step, to reproduce a classification

**§24.3.** Replaying only the contract's assertions was enough to produce an
outcome but not a *classification*, so `reproduce_source` could not reproduce
the finding set it was asked to match. The fix was to have the replayer observe
authoritative state after each step and build the same `RunEvent` stream a live
run produces, then hand that stream to the existing classifier.

**Worth recording because of what it did _not_ require:** the classifier needed
no change. AC-15 says the engine already treats an `eval` actor like an `agent`,
and that held — the gap was in what replay fed it, not in how it judged.

### D4 — the CLI runs against a throwaway database

**FR-082 / §24.6.** A case handed to CI must run without the harness's own
storage. The command creates a temporary SQLite database, records the case it
was handed into it, runs the service's own `EvalRunService`, and discards the
database when it exits. The alternative — a second, storage-free execution path
— would be a copy of the pipeline that could drift from the one under test.

### D5 — pre-existing ADR-0005 gate failures were left alone

`tests/architecture/test_adr_records.py` fails twice on
`docs/adr/0005-external-evaluator-binding.md`, which is missing its `### Positive`
/ `### Negative` sections and names no rejected alternative. Both failures
predate this milestone (verified against a clean tree) and BUILD_ORDER assigns
"Complete ADR-0005" to **M7**. Fixing it here would be 008 work. Left failing and
reported rather than quietly repaired.

### D6 — T10 shipped only half of §24.3a; T13 completed it

**§24.3a.** T10 was ticked with `non_replayable_policies` implemented and the
`surface` half missing: the case model carried a `surface` section from T1, but
nothing populated it at generation and nothing fed it to the policy engine at
replay. Criterion 5 names that replay explicitly, and §24.3a is unambiguous
about why it exists — without it a `tool_surface_poisoned` case "could never
reproduce its own classification and would fail permanently".

**Taken:** generation now reads the source run's `tool_surface_captured` and
`tool_surface_changed` events into the case, and replay emits them back into the
timeline, with the policy's two facts derived from that stream rather than read
a second time from the case. A run that captured no baseline records `None`, so
`stable_tool_surface` stays `observation_unavailable` exactly as §16.1 requires
rather than passing on a synthesised empty baseline.

**Worth an operator's eye:** nothing captures a surface baseline yet — that is
browser-side work, and the live path leaves the same fields defaulted. So the
replay is correct and tested but has no production baseline to replay *from* in
this milestone. The delta events are numbered after the trajectory rather than
interleaved into it, because the recording does not say where in the trajectory
a delta belongs and inventing a position would assert an ordering nobody
observed.
