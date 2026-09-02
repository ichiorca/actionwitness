# platform — persistence, config, telemetry, CLI

Everything the service stands on. Small surface, high blast radius: a change here
is felt by every route.

## Persistence — `…/actionwitness_service/persistence`

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `database.py` | 219 | `Database`, `UnitOfWork`, `transaction()`, `reading()` | `transaction()` issues `BEGIN IMMEDIATE` — SQLite's single writer for the **whole database**, not one workspace. Nothing may be held across a wait: no file I/O, no HTTP, no human confirmation. `reading()` stays off the write lock so polling cannot starve a mutation. Every connection re-applies the four ADR-0003 PRAGMAs (WAL, foreign keys, busy timeout, `synchronous = FULL`) — a test asserts all four. |
| `migrations.py` | 751 | The ordered migration runner and every schema version | Run once in the lifespan, before the first request. **Startup-time table creation is forbidden**; so are placeholder migrations. A destructive migration needs operator approval with a rollback plan. |
| `locks.py` | 137 | `WorkspaceLocks` — per-workspace admission control | Reference-counted, so it never accumulates one entry per workspace ever seen, and two different workspaces never block each other. Timeout matches the database busy timeout by design. |
| `repositories.py` | 431 | Row mapping for events, snapshots, findings, contracts, artifacts | `EventRepository.append` allocates `MAX(sequence_number)+1` inside the transaction, so a retry cannot become a duplicate event. |

**No connection pooling.** Every operation opens a fresh `aiosqlite` connection
(which spawns a thread) and re-applies the PRAGMAs. Known, deliberate, and
deferred pending a measurement — see ARCHITECTURE §14 before "optimising" it.

## Configuration — `config.py` (554 lines)

The only reader of the environment besides `app.py`'s call into it.

- **Modules are tri-state**: `enabled` / `disabled` / `misconfigured`. The third
  exists so a typo cannot look like a deliberate cut.
- **Fail-closed**: an unparseable value disables its module with an actionable
  reason; it never falls back to a permissive default. `HARNESS_PUBLIC_ORIGIN`
  drops to `None` when invalid, which `/healthz` reports.
- **A disabled module exposes no settings object.** `shopify` parses its four
  variables and still reports `disabled`, because no adapter is registered and no
  route is mounted (`_SHOPIFY_ADAPTER_SHIPPED`). Configuration is not capability.
- Store origins, variants, currencies and allowlists are **server-controlled**;
  no request body widens them.

## Telemetry — `telemetry.py` (269 lines)

One structured line per request, and it cannot leak by construction.

- `RequestLog` is a frozen model with `extra="forbid"` and **no free-text
  field** — every field is an identifier, a status, a duration, or a
  classification.
- Paths are reduced to route templates; an unmatched path logs `<unmatched>`
  rather than attacker-controlled text.
- `configure_logging()` installs a handler only when the process has none, and
  sets the level only alongside that handler — it must not lower a level an
  operator chose.
- Tracebacks go to `actionwitness.unhandled` / module loggers, never into the
  structured line and never into a response.

## CLI — `cli.py` (300 lines)

`actionwitness` entry point: serve, migrate, eval commands. Builds the same app
factory the service does, so there is no second composition root.

## Deployment surfaces (repo root)

| Path | Watch for |
|---|---|
| `Dockerfile` | Two isolated virtualenvs enforce the harness/demo boundary inside the artifact. Non-root user. Single uvicorn worker is load-bearing (ADR-0003). |
| `scripts/docker-entrypoint.sh` | The shell stays **PID 1** and forwards `SIGTERM` to both children. Do not reintroduce `exec` after a `trap` — that destroys the trap and orphans the store. |
| `render.yaml` | Free tier by documented operator decision; **no disk**, so `/data` is ephemeral and evidence does not survive a redeploy. |
| `docs/release-checklist.md` | The operator-attested gate before a release. |
