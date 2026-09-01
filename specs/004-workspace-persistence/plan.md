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

### T4 — the lock key set is reference-counted, not swept when unlocked

The obvious cleanup — delete a key once its lock reads unlocked — is wrong.
`asyncio.Lock.release()` wakes the next waiter's future but leaves `locked()`
false until that waiter actually resumes, so a caller arriving in that window
finds no key, builds a *second* lock for the same workspace, and is admitted
alongside the waiter about to acquire the first. Measured: with staggered
arrivals the sweeping version admits three concurrent holders where the
reference-counted version admits one. The key is therefore dropped when its
holder-and-waiter count reaches zero, which is the only moment at which no
coroutine can still hold a reference to the lock object.

This is admission control, not correctness (ADR-0003). Deleting this module
entirely would make the service slower and noisier with
`WORKSPACE_LOCK_TIMEOUT`, never wrong — the transaction is the serialization
boundary. `release_idle()` is kept for a periodic cleanup pass and returns a
count so a test can show the map is bounded rather than merely believed to be.

### T5 — a presented workspace identifier is never adopted (FR-005, FR-006)

A cookie naming a workspace that does not exist mints a *fresh* one and discards
the presented value. Adopting it would let a client choose its own workspace
identifier, and a client that can choose its own can choose somebody else's —
which is precisely what FR-006 exists to prevent. It also keeps FR-009's cleanup
from stranding a returning visitor: a stale cookie starts a new workspace rather
than failing.

### T5 — `HARNESS_ENV`, `DeploymentEnvironment`, `HarnessSettings` (project-allocated, FR-005)

FR-005 makes `Secure` conditional — "documented local HTTP development may omit
only the `Secure` attribute" — so something must say which case applies. A
two-value enum rather than a boolean, because `HARNESS_SECURE_COOKIES=false` in
production is a one-typo downgrade whereas naming the environment is auditable.
**The default is `production`**, and an unrecognised value also resolves to
`production`: an operator who forgets gets a stricter cookie and a broken local
session, which is the failure that gets noticed. Unlike every other resolver in
`config.py` this one cannot report `misconfigured`, because there is no module
to switch off — there is no service without it.

`HARNESS_DATABASE_PATH` is project-allocated alongside it; §29.1 documents the
startup commands but not where the file lives.

### T5 — cookie name, width, and lifetime are project-allocated (§20.1)

§20.1 requires "a cryptographically random anonymous workspace cookie" and fixes
its attributes but neither its name, its width, nor its lifetime.
`actionwitness_workspace`, 256 bits via `secrets.token_urlsafe`, seven days. The
`__Host-` prefix is deliberately **not** used: it mandates `Secure`, which FR-005
explicitly permits omitting for documented local HTTP, so the prefix would make
the local path impossible rather than merely unattested.

### T5 — health checks and static assets get no workspace

FR-009 excludes them from rate limiting; they are excluded from workspace
creation for the same reason. A liveness probe that minted a workspace every few
seconds would fill the table with rows no human ever visits, and FR-009's
staleness scan would then be cleaning up after the monitoring system.

### T5 — `create_app` takes an injected environment, database path, and clock

So a test constructs the real application rather than a lookalike, and so
`last_seen_at` advancing is a fact about the application's clock seam
(constitution §1) rather than about how fast the test machine is. Windows wall
clock resolution is coarse enough that two adjacent requests can otherwise share
a timestamp. Passing none of them reads `os.environ` once, in the composition
root and nowhere else.

### T6 — the workspace goes in the `WHERE` clause, not in a post-fetch comparison

FR-006 says "even when its identifier is known", and the fetch-then-compare
pattern satisfies that until one handler forgets the comparison — a forgetting
that is invisible in review, because the code that fetches looks complete. So
`WorkspaceScope` puts the workspace in the query: another workspace's row does
not resolve, and there is no fetched row for a handler to mishandle. The table
name is checked against a frozen set before interpolation, because SQLite
cannot bind an identifier and a caller-supplied table name would be an
injection point.

### T6 — the refusal is 404, and byte-identical to a genuinely missing resource

A 403 would confirm that the identifier names something real, which is the one
fact FR-006 protects. "Someone else's" and "never existed" therefore share a
status *and a message*: a wording difference is a working oracle, and that is
how this kind of leak usually survives review. A test compares the two response
bodies rather than only their statuses.

### T6 — the §15.8 exception handler landed here rather than in T7

T7 owns `Origin` validation and the `CoreError` → HTTP mapping. The handler
that turns a raised `ApiError` into §15.8's envelope arrived one task early
because without it a refusal cannot reach a client at all, and T6's tests are
required to be two-client HTTP tests. T7 extends the same handler.

### T6 — the four resource kinds are exercised through a test-mounted probe router

§15's routes for runs, confirmations, and artifacts belong to M4 and M5.
Inventing them here to have something to authorize would canonise route shapes
a later milestone owns. Instead the tests mount a probe router on the **real**
application: the cookie middleware, `WorkspaceDependency`, `WorkspaceScope`, the
`ApiError` handler, and the envelope are all production code, and only the leaf
handler belongs to the test. When the real routes arrive they call the same
scope, and T13's exit gate re-checks isolation across whatever is mounted then.

### T6 — no request-scoped `UnitOfWork` dependency

A transaction opened as a FastAPI dependency lives for the whole request,
including any I/O the handler performs — exactly the "held across a wait" that
ADR-0003 forbids. Handlers open their own around the work and nothing else.

### T7 — a missing `Origin` is allowed; a mismatching one never is

§20.1 requires validating `Origin` on mutating requests but does not say what to
do when there is none. Browsers send it on every mutating request, same-origin
included, so absence means the request did not come from a page — a CLI, a test,
or an agent, none of which carry the ambient cookie authority a cross-site page
does. Refusing them would break the documented `actionwitness` CLI without
closing anything: an attacker who can set a header can also omit one, so
trusting absence *less* than presence only inconveniences honest clients.
`SameSite=Strict` remains the primary control; this is the second lock.

Comparison is equality after normalization — never prefix, suffix, or host
containment. `https://harness.test.evil.example` and `https://evilharness.test`
are both in the test table, because those are the two shapes a "close enough"
rule accepts.

### T7 — `Origin` is checked before the workspace cookie is issued

Starlette applies middleware in reverse registration order, so the origin check
is registered last in order to run first. Without that ordering a hostile page
could fill the workspaces table by being rejected repeatedly, and a test asserts
that a refused origin leaves no row behind.

The refusal is *built* inside the middleware rather than raised: an exception
thrown from middleware travels outside the application's exception handlers and
would reach the client as a bare 500 with no envelope.

### T7 — `HARNESS_PUBLIC_ORIGIN` is optional for the harness, required for Shopify

The same variable already exists for the Shopify module, which cannot run
without it. Here it is optional: when absent — the documented local case — the
request's own origin is compared instead, which is sound because §20.1 requires
the frontend and API to be served same-origin. An unparseable value is dropped
rather than accepted loosely.

### T7 — three exception handlers, in widening order

`ApiError` → its registry status and envelope. `CoreError` → `error_from_core`,
where `INVALID_STATE_TRANSITION` becomes §16's 409 and an unmapped code becomes
a 500 carrying none of its own text. `Exception` → a fixed `HARNESS_ERROR`
message with no traceback, class name, or exception text, because an unhandled
failure is exactly the case where the message is most likely to name a table, a
path, or a value. Tests assert the absence of specific leaked substrings rather
than merely the status.

### T8 — the event ceiling commits; every other ceiling rolls back

This is the one place where the two rules in the ground rules point in opposite
directions, and getting it backwards was a real bug caught by a test.

A creation past a cap **must commit nothing** (FR-009: limits "shall never
partially commit a mutation"), so `guard_new_run` and `guard_new_artifact`
raise inside the caller's unit of work and the transaction unwinds.

A run that hits the event ceiling **must commit more**. FR-008 requires the
server to "atomically move the active run to `error`, append that boundary
event ... and preserve existing evidence" — and raising inside the unit of work
rolled back the very evidence the requirement exists to create. So
`trip_if_event_budget_exhausted` *returns* its refusal instead of raising it:
the caller commits the two boundary writes, then raises what it was handed.
Both writes still share one transaction, so there is no state where the run is
stopped but nobody recorded the stop — a test aborts mid-unit-of-work and shows
neither survives.

### T8 — the ceiling is 249 + 1, not 250

"250 persisted events, with one slot reserved for the terminal
`resource_limit_exceeded` boundary event" is a reserved slot, not an overflow. A
run that spent all 250 on ordinary events would have nowhere to record *why* it
stopped, which is the one event that makes the stop legible.

### T8 — Tier 2 ceilings are declared but not yet enforced

The constants for eval cases, eval runs, benchmark suites, trials per suite, and
Shopify pairings are transcribed here so FR-008's numbers live in one place, and
a test asserts each value. They are absent from the enforcement map because
their tables arrive with M6/M7 and counting a table that does not exist is an
error rather than a ceiling. `CONCURRENT_EVENT_STREAMS` is likewise declared;
the stream endpoint is M4's.

### T8 — the artifact byte cap counts the artifact about to be written

A cap that admitted the write which crossed it would be off by one artifact, and
that artifact could itself be 10 MiB. `stored + byte_size > ceiling` rather than
`stored >= ceiling`. Also 10 **MiB**, not 10 MB — the two differ by 485,760
bytes, and a test asserts the mebibyte reading.

### T8 — the boundary event's actor is `harness`

Not `agent`. The event is the server speaking about the run; attributing it to
the agent would put a sentence in the mouth of the thing under test.

### T8 — tripping the ceiling on another workspace's run yields `RESOURCE_NOT_FOUND`

The `UPDATE` is workspace-scoped, so a stranger's attempt changes nothing. The
refusal is the same 404 as any other cross-workspace access rather than an
`EVENT_LIMIT_EXCEEDED`, which would confirm both that the run exists and how
much of its budget it has spent.

### T9 — "120 per minute with a burst of 30" fixes two numbers, not one

The refill rate is 2 tokens/second and the capacity is 30. Both plausible
misreadings pass a naive test: capacity 120 lets a client fire 120 requests
instantly, which is not a burst of 30; refill 30/minute throttles a compliant
client to a quarter of its allowance. Separate tests rule out each.

### T9 — a forwarding header is believed only from a configured trusted proxy

§20.1: "never trust an arbitrary client-supplied forwarding header." Believing
it unconditionally makes the limit opt-out — one header per request and every
attacker is a fresh client. `HARNESS_TRUSTED_PROXIES` (project-allocated) lists
the peers whose header may be read, and it is empty by default, so an
unconfigured deployment ignores the header entirely. When it is honoured, only
the **last** hop is used: earlier entries were appended upstream of the trusted
proxy, including by the client.

A request with no peer address — a Unix-socket deployment, and the in-process
test transport — keys as one shared `"unknown"` client. That limits more than
strictly necessary, which is the safe direction for a public service.

### T9 — the workspace-creation bucket is spent only when a workspace is created

FR-009's ten-per-hour limit is on *new workspaces*, not page loads. Charging a
returning visitor would let one user exhaust an hour's allowance in a minute by
refreshing. The middleware therefore spends it only when the request carries no
workspace cookie.

### T9 — the 429 commits nothing because there is nothing to commit

The limiter runs before any handler (registered last, so it runs first), which
makes FR-009's "never partially commit a mutation" true by construction rather
than by a rollback somebody has to remember. The test proves it against a route
that would otherwise write a row.

### T9 — `Retry-After` is never zero

A client told to retry immediately fails immediately, which turns one refused
client into a busy loop.

### T9 — cleanup is cooperative, not cancelled

Cancelling the hourly sweeper mid-sweep interrupts an open transaction and
leaves the SQLite driver's worker thread unwound, which surfaces later as an
unhandled thread exception with no connection to the code that caused it —
observed once as a flaky test before the fix. The loop now waits on a stop
event, so shutdown lands between sweeps; cancellation remains only as a
five-second backstop.

### T9 — expiry is the documented exception to append-only

FR-009: "Artifact immutability applies during retention and does not prevent
documented workspace expiry or an explicit purge." Everything else in this
project says evidence is never deleted, so the deletion here is deliberately
narrow — a whole workspace aged out by its own inactivity, via the cascade root
— rather than a general row-removal capability a later handler would reach for.

Files are unlinked *after* the rows commit. If the process dies between the
two, the files are orphaned, which is recoverable; the reverse order loses files
that live rows still claim, which is not. A stored path that resolves outside
the artifact root is refused: a persisted record is untrusted input
(constitution §5), and a row carrying `../..` must not turn cleanup into
arbitrary deletion.

### T9 — eval workspaces are excluded from the 24-hour clock

FR-009 gives them a different rule — mutable state goes "immediately after
report persistence". Sharing the interactive clock would either delete an eval
mid-flight or keep one alive long past the report it existed to produce.
`purge_eval_workspace_state` implements that half and is kind-scoped, so a
mistaken identifier is inert rather than destructive; M6 calls it.

### T9 — `HARNESS_ARTIFACT_ROOT` (project-allocated)

§29.1 documents the startup commands but not where artifact files live, and
cleanup cannot remove files without knowing.

### T10 — the integration is imported inside the guard, not at module scope

§21.1 requires the harness to start with the Buggy Store package **absent from
the environment**, not merely switched off — and a configuration flag cannot
prove that. A service could honour `BUGGY_STORE_ENABLED=false` while still
importing the package at startup and dying when it is missing. So the import
happens inside `_register`'s `try`, and `ImportError` is an expected outcome
producing a `disabled` slot with "not installed" as its reason. The test that
matters makes `import integrations.*` genuinely fail and asserts the
application still completes its lifespan and serves.

Each registration is wrapped individually rather than the loop as a whole, so a
failure in one integration cannot skip the rest.

### T10 — `disabled` and `misconfigured` stay distinct

`ModuleStatus` already draws that line and the registry preserves it. An
operator who mistyped a base URL needs to see a mistake, not an absence;
reporting both as "not available" turns a typo into a mystery. The capability
report lists unavailable targets too — a bar showing only what works would make
a misconfiguration look like a feature that was never built.

A broken integration's reason names the exception **type** and not its message.
An exception's text is where a path or a credential leaks (§20), and a test
seeds a URL with a password in it to prove neither reaches the reason.

### T10 — `TARGET_UNAVAILABLE`, not a new code

FR-021's existing code already means exactly this, so nothing is
project-allocated here. It is an `ApiError` subclass so an absent target reaches
a client as §15.8's envelope rather than as a 500 whose body is a stack trace.

### T10 — one lifespan-owned HTTP client, injected (ADR-0001)

A client per request loses connection reuse; a module-level one outlives the
loop it was created on. A test that supplies its own keeps ownership of it —
closing somebody else's client is not the lifespan's business.

### T11 — reset's retention half has its own tests

FR-013 has two halves and only one is obvious. "Cancel nonterminal runs ... and
unresolved confirmations" is the half anyone would implement; "preserve
completed artifacts and the selected contract so the workspace returns to
`ContractReady`" is the half a plausible implementation drops, because clearing
everything is simpler. So retention is tested directly: a terminal run and its
artifact survive a reset, and the selected contract survives it too — the
workspace cannot return to `ContractReady` if reset removed what made it ready.

Reset also leaves a **decided** confirmation alone. A cancelled-because-pending
confirmation is housekeeping; rewriting an approved or denied one would revoke
consent a person actually gave (constitution §5).

### T11 — the cancellation event is appended before the status changes

The append reads the run's current event count. A run already relabelled
`cancelled` would be recording the cancellation of something that, by its own
status, was never running.

### T11 — no route takes a workspace identifier

There is no path parameter, query parameter, or body field naming a workspace on
any of §15.1's four routes. That is the point: the cookie is the only input, so
a second client has nothing to aim at. The AC-11 test for reset shows the
intruder resetting *its own* empty workspace as hard as it can — `purge_completed`
included — while the first client's running run is untouched.

### T11 — §15.1's table contains backslash typos

The published table reads `\workspace\reset` and `\runs\{run_id}\events`. These
are markdown-escaping artifacts, not paths; the routes are mounted with forward
slashes under `/api/v1`. Flagged rather than reproduced.

### T11 — `next_action` is a small honest projection, not guidance

§15.1 asks for "authoritative guidance, and one safe `next_action`". §18's
guidance system — the `guidance_events` stream with its copy versions, actors,
and recovery actions — is M4's. Inventing a partial version here would create a
second source of truth for what the user should do next, so `next_action` is
derived from the workspace's own columns only and no `guidance` field is
fabricated. **Operator decision worth flagging:** the guidance half of
`GET /workspace` is deferred to M4 rather than stubbed.

### T11 — the failure-profile token is not validated against a list

FR-011 names the Tier 1 profiles, but `TargetDescriptor` has no
`supported_fault_profiles` field to validate against — it carries
`supported_scenario_modes` only. Adding one is a change to a public core
protocol, which the escalation contract reserves for the operator. So the route
validates the token's *shape* and stores it opaquely, and the adapter remains
the authority when the run is armed (M4). **Queued for an operator decision:**
either add `supported_fault_profiles` to the descriptor, or accept that an
unsupported profile is caught at arming rather than at selection.

Scenario mode, by contrast, *is* validated against the descriptor, because
§9.1 gives it a field to validate against.

### T11 — selection is refused while a run is in flight

FR-011 says the option "must be chosen before arming" and FR-012 says an armed
run's configuration is immutable and "completed evidence is never relabeled".
A terminal run does not block, though — a finished run must not lock a
workspace forever.

### T12 — `source_template_id` is a column, never a key in the document

Caught by a failing test rather than by review. Seeding first hashed the
template's document and *then* wrote provenance into it, so the stored document
no longer matched its own hash and every read failed its integrity check. The
document is what the content hash covers, and the hash is the contract's
identity — writing anything into it makes the stored hash describe something
nobody authored. `ContractRepository.add` therefore takes `source_template_id`
as a keyword-only column argument, which still satisfies the core's
`add(record)` signature.

The listing projection reads that column, so it is a separate repository method
rather than a reconstruction of full `ContractRecord`s: a summary is not a
record, and rebuilding records only to throw their documents away would parse
every template on every list.

### T12 — template identity includes the content hash

`tpl_<template_id>_<hash prefix>`. Seeding is idempotent across restarts, and a
template whose text changes between releases becomes a **new row** rather than
silently overwriting the version an existing run was armed against (FR-012:
"completed evidence is never relabeled"). The old row stays readable for the
runs that used it.

### T12 — selection takes no request body

FR-024: "no endpoint may combine a contract with a different target." The target
comes from the contract's own immutable `target_id`, so the forbidden
combination is not expressible rather than merely rejected — there is no
parameter to combine. When the named target is unavailable, **nothing is
written**: a workspace left holding a contract whose target cannot run would be
exactly the state the requirement forbids.

### T12 — a tampered contract is `HARNESS_ERROR`, not a new code

There is no evidence-integrity code in the registry, and rather than allocate a
fifth project-allocated one, a stored contract that no longer hashes to its
recorded value returns 500. It is a fault in the deployment, not something the
caller can fix by changing the request, and constitution §5 requires it to be an
explicit non-pass rather than serving the document anyway. **Queued for an
operator decision:** whether M4's evidence-chain verification should introduce a
distinct code rather than reusing `HARNESS_ERROR`.

### T12 — only three of §15.2's six endpoints are implemented

`POST /contracts` (instantiate from a template), `/from-candidates`, and
`/published` belong to M4 and M8 and are absent rather than stubbed, per T12's
scope: list templates, read one contract, select the active contract.

### T12 — templates are seeded from the integration, and only when it is present

The three Tier 1 contracts arrive as data from `integrations.buggy_store`, which
understands what `target.cart.total` means; re-authoring them in a
target-neutral service would put commerce vocabulary where the constitution
forbids it, and copying them would give the project two sources of truth for
what a contract asserts. Seeding is skipped entirely when the integration is not
installed — §21.1 requires the harness to run without it, and a startup that
insisted on seeding its templates would make that impossible.
