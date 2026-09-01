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

## Deviations and decisions worth an operator's eye

Every judgment call that extends or departs from the spec, anchored to the
section it answers to. Same convention as 002 and 003.

### T1 — `artifacts.byte_size` is project-allocated (§17.1, FR-008)

§17.1's `artifacts` table lists no size column, but FR-008 caps "10 MiB of
persisted artifact bytes" per workspace. Enforcing that by stat-ing files inside
a transaction would be both slow and racy — the file can change between the stat
and the commit — so the size is recorded on the row that the cap sums over.
Nothing else reads it.

### T1 — `snapshots` carries `namespace`, `provenance`, and `schema_version` (§17.1, §9.3)

§17.1's `snapshots` table lists `provider` but none of these three. The core's
`SnapshotRepository.get` returns an `Observation`, and an `Observation` is not
reconstructible without them: the namespace is what an assertion path resolves
through (§9.3), so a snapshot restored without it would answer a *different*
contract than the one it was captured for — silently, and only on the replay
path. They sit beside the payload rather than inside it, for the same reason
§9.3 keeps `state_version` out: a key inside the payload would be assertable
through `target.namespace` and would change the content hash.

### T1 — Tier 2 owner columns on `artifacts` carry no foreign key (§17.1)

`evaluation_case_id`, `evaluation_run_id`, `benchmark_suite_id`, and
`shopify_pairing_id` are declared because §17.1 declares them, but the tables
they would reference do not exist until M6/M7 and a foreign key to a missing
table is an error. The columns keep §17.1's shape; the keys arrive with the
tables.

### T2 — `Database(busy_timeout_ms=...)` narrows the wait for tests only

ADR-0003 fixes the busy timeout at 5,000 ms, and the default is exactly that.
The keyword exists so a contention test can observe a refusal in milliseconds
rather than waiting out five real seconds thirteen times. It narrows the wait,
never widens the contract, and
`test_every_connection_applies_the_four_adr_0003_pragmas` asserts the production
default on a connection built the production way.

### T2 — four project-allocated error codes (§15.8)

§15.8 fixes the envelope but not a code for each of these situations. Each is
recorded in `tests/unit/test_registry.py`, which fails if the set changes
without a note:

| Code | HTTP | Why the spec's vocabulary was insufficient |
|---|---|---|
| `ORIGIN_NOT_ALLOWED` | 403 | FR-005 requires refusing a mutation from an unconfigured `Origin`; §15.8 names no code for it. |
| `RESOURCE_NOT_FOUND` | 404 | FR-006's cross-workspace refusal. **Deliberately 404, not 403** — a 403 would confirm that the identifier names something real, which is the fact FR-006 is protecting. |
| `RATE_LIMIT_EXCEEDED` | 429 | FR-009 fixes the buckets and the status but names no code. The only project code with `retryable: true` besides `WORKSPACE_LOCK_TIMEOUT`. |
| `HARNESS_ERROR` | 500 | The terminal mapping for an unmapped internal fault, so a failure surfaces as a stable envelope rather than as a leaked message or traceback (§20). |

`retryable` is read from the registry rather than passed by the call site, so a
handler cannot advertise a rejected intent as safe to repeat.

### T2 — a lock timeout is never retried on the caller's behalf (ADR-0003, constitution §5)

The busy timeout has already elapsed by the time `WORKSPACE_LOCK_TIMEOUT` is
raised. Retrying inside the server would repeat a mutation whose outcome is
ambiguous, which constitution §5 forbids; the caller retries under its original
idempotency key or does not retry at all.

### T3 — `SnapshotIntegrityError` rather than a `None` or a bare payload

A snapshot row whose payload no longer hashes to its stored `content_hash` has
been altered outside the append-only path. Constitution §5 requires an explicit
non-pass rather than a degradation to success — and returning `None` would be a
degradation too, because "absent" and "tampered" are different facts and only
one of them is recoverable by re-observing.

### T3 — a rolled-back append reuses its sequence number

`MAX(sequence_number) + 1` is computed inside the transaction, so a rejected
append releases the number it reserved and the next append takes it. The
timeline therefore has no gaps. A monotonic counter that leaked numbers on
rollback would leave holes a reader could not distinguish from deleted
evidence (§16.1).

### T3 — `list_after` applies no page ceiling

The route above it owns §15.3's page size. A repository that silently returned
fewer rows than asked would make a polling client believe it had reached the end
of the timeline. FR-008 caps a run at 250 events, so the page is bounded by the
domain rather than by a number this layer invents.

### Dependency — `aiosqlite` pinned to 0.22.1, service package only

The constitution keeps `actionwitness_core` free of aiosqlite, and the
architecture lane fails if the core grows an import of it. Chosen over
hand-rolling a thread-pool wrapper around stdlib `sqlite3` because ADR-0003's
busy-timeout and `BEGIN IMMEDIATE` semantics require the driver to preserve
per-connection transaction state across awaits, which a naive executor wrapper
does not. Small, single-module, no transitive dependencies, actively maintained.
