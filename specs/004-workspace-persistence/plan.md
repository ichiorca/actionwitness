# 004 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

The organising fact of this milestone is that **isolation is a security
property, not a feature**. AC-11 is checked with two clients that know each
other's identifiers, and every exit-gate item is a refusal rather than a
capability. So the order below reaches the authorization boundary early and
keeps everything after it inside that boundary, rather than adding routes first
and scoping them afterwards.

The other organising fact is that ADR-0003 is already written and binding. The
Buggy Store implemented its own repository against it in 003 (`examples/
buggy_store/repository.py`, `migrations.py`), and that code is the worked
example for this one — same pragmas, same `BEGIN IMMEDIATE` unit of work owned
by its caller, same explicit ordered migration runner. What differs is that
these tables carry *evidence*, so append-only and insert-only stop being tidy
habits and become the thing an evidence chain rests on.

1. **Migrations and schema bootstrap** — the nine Tier 1 tables of §17.1,
   transcribed column by column, with `(run_id, sequence_number)` unique on
   `events` and foreign keys that make `workspace_id` the cascade root. ADR-0003
   forbids `CREATE TABLE IF NOT EXISTS` in repository code and placeholder
   migrations; the runner runs once at startup. Tier 2 tables are deliberately
   absent — adding them now would ship a schema no code fills.
2. **Repositories and the unit of work** — implementations of the core's
   `ContractRepository`, `SnapshotRepository`, `EventRepository`,
   `FindingRepository`, and `UnitOfWork` protocols. None of them opens its own
   transaction: the application service opens one and repositories join it. The
   protocols already declare no update or delete method for an insert-only
   table, and these implementations must not add one.
   Sequence allocation is `MAX(sequence_number) + 1` inside the same
   `BEGIN IMMEDIATE` that appends the event, with the unique constraint as the
   backstop rather than the mechanism (ADR-0003).
3. **The per-workspace lock manager** — keyed `asyncio.Lock` as *admission
   control*, swept when idle and unheld. ADR-0003 is explicit that correctness
   never depends on it. The store's `StoreService` already has this shape; the
   difference here is that a lock-acquisition timeout maps to
   `WORKSPACE_LOCK_TIMEOUT`, `retryable: true`, and never to a silent retry.
4. **Workspace cookie middleware** — a cryptographically random anonymous
   workspace on first access, `HttpOnly`, `SameSite=Strict`, `Secure` in
   production and only in production (FR-005 permits omitting `Secure` for
   documented local HTTP). Every stateful endpoint resolves the workspace from
   the cookie, and a supplied `workspace_id` is never an authorization
   mechanism (FR-006, §20.1). This is the stage the whole exit gate rests on.
5. **Origin validation and the error envelope** — `Origin` checked on every
   mutation, and one mapping from repository/domain failure to §15.8's envelope.
   The core raises structured `CoreError`s carrying codes and no HTTP status;
   this is where they acquire one. Invalid transitions become 409 with the code
   §16 names, using the `journeys.transitions` validation already in the core.
6. **Limits, ceilings, and cleanup** — FR-008's numbers are exact and not
   negotiable defaults: 250 events per run with one slot reserved for the
   terminal `resource_limit_exceeded`, 10 outcome runs, 10 eval cases, 20 eval
   runs, three suites of 100 trials, five pairings, 25 artifacts, 10 MiB of
   artifact bytes, two concurrent streams. FR-009's buckets are 120 requests per
   minute with a burst of 30, and 10 workspace creations per hour, keyed from
   the direct peer or explicitly trusted proxy metadata — never an arbitrary
   client-supplied forwarding header (§20.1). Every limit response is a stable
   429 or `WORKSPACE_LIMIT_EXCEEDED` **that commits nothing**.
7. **Adapter registry** — a missing optional target reports a bounded
   unavailable state. §21.1: the harness must start and run against a
   non-commerce fake with the Buggy Store package absent, which the 002-T12
   fixture already demonstrates at the core level.
8. **`/api/v1/workspace` and contract templates** — §15.1's four routes and
   §15.2's template list, read, and select. Templates are the three the Buggy
   Store integration seeded in 003-T12, exposed through the harness rather than
   re-authored here.

Cross-cutting:

- **Every test that matters uses two clients.** A single-client test proves a
  route works; AC-11 asks whether a second client can reach the first one's
  state, and that question has a different shape.
- **A rejection must leave nothing behind.** Exit-gate item 3 and FR-009 both
  say a limit response never partially commits. That is a transaction-boundary
  property, so the limit check belongs inside the unit of work rather than in a
  decorator that has already let the handler run.
- **Reset is not delete.** FR-013 and exit-gate item 4: reset cancels
  nonterminal work and unresolved confirmations while *retaining* terminal
  artifacts and the selected contract. A reset that cleared everything would be
  simpler and would destroy the evidence the product exists to keep.
- **Nothing async holds a lock or a transaction across a wait.** ADR-0003's
  "nothing is held across a wait" is the rule that makes M5's confirmation flow
  possible; violating it here would be discovered two milestones later.
- **The store's repository is the reference, not a dependency.** `examples/
  buggy_store` and this layer share ADR-0003 and nothing else — the architecture
  gate forbids the store importing anything from here, and this layer reaches
  the store only through `integrations.buggy_store`.
