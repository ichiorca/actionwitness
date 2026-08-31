# 003 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

The organising constraint is BUILD_ORDER invariant 2: **the Buggy Store imports
no assurance package and runs by itself.** `tests/architecture` already enforces
that on `examples/buggy_store/src`, so the store is built first and completely,
and the adapter is added afterwards from the outside. Building them together is
how a demo service object ends up imported into an integration.

1. **Catalog and canonical state** — immutable seeded products from §13.1, and
   the §13.2 state shape including `preferences`, which exists so a later
   journey can change a path no cart contract asserts. Money is decimal strings
   or `Decimal`, never binary float (§13.2). `line_key` is stable fixture
   metadata and the canonical key under `target.cart.items`.
2. **Standalone repository** — its own SQLite, its own async driver dependency,
   per-workspace isolation, monotonic `state_version`, and idempotency records.
   ADR-0003 fixes WAL, foreign keys, the 5,000 ms busy timeout, and
   `BEGIN IMMEDIATE`; the store follows it even though the harness tables are
   M3's, because a second transaction model is harder to remove than to avoid.
3. **Business operations** — absolute cart mutation, discount, confirmation
   request/decision/cancel, protected checkout. Normal retry semantics are part
   of *correct* behavior and are tested here (App. D.2): an identical
   `(request_id, payload)` returns the first persisted result; a reused ID with
   a different payload is `IDEMPOTENCY_KEY_REUSED`, `retryable: false`; a
   repeated `apply_discount` for the active code is a no-op reporting
   `already_applied`.
4. **`/demo/api/v1` and the human UI** — the §15.5 endpoint table, Pydantic
   validation, stable errors, and an ordinary storefront a person can use. The
   UI matters beyond demo polish: AC-03 compares what a human sees with what the
   adapter observes, and it is the fallback when WebMCP is absent.
5. **Scenario selection and the one Tier 1 fault** — `pre_fix`/`post_fix` plus
   `discount_reported_but_not_applied` (§13.3). The other four profiles are
   named in the spec and stay switched off; §20.4 requires an injected unsafe
   mode to be clearly labelled, and a half-built injector is worse than none.
6. **The adapter** — Appendix D.2's five tool specs verbatim, the §13.4 effect
   map, and observation mapping to the `target` namespace with provider
   `buggy_store_state`. Transport is ADR-0001's injected `httpx.AsyncClient`:
   a configured base URL in production, `ASGITransport` in tests, and no
   privileged path for replay.
7. **The end-to-end trace** — one call followed from harness-facing arguments to
   an authoritative observation, which is the exit gate's last item and the
   first time the M1 engine meets a real target.

Cross-cutting:

- **The adapter imports the store's HTTP contract, never its service objects.**
  This is the boundary the whole milestone exists to establish, and it is the
  one an integration author will be tempted to cross first.
- **The store is not a workspace owner.** Run-lock policy stays in the harness
  (M3/M4); the store answers requests and knows nothing about runs.
- **Observations come from the store's own state, not from tool responses.** The
  M1 port models make the two unconvertible; the adapter must keep them that way
  when the responses are real.
- **The discount fault must be provably a *false success*.** §13.3 requires the
  tool response to stay syntactically valid while canonical state disagrees. A
  fault that returned an error would prove nothing the execution layer does not
  already catch.
- The non-commerce adapter from 002-T12 is the conformance template: whatever
  the Buggy Store adapter needs that the support-desk fake did not is a signal
  the ports are underspecified, and belongs in a core change rather than a
  target-specific workaround.
