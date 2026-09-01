# 005 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

The organising fact of this milestone is that **it is the first time the product
does what the product is for.** Everything before now built the surfaces: a
target that can lie (003), storage that keeps evidence honestly (004), a core
that can evaluate a contract (002). M4 is where a tool reports success, an
independent observation disagrees, and the harness says so. Exit-gate item 1 is
that sentence made executable.

The second organising fact is that 004 already decided the hard parts of *how*.
`BEGIN IMMEDIATE` with one owner per unit of work, the per-workspace lock as
admission control, workspace scoping in the `WHERE` clause, the §15.8 envelope,
FR-008's ceilings checked inside the transaction, and the rule that nothing is
held across a wait — all of that exists and is tested. M4 adds journey logic on
top of it and must not reopen any of it. Where this plan says "one transaction",
it means the `Database.transaction()` that 004 built.

1. **Arm as one transaction** — authorize, validate the immutable configuration,
   capture one authoritative initial observation, validate preconditions, create
   the run, snapshot, and events, and derive guidance. All of it commits
   together or none of it does: a run that exists without its `before` snapshot
   has no baseline to compare against, and FR-012 fixes the configuration at
   arming precisely so that "completed evidence is never relabeled".

   Note the ordering constraint 004 imposes: the observation is I/O, and nothing
   may hold a transaction across a wait (ADR-0003). So the observation is
   captured *before* the transaction opens, and the transaction validates it and
   writes it. That is the shape M5's confirmation flow needs too.

2. **Generic target-tool invocation** — Python validation, run-state checks,
   event-cap reservation, start event, adapter dispatch, immediate authoritative
   effect observation, and **exactly one** terminal event. The invocation is
   deliberately generic: §9.1's protocols are the only thing it knows about the
   target, and a branch on a tool name here would put commerce semantics in the
   harness.

   The event-cap reservation is 004's `trip_if_event_budget_exhausted`, which
   already returns its refusal rather than raising so the boundary event commits
   (FR-008). This stage is its first real caller.

3. **Invocation evidence** — redacted inputs, bounded output, status, timing,
   request and correlation IDs, state versions and hashes, and effect-path
   evidence. §20.3 requires redaction "before persistence, hashing, or export",
   so redaction happens before the `Observation` is built rather than before it
   is written.

4. **The exclusive run mutation lease and the verification race gate** — a new
   target action arriving after verification has begun must lose *cleanly*, with
   `RUN_ALREADY_VERIFYING` and no partial snapshot (exit-gate item 3). This is
   the milestone's sharpest concurrency requirement, and it is a transaction
   boundary question rather than a lock question: the per-workspace lock is
   admission control and correctness never depends on it (ADR-0003).

5. **Verification** — final observation capture, assertion, trajectory, and
   policy evaluation, findings, the layered report, and the immutable terminal
   transition. The evaluation itself is already implemented and tested in the
   core (002); this stage is the I/O and persistence around it, and must not
   re-derive a verdict the core owns.

6. **The false-success classifier** — the last relevant intended-effect action
   and its immediate authoritative post-call evidence. §12.2 forbids inferring
   an effect the adapter did not declare, so an adapter with an empty effect map
   loses causal attribution and nothing else.

7. **Server-derived guidance** — `GuidanceState`, append-only guidance events,
   and the same compact `next_action` projection the tools return. 004 shipped a
   deliberately minimal `next_action` derived from workspace columns and
   fabricated no `guidance` field; this stage replaces that projection with the
   real one, and the two must not coexist as separate answers to the same
   question.

8. **Scenario switch/reset and matched comparison** — controlled-input hashes
   and actual trajectory identity. A mismatched rerun is *valid* and returns
   `not_comparable` with the differing fields (exit-gate item 4); it is not an
   error, and treating it as one would hide the most common real result.

9. **Paged events and report/comparison endpoints** — §15.3's
   `after_sequence`/`limit` paging over the append-only stream 004 built, plus
   the report and comparison reads.

Cross-cutting:

- **The tool's self-report is never the outcome.** Constitution §5 and the whole
  point of the product: a verdict requires an independently sourced
  authoritative observation, and `ToolExecutionResult` and `Observation` are
  unrelated types with no conversion between them. Every stage here handles both
  and must keep them apart.
- **Exactly one terminal event per invocation.** Two would make the timeline
  ambiguous about what actually happened; zero would make a hung invocation
  indistinguishable from one that never started.
- **Nothing is held across a wait.** Adapter dispatch and observation are I/O.
  The transaction opens after them and closes before the next one.
- **004's boundaries are settled.** Workspace scoping, the error envelope, the
  ceilings, the lock manager, and the repositories are tested and must be used
  rather than re-implemented. A change to any of them in this milestone is a
  signal to stop and check whether it is really an M4 change.

## Deviations and decisions worth an operator's eye

_Recorded per task as the milestone proceeds, anchored to the spec section each
answers to — the convention 002, 003, and 004 use._

### Carried in from 004, still open

- **`maximum_mutations` has no §22 classification** (from 002). Mapped to
  `idempotency_violation`; needs a decision before M11.
- **ADR-0004's literal ±(2^53 − 1) bound contradicts its own round-trip
  rationale** (from 002). Implemented the rationale; needs a superseding record
  or a corpus change.
- **The Buggy Store's three project-allocated endpoints and its
  `X-Workspace-Id` header** (from 003) are still awaiting confirmation. 004 did
  not canonize them and M4 must not either — the harness reaches the store only
  through `integrations.buggy_store`.
- **`TargetDescriptor` has no `supported_fault_profiles`** (from 004-T11). The
  failure-profile token is stored opaquely and validated by the adapter at
  arming, which is this milestone's stage 1. Either add the descriptor field or
  accept validation-at-arming; the decision lands here.
- **The guidance half of `GET /workspace`** (from 004-T11) was deferred rather
  than stubbed. Stage 7 is where it arrives.
- **A tampered stored contract returns `HARNESS_ERROR`** (from 004-T12) because
  no evidence-integrity code exists in the registry. Whether M4's evidence-chain
  verification introduces a distinct code is an operator decision.

### T1 — FR-030's "inside the workspace transaction" vs ADR-0003 (**operator decision**)

FR-030 says arming "shall read canonical state once inside the workspace
transaction, validate preconditions against that exact value, persist it as the
initial snapshot". ADR-0003, Accepted and binding, says nothing async holds a
lock or a transaction across a wait. Capturing canonical state is an HTTP call
to the target, so the two cannot both be satisfied literally.

Implemented as: read **once**, validate against **that exact value**, persist
**that exact value** — with the transaction opened after the read. Every
substantive clause of FR-030 holds; only the literal placement of the
transaction boundary differs.

The reasoning, so an operator can overrule it knowingly:

- Holding the SQLite write lock across an external HTTP call would stall every
  other workspace in the process and start tripping busy timeouts unrelated to
  the slow target.
- It would buy nothing. Canonical state lives in the *target*, not in this
  database, so a SQLite transaction gives no isolation against it changing. The
  literal reading protects nothing the implemented reading does not.

What the transaction does own is the genuinely racy part: the configuration is
re-read inside it and compared against what was read before the capture, and a
mismatch refuses rather than arming against a selection nobody made. That is
FR-012 under concurrency, and it has its own test.

**Queued for the operator:** confirm this reading of FR-030, or direct that the
capture move inside the transaction and accept the serialization cost.

### T1 — arming does not reseed the target

BUILD_ORDER §7/M4 lists "capture one authoritative initial observation" at
arming; reseeding through the adapter is T10's "scenario switch and reset". So
arming observes and does not call `prepare()`. A run armed against whatever
state the target already holds is the honest default — preconditions are what
decide whether that state is acceptable.

### T1 — `fault_active` is left at its default

§17.1's `runs.fault_active` cannot be derived by the harness: whether a profile
is active in a given scenario mode is target semantics, and §9.1 forbids the
harness from interpreting mode names. The selection itself (`scenario_mode`,
`failure_profile`) is recorded faithfully. The adapter reports activation, so
the column is populated in T10 where the adapter is consulted.

### T1 — event order is `run_armed` then `snapshot_captured`

FR-030's prose orders the *persistence* (snapshot, then run) but the events
table hangs off `runs`, so the run row must exist first regardless. In the
timeline the run's own creation is sequence 1: a `snapshot_captured` at
sequence 1 would describe a run that, by its own timeline, did not yet exist.

### T1 — `POST /runs` takes no contract identifier

§15.3 describes arming "a contract", and FR-024 already made exactly one
contract active with its target selected atomically. Accepting an identifier
here would reintroduce the combination FR-024 forbids, so the run is armed
against what the workspace has selected — the same thing `GET /workspace`
reports.

### T1 — proposal mode is declared and refused (**operator decision**)

§15.3's `mode` accepts `verification` or `proposal`. BUILD_ORDER §7/M4 scopes
this milestone to the verification slice and lists nothing about candidate
derivation or curation, so `proposal` is refused explicitly rather than silently
downgraded — the 003 pattern for an unimplemented option. **Queued for the
operator:** which milestone owns proposal mode? §32's cut order puts it at
priority 5 and AC-23 marks it `[T1]`, but no BUILD_ORDER milestone lists it.

### T2 — guidance derivation lives in the core, not the service

§26.1's locked decision: "Guidance state and `next_action` shall be derived from
the same server lifecycle state for the UI, WebMCP responses, and audit trail."
Three surfaces, one derivation — so it is a pure, total function in
`actionwitness_core.journeys.guidance` rather than three renderers in three
places. Two implementations would agree in testing and diverge in exactly the
situation guidance exists for: the one where a person and an agent disagree
about whose turn it is.

`GuidanceState.next_action()` is that same object narrowed, not a second
derivation, and a parametrized test asserts the two agree for every phase.

### T2 — `WorkspacePhase` and `GuidanceActionCode` added to the core registry

`WorkspacePhase` transcribes §11.5's normative state diagram — it is a
*workspace* phase, distinct from `RunState`, because FR-120 starts producing
guidance before any run exists. All thirteen members are present even though
four are not yet reachable, and a test asserts the copy table is total over the
enum: a phase with no entry renders an empty banner, which is worse than a wrong
one because nobody can tell it is empty by looking.

`GuidanceActionCode` is **project-allocated**. FR-120 and FR-121 require an
`action_code` on every guidance event and every tool `next_action`, and require
the banner and the tool result to "resolve from the same server state and action
code" — but the specification enumerates no vocabulary. Ten codes, registered so
the UI, the tools, and the audit trail share one set of names.

### T2 — 004's placeholder `next_action` was removed, not left beside the real one

004-T11 shipped a deliberately minimal `next_action` returning bare strings
(`"select_target"`, `"arm_run"`, …) and fabricated no `guidance` field. That
projection is now deleted and `GET /workspace` serves both halves from the one
`GuidanceState`. Leaving it would have created a second answer to "what should I
do next", which is precisely what FR-120's "the frontend shall not invent a
conflicting next action" forbids — and the wire shape of `next_action` changed
from a string to FR-121's compact object, so four 004 tests were updated to the
real contract.

`"select_target"` disappeared with it: §11.5's diagram has no such state, and
FR-024 makes contract and target selection atomic, so a workspace holding a
contract without a target cannot exist.

### T2 — the two guidance streams are joined by the guidance event's own id

§12.13: "Guidance before a run exists is recorded in the separate
workspace-scoped `guidance_events` stream. After arming, `guidance_transitioned`
is also appended to the run timeline using the same guidance-event ID." The run
event carries that id in its payload, and a test asserts the identity rather
than merely that both rows exist — two rows written at the same instant are not
linked, and a reader reconstructing "who was asked to do what" would be guessing
from timestamps.

The run event's actor is `harness`, not the guidance's own `active_actor`: the
event records that the *server* moved guidance, not that the person or agent it
is addressed to did something.

### T2 — `workspace_version` is allocated like an event sequence

`MAX + 1` scoped to the workspace, inside the appending transaction with the
write lock already held (the ADR-0003 pattern). §17.1 names the column but fixes
no semantics; this gives guidance an ordering for the stretch *before* any run
exists, which the run timeline cannot cover.

### T2 — copy rules are tested as behaviour, not style

FR-122 says "copy shall not imply that an agent can make a human decision", and
that promise breaks as a sentence rather than a type error. So the guidance lane
asserts that no instruction addressed to the agent contains approve/authorize
language, that only `human_approver` is ever asked to decide a confirmation,
that every waiting phase says what it is waiting for (FR-124), and that `system`
is the active actor only while it is genuinely working.

### T3 — the canonical state columns hold the observation, never the tool's claim

The sharpest decision in this task. `ToolExecutionResult.state_version_after` is
the version the *tool's own response body* claimed; `events.state_version_after`
is what the observation provider independently saw. FR-032 calls the column
value "canonical", and canonical means observed.

So the two channels are recorded in different places: the observed values fill
the `state_version_*` / `state_hash_*` columns, and the tool's claim lives in
the event payload under a `reported` key beside an `observed` sibling. A tool
that reports success while changing nothing therefore produces an event whose
`reported_status` is `success` and whose observed state hash is unchanged —
which is the disagreement the product exists to surface. Collapsing them into
one column would pass every other test in the suite and delete the only evidence
that matters, so there is a test that arms the real discount fault and asserts
both halves.

### T3 — two observations per invocation

FR-032 requires canonical `state_version_before` *and* `state_version_after`, so
the pipeline observes on both sides of the dispatch rather than carrying the
previous event's "after" forward as the next "before". A derived value would be
wrong the moment anything changed out of band, and out-of-band change is exactly
what an assurance harness must be able to see. The cost is two target reads per
call, accepted deliberately.

### T3 — a closed-subset schema validator in the core rather than a dependency

A general JSON Schema library is built to be permissive about what it does not
recognise: an unknown keyword is ignored and the document validates. FR-021
keeps the declarative surface "to allowlisted scalars" and §11.4 calls these
schemas closed, so `actionwitness_core.ports.schemas` implements the exact
subset the published specs use and **refuses a keyword it does not implement**.
That turns a silently-ignored constraint into a loud failure when the tool spec
is written. No new dependency, and the closure FR-021 asks for is enforced
rather than assumed.

Defaults are applied here rather than inside the adapter, so the arguments that
reached the target are the arguments the timeline recorded.

### T3 — an unobservable target before the call refuses; after the call does not

With no baseline there is nothing to compare against, so a pre-call observation
failure dispatches nothing. After the call the invocation has already happened:
refusing to write its terminal event would leave the timeline claiming a call
that never ended, so the absence is recorded (`observed.available: false`,
null state hash) and the verdict deals with it. Constitution §5's "never
degrades to success" is satisfied by recording the absence, not by hiding it.

### T3 — FR-008's ceiling refusal is raised outside the transaction

`trip_if_event_budget_exhausted` returns its refusal so the boundary event
commits (the 004-T8 shape). `_start` therefore returns a sentinel and
`_start_or_trip` raises after the `async with` closes — raising inside would
roll back the very evidence explaining why the run stopped, which is the bug
004-T8 caught and this must not reintroduce.

### T3 — `tool_identity_hash` is recorded but not compared

§15.3 accepts it as "the identity of the tool definition as observed immediately
before dispatch, which FR-169 compares against the armed baseline". It lands on
the start event; the comparison, the armed baseline, and
`tool_identity_mismatch` are FR-169's own work and belong to the tool-surface
task. **Queued for the operator:** which milestone owns FR-169 — no BUILD_ORDER
milestone lists it, and `tool_surface_poisoned` is a Tier 2 profile.

### T3 — the fault tests set the store's scenario directly, and say why

The harness records a scenario selection (004-T11) but does not yet reseed the
target through the adapter — that is T10. A test needing a genuinely faulty
target therefore drives the store's own `/demo` surface rather than setting a
harness column and hoping. Discovered by a failing test: the first version
selected `pre_fix` on the harness and the store cheerfully applied the discount,
because nothing had told it to misbehave.

### T4 — redaction happens once, at capture, and the verdict uses the redacted value

§20.3 requires redaction "before persistence, hashing, or export". Evaluation is
not in that list, which leaves a choice: evaluate against the unredacted
observation and store the redacted one, or redact once and use that for
everything.

Redacting once wins, because the alternative produces a verdict a reader of the
evidence cannot reproduce — and reproducing a verdict from stored evidence is
what replay (§24) is. A contract that asserts on a redacted path therefore
fails, which is the right answer: a contract should not be asserting on a
secret. Verified against the real store payload before wiring it in, so no
existing assertion path is touched by the default keys.

The policy comes from the contract the run was **armed against** (FR-025), not
from whatever is selected now — a policy that drifted would redact this run's
evidence by a rule it was never run under.

### T4 — absent is not null, and unknown is not unchanged

Two distinctions in `effect_evidence`, both of the kind a plausible
implementation collapses:

* a path that does not resolve is a question the observation cannot answer,
  while a path resolving to `null` is an answer. Reported as separate
  `before_present` / `after_present` flags rather than folded into the value.
* an observation that could not be taken makes every declared path
  **unknowable**. Reporting `changed: false` there would claim the harness
  watched something it never saw, and `changed: true` would infer a movement
  from a failed read. Both are `None`.

The first version of this had a real bug: it checked `resolution.value is
MISSING`, but `resolve` returns a `Resolution` carrying a `found` flag, so
absence was never detected and every missing path read as a present `null`.
Caught by printing the output rather than by a test, which is why the absent
cases now have their own tests.

### T4 — `changed` is decided on the stored values

Redacted and bounded, not on the originals. A comparison against something that
was never persisted could not be re-derived by a reader of the evidence, so two
values differing only beyond the truncation bound compare equal — and a test
says so, rather than leaving it as an accident of implementation.

### T4 — a broad `except` around the post-call observation, and what makes it safe

An adapter is foreign code and may fail in any way its transport does, so the
post-call read catches broadly rather than enumerating exception types. That
breadth hid a real defect during this task: an edit that added a parameter to
`_observe` did not reach the call inside `_observe_or_none`, and the resulting
`TypeError` was swallowed as "target unobservable".

The counterpart test caught it — `test_an_honest_mutation_moves_the_observed_state`
exists precisely so a bug making every observation fail cannot pass silently,
and it earned its place on its first run. The comment at the catch now names it,
so a future reader knows what is holding the breadth safe.

### T5 — the race is one `UPDATE … WHERE status = 'running'`

FR-038's load-bearing word is **atomically**. The precondition check and the
status change are a single conditional update inside one transaction, so two
concurrent verify requests cannot both observe `running` and both proceed. A
check-then-update written as two statements passes every single-client test in
the file and admits two verifications under load — and two verifications mean
two final snapshots, which is exactly the "partial final snapshot" the
requirement's last sentence forbids. Tested with two concurrent requests and
again with eight, because two can agree by luck.

### T5 — "in flight" is read off the timeline, not from a flag

An invocation is in flight when its start event has no terminal event under the
same correlation id. A flag would be simpler and would not survive a restart: a
server that died mid-invocation would come back with the flag cleared and verify
over the top of a call that never finished. The test writes an orphaned start
event rather than setting a flag, so it is testing the definition rather than
the bookkeeping.

### T5 — the checks are ordered for the accurate reason, not the requirement's order

FR-038 lists "at least one terminal event" before "nothing in flight", but in a
`running` run the first can only fail *because* of the second — the timeline is
append-only, so a terminal event never disappears. Checking in-flight first
therefore reports why verification is waiting instead of reporting that nothing
has happened. The completed-action check remains as a defensive backstop and
says so.

An `armed` run is refused earlier still, by the core's transition table:
`armed` leads to `running`, `cancelled`, or `error` and never straight to
`verifying`, so §16's invalid-transition mapping answers and the gate needs no
second opinion.

### T5 — `/verify` returns 202, and does only the gate

Capturing the final observation and evaluating the contract belong to the
verification task; this decides *whether* verification may start. The split is
what makes "no racing request may capture a partial final snapshot" true by
construction: the race is settled before any observation is taken, so a loser
has nothing to capture. 202 rather than 200 because the transition is accepted
and the run is closed to actions, but no verdict exists yet — 200 would invite a
client to read a result that has not been produced.

**Note for the verification task:** until it lands, a run that passes the gate
stays in `verifying`. That is a visible intermediate state, not a stuck one —
reset returns the workspace to ready (FR-013) — but it is the reason these two
tasks should not be separated by a release.

### T5 — FR-039's lease has no caller yet, and that is stated rather than hidden

The lease refuses a *direct human mutation of target state* while a run occupies
any of its four non-terminal states. The harness has no such surface: the human
store panel is M5's, and the Buggy Store's `/demo` API belongs to the store
(§15.5), which cannot be told about harness runs without breaking the boundary
the architecture gate enforces. `require_no_lease` is exported and tested now so
that the panel is built against a rule that already exists rather than one
invented beside it. **Queued for the operator:** confirm that FR-039's server-side
enforcement point is the M5 store panel, or name the surface it belongs to.

The four states are named rather than "any active run" because FR-039 keeps
reads, reset, and confirmation decisions available, and a lease over every state
would break the recovery paths it exists alongside.

### T5 — a real gap closed: contract selection during a run

Found while working out where the lease applies. `POST /contracts/{id}/select`
was unguarded, so a workspace could be pointed at one contract while its run was
being judged by another — the relabelling FR-012 forbids. Now refused with
`RUN_IN_PROGRESS` while a run is in flight, and available again after reset.

`RUN_IN_PROGRESS` rather than FR-039's `RUN_MUTATION_LOCKED`: this is a change
to the harness's own run configuration, not a direct human mutation of target
state, and conflating the two would tell a caller the wrong thing about what is
locked and why.

### T6 — the milestone's point, now executable

`test_the_pre_fix_journey_fails_on_independent_observation`: the store reports
the discount applied, the authoritative read says the cart total never moved,
and the run fails on the observation rather than on the tool's word. Its
counterpart runs the same contract and the same calls against an honest target
and passes — without the pair, "fails in pre_fix" could just mean the harness
fails everything.

### T6 — the core owns the verdict; this task owns the I/O

Every judgement comes from `actionwitness_core.engine`: assertions, the
trajectory check, policies, and the aggregation into a layer result. Nothing in
the service decides whether a check passed. That is what makes the verdict
replayable — the same evidence through the same pure functions gives the same
answer anywhere, which is what §24 rests on — and it is why the tests assert
what was *persisted and sealed* rather than recomputing the answer, since a
test that recomputed would agree with a broken implementation.

Evaluation reads the stored timeline back out of the database and rebuilds the
core's `RunEvent` models. FR-050 defines policy determinism over "the same
snapshots and the same recorded event stream", so evaluating against anything
the timeline does not hold would produce a verdict a replay could not reach.

### T6 — the whole verdict commits together

Snapshot, findings, per-check events, the terminal transition, and the guidance
handoff are one transaction. A run that recorded findings but never reached a
terminal state — or reached one without its findings — would be a report that
disagrees with its own evidence. Tested by failing the last write in the seal
and asserting that nothing from it survived.

### T6 — verification is synchronous, so a late action meets a sealed timeline

FR-038 says an action after the transition gets `RUN_ALREADY_VERIFYING`. Because
verification completes inside the same request, the `verifying` window is
sub-request: an action arriving *during* it does get that code (covered in the
invocation suite and by the overlap test), while one arriving *after* meets a
terminal run and gets `RUN_TIMELINE_SEALED`, which is the accurate description.
A second verify likewise gets the invalid-transition refusal rather than the
in-flight one.

**Queued for the operator:** confirm this reading. The alternative is to make
verification asynchronous so `verifying` is an observable state between
requests, which would keep FR-038's code literal at the cost of a two-call
client flow and a run that can sit unfinished.

### T6 — one event per check, and the budget reserved to pay for it

§16.1 describes `assertion_evaluated` and `policy_evaluated` as "one contract
assertion produced a result" — one event each. With 25 assertions and 10
policies allowed, verification writes up to 38 events, and a run that had spent
its whole invocation budget would push the total past FR-008's 250 at the exact
moment it tried to produce a verdict. Truncating verification events is not an
option: that is dropping evidence.

So `WorkspaceCeilings` gained a `reserved` parameter, and every invocation-start
check now holds back exactly what this run's contract will need —
`verification_started`, the final `snapshot_captured`, one event per check, and
`verification_completed`. The contract is fixed at arming (FR-012), so the
reservation is exact rather than a margin. This is a correction to 004-T8's
ceiling arithmetic, made here because this is where the overrun becomes
reachable.

### T6 — `post_call_effect_state` is stored beside the audit view

`RunEvent.post_call_effect_state` is "a namespace-rooted context fragment"
because FR-055 resolves an *assertion's* path against it. T4's `effects` mapping
is keyed by path and shaped for a person to audit. Both are stored: one is read
by a reader, the other by the classifier, and neither can substitute for the
other. The fragment is pruned to the declared paths rather than holding the
whole observation, which would duplicate the snapshot on every invocation row.

### T6 — findings persist `paths` as null when there is one path

§17.1 distinguishes a single-path finding from a multi-path one, which 004-T3's
repository already handled; this is the first task that exercises it with real
findings.

### T7 — five layers, because one verdict would conflate two different failures

The exit gate's sentence is the whole point of §23.1's layering: under the
discount fault the trajectory is what the contract expected and every call
worked, and the business outcome still fails. A single pass/fail would collapse
"the agent did the wrong thing" and "the target lied about doing the right
thing" into one answer, and a reader needs different responses to each.

`Evaluation` keeps assertions, the trajectory finding, and policies in separate
groups for the same reason. Flattened into one tuple, a failing policy would
drag `business_outcome` down with it — the conflation the layers exist to
prevent — so the flattening happens only for the run-level aggregate and for
persistence.

### T7 — `model_tool_selection` cannot be set, rather than merely not being set

§23.1 finalizes it as `not_evaluated` in a source report and forbids a Tier 2
import from updating it. `compose_outcome_report` has no parameter for it, so
this holds by construction rather than by care — the service could not set it
wrongly if it tried.

### T7 — the report is an immutable artifact, hashed over the bytes on disk

§25 refers to "the source report hash", which needs a stored referent, and
§17.1's `artifacts` table is where it goes. The document is serialized with the
core's canonical serializer and *those exact bytes* are both hashed and written:
pretty-printed JSON beside a hash taken over canonical text would produce an
artifact whose own hash a reader could not reproduce. §17.2 excludes the
top-level `content_hash` member from the hash input, so verification needs only
the file.

The file is written before the row is inserted, and the row joins the seal
transaction. File I/O must not happen inside a transaction (ADR-0003), and of
the two crash windows the safe one is a file with no row: unreachable, and
replaced by the next write. A row pointing at a missing file is one a reader
*would* reach.

FR-008's artifact ceilings are checked at the insert rather than before the
write, because the count and the insert must share a transaction. A refused
ceiling therefore leaves an unreferenced file, which is the same recoverable
case.

### T7 — `artifacts.artifact_type` is a project-allocated constant, not an enum yet

§17.1 names the column and enumerates no vocabulary. One value is not a closed
set worth registering; the eval, benchmark, and regression types arrive with the
milestones that produce them, and the enum can be introduced when there is a set
to close.

### T7 — removed the `RunMode` duplication T1 introduced

T1 defined a `RunMode` class of string constants in `run_service`, not realising
the core already had `reports.enums.RunMode` with exactly those two values. The
report has to name the mode, so two lists of the same two strings would
eventually disagree. The service now uses the core's enum.

### T8 — the classifier is mostly about *not* accusing

FR-055 uses `false_success_or_state_mismatch` **only when** the last relevant
action reported success *and* its immediate post-call observation also
mismatched. Every other route — the action failed, was cancelled, has no
immediate observation, or no declared effect overlaps the path — falls back to
`assertion_mismatch` rather than inferring causality.

A classifier that accused whenever an assertion failed after a successful-looking
call would pass the headline test and be wrong every other time, and being wrong
here means telling somebody their target lied when it did not. So six of the
nine tests are fall-back cases, one per route FR-055 lists.

The engine itself is the core's, unchanged; this task wired it in and proved
each branch through the real store.

### T8 — classification runs before the primary failure is chosen

§22 orders failures *by* classification, so picking the primary failure from
unclassified findings would choose by the wrong key — and the findings persisted
alongside the report would then disagree with the report's own headline.
Classification therefore happens inside `_evaluate`, before aggregation and
before composition.

### T8 — an adapter without effect metadata keeps its verdict and loses only blame

§12.2: "missing effect metadata disables only causal false-success
attribution." Tested by emptying the adapter's effect map and asserting two
things at once — the run still fails on the same assertion, and the finding
falls back to `assertion_mismatch` with `kind: none`. Asserting only the second
would not show that the verdict survived.

### T8 — the missing-observation test edits the timeline, not the provider

The first version blinded the observation provider mid-journey and depended on
call ordering that was hard to reason about — it passed, but I could not say
confidently that it passed for the right reason. The classifier reads the stored
timeline, so the test now removes `post_call_effect_state` from the stored event
and verifies. Same branch, no fixture whose ordering has to be argued about.

### T9 — one projection, read after the state change

`current_guidance(work, workspace_id)` is now the only place any surface asks
"whose turn is it?". FR-120 makes FastAPI the deriving authority and says the
frontend must not invent a conflicting next action; the same discipline has to
hold on the server's own side, because a handler that picked a phase itself is a
second opinion with no more authority than the frontend's.

It is called *after* whatever state change prompted it, inside the same
transaction, so the recorded handoff describes the workspace the caller is about
to see. Deriving before would have made arming record `contract_ready` — the
state the request arrived in — and the audit trail would show a handoff that
never happened.

### T9 — the bug a hardcoded phase was hiding

T3's invocation response derived `WorkspacePhase.RUNNING` unconditionally. When
the FR-008 event ceiling trips, the server moves the run to `error` in that same
request — and the response would still have told the caller to invoke another
tool, which is the server inventing a next action no state supports. Now derived
from the workspace as it stands, with a test that trips the ceiling and asserts
the caller is not told to keep going.

### T9 — append-only is not append-always

`GuidanceRecorder.transition` records only when the phase actually changes.
FR-122 is about control *moving* between actors; re-recording the same phase on
every request would bury the real handoffs under repetitions of the state the
workspace was already in. Four invocations in a row produce one `running`
transition, and a test says so.

### T9 — verification no longer clears `active_run_id` (**behaviour change**)

Found by the test asserting the verify response and the banner agree: they
didn't. Verification said "review the findings" while `GET /workspace` said "arm
a run", because clearing the pointer left the projection seeing a workspace with
a contract and no run.

§11.5's diagram settles it — a workspace stays in `Passed`/`PassedWarnings`/
`Failed` showing that run's findings, and leaves those states by **reset**,
which is where FR-013 already clears the pointer. So the pointer now names the
finished run until reset. A terminal run does not block arming another, because
the lease counts only non-terminal states.

### T9 — a latent ordering flake in template seeding, fixed

`seed_templates` stamped `created_at` from its own clock read per template, so
three rows written in one transaction got three microsecond-apart timestamps.
`ORDER BY created_at, id` then depended on how fast the loop ran — stable on one
machine, shuffled on another, and it surfaced here as a test that had been
passing by luck. They are seeded together, so they are now stamped together.

The test that exposed it also stopped selecting "the first template that is not
the canonical one": two of the three require an empty cart, so picking by
position made it depend on listing order for a reason unrelated to what it
asserts. It names the template it needs.
