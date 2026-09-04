# platform — persistence, config, telemetry, CLI

Everything the service stands on. Small surface, high blast radius: a change here
is felt by every route.

## Persistence — `…/actionwitness_service/persistence`

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `database.py` | 219 | `Database`, `UnitOfWork`, `transaction()`, `reading()` | `transaction()` issues `BEGIN IMMEDIATE` — SQLite's single writer for the **whole database**, not one workspace. Nothing may be held across a wait: no file I/O, no HTTP, no human confirmation. `reading()` stays off the write lock so polling cannot starve a mutation. Every connection re-applies the four ADR-0003 PRAGMAs (WAL, foreign keys, busy timeout, `synchronous = FULL`) — a test asserts all four. |
| `migrations.py` | 946 | The ordered migration runner and every schema version | Run once in the lifespan, before the first request. **Startup-time table creation is forbidden**; so are placeholder migrations. A destructive migration needs operator approval with a rollback plan. |
| `locks.py` | 137 | `WorkspaceLocks` — per-workspace admission control | Reference-counted, so it never accumulates one entry per workspace ever seen, and two different workspaces never block each other. Timeout matches the database busy timeout by design. |
| `repositories.py` | 431 | Row mapping for events, snapshots, findings, contracts, artifacts | `EventRepository.append` allocates `MAX(sequence_number)+1` inside the transaction, so a retry cannot become a duplicate event. |

**No connection pooling.** Every operation opens a fresh `aiosqlite` connection
(which spawns a thread) and re-applies the PRAGMAs. Known, deliberate, and
deferred pending a measurement — see ARCHITECTURE §14 before "optimising" it.

## Configuration — `config.py` (581 lines)

The only reader of the environment besides `app.py`'s call into it.

- **Modules are tri-state**: `enabled` / `disabled` / `misconfigured`. The third
  exists so a typo cannot look like a deliberate cut.
- **Fail-closed**: an unparseable value disables its module with an actionable
  reason; it never falls back to a permissive default. `HARNESS_PUBLIC_ORIGIN`
  drops to `None` when invalid, which `/healthz` reports.
- **A disabled module exposes no settings object.** `shopify` now reports
  `enabled` when its four variables parse: `_SHOPIFY_ADAPTER_SHIPPED` is `True`,
  the registry registers the adapter, and the routes mount. Flip the flag back if
  the registration is ever removed — configuration is not capability.
- Store origins, variants, currencies and allowlists are **server-controlled**;
  no request body widens them.

## Environment files — `env_file.py`

Turns an environment file on disk into the mapping `config.py` is handed.
Composed at the root, so `config.py` still takes an injected mapping and never
learns that files exist.

- **The process environment wins.** A file is a default written earlier; an
  explicit `FOO=bar uv run ...` is a decision made now.
- **Only on the un-injected path.** `create_app(environ=...)` gets exactly that
  mapping — a developer's local file leaking into the suite would make tests
  pass differently per machine.
- **Names are logged; values never are.** This file holds credentials, so
  unparseable lines are counted rather than quoted.
- **Nothing here is fatal.** Missing, oversized, binary, or malformed all resolve
  to "no variables from the file", matching `config.py`'s rule that construction
  never raises.
- Not a shell parser: no interpolation, no substitution, no escape decoding. A
  configuration file must not be a program.

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
| `Dockerfile` | Two isolated virtualenvs enforce the harness/demo boundary inside the artifact. The harness venv installs core, service, `buggy_store`, `google_evals`, **and `shopify`** — the benchmark routes lazy-import `google_evals` on every request and the audit routes lazy-import `shopify` when enabled, so omitting a `--package` line fails only in the image (dev machines and CI always hold the whole uv workspace; that is exactly how it broke once). Non-root user. Single uvicorn worker is load-bearing (ADR-0003). |
| `scripts/docker-entrypoint.sh` | The shell stays **PID 1** and forwards `SIGTERM` to both children. Do not reintroduce `exec` after a `trap` — that destroys the trap and orphans the store. |
| `render.yaml` | Free tier by documented operator decision; **no disk**, so `/data` is ephemeral and evidence does not survive a redeploy. |
| `docs/release-checklist.md` | The operator-attested gate before a release. |
