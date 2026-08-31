# ADR-0003 — SQLite transaction and lock model

- **Status:** Accepted
- **Date:** 2026-08-31
- **Implementing change:** 001-T3 (record); repositories and lock manager in M3

## Context

SQLite is the durable server-side source of truth for the first release (spec §17;
locked decision 11). It is sufficient only because the deployment is one service
instance running one Uvicorn worker — a single writer. That premise is doing a lot
of load-bearing work, and every rule below exists to keep it from being violated
accidentally.

Three properties must hold at once, and they pull against each other:

1. **Workspace mutations are serialized** (FR-007). Mutations in *different*
   workspaces must still run concurrently, so a single global write lock is both
   correct and unacceptably coarse.
2. **No transaction or lock is held across browser I/O or a human decision**
   (spec §17). A confirmation wait is unbounded in wall-clock terms; holding
   SQLite's write lock across one would deadlock the single worker against itself.
3. **Evidence is append-only and deterministically sequenced.** `events` carries a
   unique `(run_id, sequence_number)`, so sequence allocation is itself a
   contended write that must not race.

The constitution adds a fourth: schema changes use explicit, tested migrations,
and startup-time table creation is forbidden. This must be reconciled with spec
§29.1, which says seed data initializes on startup if absent.

This record is required before M3 repository work (BUILD_ORDER §6).

## Decision

### Connection configuration

Every harness connection is opened through `aiosqlite` and configured identically,
at open time, before any statement runs:

| Setting | Value | Why |
|---|---|---|
| `PRAGMA journal_mode` | `WAL` | Readers (polling, report reads) never block the writer |
| `PRAGMA foreign_keys` | `ON` | Off by default in SQLite; workspace isolation depends on it |
| `PRAGMA busy_timeout` | `5000` (ms) | Spec §17; bounds lock contention instead of failing instantly |
| `PRAGMA synchronous` | `FULL` | Evidence durability outranks write throughput at this scale |

`journal_mode` is persistent per database file; the rest are per connection and
must be re-applied on every connect. A connection that fails to apply them is not
returned to the caller.

### Transactions

Every workspace mutation runs inside `BEGIN IMMEDIATE`. `aiosqlite` inherits
stdlib autocommit behavior, so the deferred default would take the write lock only
at the first write — turning a read-then-write sequence into a lost update under
concurrency. `BEGIN IMMEDIATE` takes it up front and converts the race into a
bounded wait governed by `busy_timeout`.

A unit of work is one transaction with one owner: the application service opens
it, repositories join it, and no repository opens its own. Read-only paths use a
deferred transaction and never escalate.

### Sequence allocation

`sequence_number` is allocated inside the same `BEGIN IMMEDIATE` transaction that
appends the event, as `MAX(sequence_number) + 1` scoped to the run. The unique
constraint on `(run_id, sequence_number)` is the correctness backstop, not the
allocation mechanism: if it ever fires, the transaction was wrong, and the error
surfaces rather than being retried into a duplicate.

### Two-tier locking

The keyed per-workspace `asyncio.Lock` is an **admission control**, and the
database transaction is the **serialization boundary**. Correctness never depends
on the in-process lock — it depends on `BEGIN IMMEDIATE`. The lock exists so that
concurrent requests for one workspace queue in the event loop instead of piling
onto SQLite's writer and burning the busy timeout.

Locks are created on demand, keyed by workspace ID, and swept when idle and
unheld, so the map cannot grow without bound across the workspace lifetime.

### Nothing is held across a wait

No transaction and no workspace lock may span browser I/O, SSE delivery, or a
human confirmation wait. Protected checkout is therefore two transactions
(spec §17, FR-066):

1. **Create.** A short transaction writes the pending confirmation bound to
   workspace, run, invocation, state, consequence, and expiry. It commits and
   releases the lock. The invoking page's tool promise stays pending with no
   server-side lock behind it.
2. **Consume.** After the human decides, a *new* transaction re-acquires the lock,
   revalidates the confirmation against current state, and consumes it exactly
   once in the same transaction as the order creation.

Revalidation in step 2 is mandatory precisely because nothing was held between the
two: state may have moved, and a stale or replayed approval must fail closed.

### Lock timeout is an error, never a retry

A `busy_timeout` expiry or lock-acquisition timeout maps to a stable, `retryable:
true` API error (`WORKSPACE_LOCK_TIMEOUT`; see the error registry) and never to an
unhandled exception. The server does not silently retry the mutation: a retry is
the client's decision under its own idempotency key, because an ambiguous
transport outcome must not be automatically retried (constitution §5).

### Migrations

Schema is created by an explicit, ordered, tested migration runner invoked once at
startup. No `CREATE TABLE IF NOT EXISTS` in repository code, and no placeholder
migration files.

This is consistent with spec §29.1's "seed database initializes on startup if
absent": *running the migration runner* at startup is allowed and expected;
*repositories creating their own tables on first use* is what the constitution
forbids. Seeding reference data is a separate, idempotent step that runs after
migrations complete.

### No `sqlite3` on the event loop

Standard-library `sqlite3` calls never execute on the ASGI event loop. Harness
persistence is `aiosqlite` throughout; any unavoidable synchronous work runs in a
bounded threadpool.

### Namespacing

Harness and demo tables may share one file in the composed image but use separate
repository implementations and table-name prefixes. `actionwitness_core` knows only
repository protocols and never sees a connection, a cursor, or a table name.

## Consequences

### Positive

- The single-writer premise is enforced by configuration rather than assumed, so
  violating it produces a bounded, typed error instead of silent corruption.
- Per-workspace keying preserves the isolation guarantee AC-11 tests: two clients
  cannot contend with, or observe, each other.
- The two-transaction confirmation flow makes the deadlock impossible by
  construction rather than by reviewer vigilance, and it forces the revalidation
  that stale-approval safety depends on anyway.
- WAL keeps paged polling — the Tier 1 timeline transport — off the writer's path,
  which matters because polling is the most frequent read in the product.
- Repository protocols plus explicit migrations keep the documented PostgreSQL
  migration path open; nothing above depends on a SQLite-only behavior.

### Negative

- **The model is only as good as the deployment.** A second worker or instance
  silently breaks the in-process lock tier — the database transaction still
  protects correctness, but contention and timeout behavior change sharply.
  **Follow-up, owed in M8:** the deployment must assert one worker and one
  instance, and that assertion needs to be verifiable at startup rather than
  documented in a runbook.
- `synchronous = FULL` costs an fsync per commit. Accepted for evidence integrity
  at demo scale; it is the first knob to revisit if write latency becomes a
  problem, and revisiting it needs a superseding record.
- `MAX(sequence_number) + 1` is a read-then-write inside the transaction. It is
  correct under `BEGIN IMMEDIATE` and unacceptable without it, which makes the
  transaction discipline load-bearing in a way that is easy to erode later.
- Two-transaction confirmation means a crash between create and consume leaves a
  pending confirmation. It must expire rather than linger, so expiry is required
  behavior, not a nicety.
- Per-connection pragmas are a repeated correctness step. Any code path that opens
  a connection outside the shared helper silently loses foreign keys. **Follow-up,
  owed in M3:** a test that opens a connection through the production helper and
  asserts all four settings.

## Rejected alternatives

### Deferred transactions with application-level retry

Rejected: deferred `BEGIN` acquires the write lock at first write, so a
read-validate-write mutation can be invalidated between its read and its write.
The retry loop that repairs this is exactly the automatic retry of an ambiguous
outcome the constitution forbids, and it would re-run mutation intent that may
already have partially applied.

### A single global write lock

Rejected: it is trivially correct and violates FR-007's requirement that different
workspaces proceed concurrently. It would also make one slow workspace a
denial-of-service against every other, which matters on a shared public demo.

### Holding the workspace lock across the confirmation wait

Rejected: a human confirmation has no bounded duration. On a single worker this
deadlocks the process against itself, and spec §17 forbids it outright. The cost
of not holding it — mandatory revalidation on consume — is a safety improvement
rather than a workaround.

### `CREATE TABLE IF NOT EXISTS` at repository startup

Rejected by the constitution, and for a concrete reason: it makes the schema an
emergent property of which code paths happened to run, so no two environments are
provably identical and no destructive change is reviewable.

### PostgreSQL from the start

Rejected for this release: it adds a service dependency, a container, and
provisioning to a single-instance demo that must run from a clean checkout with no
credentials. The decision is deliberately reversible — repository protocols and
explicit migrations are what keep it so — and locked decision 11 already names
horizontal scaling as the trigger for a superseding record.

## Notes

`WORKSPACE_LOCK_TIMEOUT` is a project-allocated code rather than one the
specification names; it is marked as such in the error registry so its provenance
stays visible.

The most likely superseding trigger is horizontal scaling. Anything that requires
more than one writer invalidates this record wholesale rather than in part.
