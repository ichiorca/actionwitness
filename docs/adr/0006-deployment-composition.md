# ADR-0006 — Deployment composition

- **Status:** Accepted
- **Date:** 2026-09-01
- **Implementing change:** 009-T1/T2/T3 — `Dockerfile`, `scripts/docker-entrypoint.sh`, `apps/actionwitness_service/src/actionwitness_service/api/composition.py`

## Context

Spec §29.1 requires one Render web service serving the harness at `/`, the
harness API at `/api/v1`, and the Buggy Store at `/demo` plus `/demo/api/v1`. The
same sentence adds the constraint that decides this record: **"process
co-location shall not bypass the versioned target API or adapter boundary."**

That clause exists because co-location makes the wrong thing easy. Once both
applications are in one container, `from buggy_store.service import StoreService`
works, is faster than an HTTP call, and needs no port. It is also the end of the
product: ActionWitness's entire claim is that it observes authoritative business
state through a channel the tool under test does not control (constitution §5,
"A tool's self-report is evidence, never proof"). An in-process call into the
demo target's own service object is not an independent channel — it is the same
process reading its own memory, and no test in the suite would fail.

Three further constraints bind the choice:

- §26.7 and the 003 exit gate require the Buggy Store to install and run with
  every assurance package absent. A claim like that decays quietly unless the
  artifact preserves it.
- ADR-0003's `BEGIN IMMEDIATE` + busy-timeout model assumes a single writer
  process per SQLite file. §29.1 says one instance, one Uvicorn worker.
- Render provides one port. Whatever runs must present a single listener.

## Decision

**The harness and the Buggy Store run as two processes in one container, in two
separate virtualenvs, and communicate only over the store's versioned HTTP API on
loopback.**

- `scripts/docker-entrypoint.sh` starts `/opt/store/bin/buggy-store` bound to
  `127.0.0.1:$BUGGY_STORE_PORT`, then `exec`s Uvicorn with `--workers 1` on
  `$PORT`. The store is never published outside the container.
- `api/composition.py` owns the mount points. `/demo/api/v1/**` is reverse-proxied
  to the store process over the lifespan-owned HTTPX client — the same client
  `integrations.buggy_store` uses, so the adapter and the storefront reach the
  target by the same route.
- `/` and `/demo` are **static** and are served by the harness process from
  `HARNESS_STATIC_ROOT/harness` and `HARNESS_STATIC_ROOT/demo` (§29.1 step 4).
  Only the versioned API is proxied; the store process has no frontend to serve.
- The image builds two virtualenvs, `/opt/harness` and `/opt/store`. The store's
  environment contains no `actionwitness` distribution and not even `httpx`.
- Headers crossing the proxy are allowlisted. `X-Workspace-Id` passes through
  untouched; the harness workspace cookie is never forwarded.

## Consequences

### Positive

- The §25.11 boundary is a property of the artifact rather than a convention.
  Reaching past it requires adding a dependency to a manifest and an import to a
  file, both of which are reviewable; today it would fail at runtime, because the
  package is not installed in that virtualenv.
- The composed path is the developed path. The adapter's HTTP calls, the
  storefront's fetches, and the integration tests all traverse the same versioned
  surface, so a bug in that surface cannot hide in one configuration.
- The store keeps its own database file, migration runner, and per-workspace
  seeding, so `§29.1`'s "recover to a known seeded state after restart" needs no
  deployment-specific code.
- One listener on `$PORT` satisfies the platform without a supervisor or a second
  HTTP server in front.

### Negative

- **Two processes, one container, no supervisor.** If the store exits, the
  harness stays up and `/demo/api/v1` answers `TARGET_UNAVAILABLE` until the
  container is restarted; nothing restarts the store in place. Chosen over adding
  a supervisor dependency for a single-container demo, and the failure is at
  least loud and correctly classified. Follow-up if this deployment outlives the
  submission: a real process supervisor, or two Render services.
- Uvicorn is PID 1 after `exec`, so the store process is not reaped and signal
  handling for it is left to container teardown. Acceptable for a process whose
  lifetime equals the container's; not acceptable if the store ever needs to be
  restarted independently.
- The image carries two Python environments, duplicating FastAPI, Starlette,
  Pydantic, and Uvicorn — roughly a few tens of megabytes. That is the price of
  the isolation claim, paid in image size rather than in review vigilance.
- The proxy is a second place that must not leak headers. It is an allowlist, and
  the allowlist has a test, but it is code that would not exist under a single
  process.
- Buffering the proxied body caps request size at 64 KiB (§20.2). Fine for this
  API; it would need streaming if the store ever accepted an upload.

## Rejected alternatives

### Import the Buggy Store into the service process

The obvious option, and the one §29.1 explicitly forecloses. It deletes the port,
the proxy, the second virtualenv, and this record. It also makes the
"authoritative observation" a function call into the same process that produced
the tool response, which is the exact failure mode the product exists to detect.
The reason still holds in a year: an independent observation cannot be
independent of itself.

### One virtualenv for both applications

Cheaper image, same two processes. Rejected because it puts `actionwitness_core`
one `import` away from the demo target with nothing but discipline in between.
§26.7's isolation would then hold only because nobody had yet written the import,
and the architecture gate that asserts it runs against the source tree, not
against the artifact.

### A process supervisor with a fronting router (nginx, supervisord, honcho)

Handles restarts and signals properly, and would remove both of the first two
negatives above. Rejected for V1 on dependency grounds: it adds a runtime
component that must be pinned, configured, audited, and reasoned about for header
handling, to solve a problem — in-place restart of a demo target — that a
container restart already solves. Worth revisiting the moment this deployment is
expected to survive unattended.

### Proxy all of `/demo/**` rather than only `/demo/api/v1/**`

Simpler to describe. Rejected because it is wrong: the store process serves no
frontend, so every storefront asset request would be forwarded to a process that
would answer 404. The split follows the fact that §29.1 step 4 copies each
application's assets into the *harness* image, not into the store.

## Notes

Verified by `tests/integration/test_one_origin_composition.py`, which asserts the
mount points, the header allowlist in both directions, workspace isolation across
the proxy, and that an unreachable store produces an explicit non-pass rather
than anything a client could read as success.

The single-worker rule is load-bearing and is stated in three places on purpose —
this record, the `Dockerfile` header, and the entrypoint. A future change that
"tunes up" `--workers` introduces a second writer against one SQLite file, and
ADR-0003's lock model has no answer for it.

A superseding record is justified if the deployment moves off SQLite (which
removes the single-worker constraint), or if the store is promoted to its own
service (which removes the entrypoint and the loopback port, but not the proxy).
