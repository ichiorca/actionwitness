# ADR-0001 — Buggy Store adapter transport

- **Status:** Accepted
- **Date:** 2026-08-31
- **Implementing change:** 001-T2 (record); `integrations/buggy_store` adapter in M2

## Context

`integrations/buggy_store` translates between `actionwitness_core` port protocols
and the Buggy Store's public versioned HTTP API at `/demo/api/v1`. The harness and
the store are separate distributions, but the event deployment co-locates them in
one Docker image behind one origin (spec §25.11, §29.1). Co-location creates the
temptation to call the store's Python service objects directly, which would be
faster and simpler.

Three rules forbid that shortcut:

- Locked decision 8 (spec §32): browser execution and replay both reach a target
  through registered adapter protocols and versioned target APIs; co-deployment
  never authorizes direct service imports.
- Spec §26.7 fails CI if `integrations.buggy_store` imports Buggy Store service
  objects, and if `examples/buggy_store` imports the assurance stack.
- The constitution's primitives table: the adapter owns *HTTP translation*, not a
  second copy of the store's business semantics.

Spec §33 open question 8 leaves the transport itself open: HTTP over loopback, an
ASGI test transport, or a small process-local HTTP client — whichever is chosen
must exercise the same versioned API contract an independently deployed target
would serve. This record closes that question. It is required before M2 adapter
work (BUILD_ORDER §6).

## Decision

The adapter takes **one injected `httpx.AsyncClient`** and never constructs its
own. The client is the adapter's only route to the store, and the adapter's
behavior is identical regardless of what that client is wired to.

The composition root owns the wiring and the client's lifetime:

- **Production and co-deployed:** `httpx.AsyncClient(base_url=<configured store base URL>)`
  — real HTTP, including over loopback in the composed image. The base URL is
  server-controlled configuration, never inferred from a request.
- **Tests:** `httpx.AsyncClient(transport=httpx.ASGITransport(app=<buggy store ASGI app>), base_url="http://buggy-store.test")`
  — the same request/response contract with no port allocation or live server.
- **Replay:** the same registered adapter and the same injected client type as an
  interactive run. Replay gets no privileged path (spec §32 decision 8).

The client is created and closed by the FastAPI lifespan, per the constitution's
rule that async clients are lifespan-owned and reused. `integrations/buggy_store`
declares `httpx` in its own `pyproject.toml`; `actionwitness_core` does not depend
on it and `tests/architecture` already lists `httpx` among the imports forbidden
to the core.

## Consequences

### Positive

- One code path. The adapter has no test-only branch, so what tests exercise is
  what production runs — the divergence that makes ASGI-only testing dangerous is
  removed by construction rather than by discipline.
- The forbidden-import gate stays mechanical: the adapter's only store-facing
  symbol is an HTTP client, so a direct service import is a visible new import
  rather than a subtle coupling.
- Fast, deterministic, port-free adapter conformance tests, which spec §26.2
  requires to run without a live server.
- The store stays independently deployable. Pointing the base URL at another host
  is a configuration change, not a code change.

### Negative

- **ASGI transport does not exercise the network.** It bypasses real connection
  handling, timeouts, redirects, and partial-response framing. Testing only
  through ASGI would leave the ambiguous-transport-outcome rail (constitution §5;
  spec §10 review gate "ambiguous transport outcomes are not automatically
  retried") untested. **Follow-up, owed in M2:** at least one integration test
  must drive the adapter against a real loopback HTTP server, and the timeout and
  ambiguous-outcome cases must be tested there, not against ASGI.
- `httpx` becomes a pinned runtime dependency of the integration distribution and
  must be license-checked and audited like any other (constitution §5).
- Client lifetime becomes a composition-root concern. A per-call client would leak
  connections; the lifespan ownership rule has to be enforced in review, since no
  gate detects it.
- Loopback HTTP in the composed image costs a real socket round trip per call.
  Acceptable for a single-worker demo; noted here so it is not rediscovered as a
  performance surprise.

## Rejected alternatives

### Direct Python import of Buggy Store service objects

Rejected: it violates locked decision 8 and fails the §26.7 architecture gate. It
would also defeat the product's own claim — an adapter that shares a process and a
call stack with the target is not an independent observer, and the harness would
be asserting against the same objects the tool mutated.

### Loopback HTTP in tests as well as production

Rejected: it requires provisioning and tearing down a live server and a port per
test. That is slow, order-sensitive, and flaky in CI — all of which the testing
rules forbid — for a fidelity gain that one dedicated loopback integration test
captures at a fraction of the cost. This alternative is not rejected outright so
much as narrowed: it survives as the required M2 follow-up above.

### A bespoke process-local HTTP client

Rejected: it is a second implementation of the store's public contract. It would
drift from the real API, and any drift would silently invalidate every adapter
test. The constitution's primitives table is explicit that this project does not
reimplement HTTP transport machinery.

## Notes

Superseding this record would require a new ADR. The most likely trigger is the
store moving out of the composed image to a separately deployed host, which this
decision already supports through configuration and would not by itself justify a
reversal.
