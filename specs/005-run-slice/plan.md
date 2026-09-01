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
