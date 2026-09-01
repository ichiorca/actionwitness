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
