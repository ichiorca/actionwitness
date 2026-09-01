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

## Status — 003 complete (2026-08-31)

All thirteen tasks landed with their tests. `uv run pytest -q` is green (1191),
`uv run pytest tests/architecture -q` is green (77, now including both isolation
jobs), and the storefront's four frontend commands pass from a clean `npm ci`.

Exit gate, item by item:

1. **The store runs and tests with all assurance packages absent.**
   `scripts/store_only_isolation.py` installs only `buggy-store`, proves the
   assurance stack is missing, performs a real storefront journey, and runs its
   208 tests there. Wired into the architecture lane.
2. **Normal retries return the first persisted result; conflicting reuse is a
   non-retryable conflict.** Tested at the repository, service, API, and adapter
   layers.
3. **The discount fault reports success without changing canonical state, in
   `pre_fix` only.** Reproduces Appendix B exactly, including the unchanged
   `state_version`. Proven through the versioned API and again through the
   adapter.
4. **Adapter conformance.** Allowlisting, schema validation, effect metadata,
   prepare/execute/observe, and no service import — the last checked by module
   graph rather than by reading the source.
5. **One target call traced end to end**, including the HTTP request the adapter
   actually sent.

### Deviations and decisions worth an operator's eye

- **`RetrySemantics.naturally_idempotent` was added to the core.** Appendix
  D.2's `apply_discount` mutates, carries no request ID, and cannot be duplicated
  by repetition, so all three existing values would have been false statements in
  published metadata. This plan's own cross-cutting note called for exactly this:
  a gap the adapter reveals belongs in a core change.
- **Three endpoints are project-allocated**, because §15.5's table predates what
  the adapter needs. `POST /store/scenario` performs
  `ManagedTargetAdapter.prepare` (§9.1 defines it as restoring a fixture *and*
  selecting a scenario); `GET /store/scenario` publishes what FR-017's panel
  needs; `GET /store/state` returns the whole §13.2 document, because an
  observation that could not see `preferences` would make §12.16 structurally
  impossible. **Worth confirming before the API surface is frozen.**
- **`X-Workspace-Id` is project-allocated.** §15.5 names no scoping mechanism.
  It is the store's isolation scope, never an authorization one; the harness's
  cookie sits in front of it.
- **Only two of the six fault profiles are implemented**, per BUILD_ORDER §7/M2.
  The other four are recognised, described, and refused with
  `FAULT_PROFILE_UNAVAILABLE` rather than downgraded to `none`.
- **Three contract templates ship, not one per Tier 1 profile.** FR-020 frames
  the three as "corresponding to the three failure profiles", and Tier 1's third
  is `undeclared_side_effect`, which has no injector in this build. Shipping a
  fourth template asserting a fault nothing can produce would look like coverage
  and provide none, so a test fails if any template ever claims an uninjectable
  profile. **The `undeclared_side_effect` contract arrives with its injector.**
- **An approved confirmation now lapses at expiry, not only a pending one.** A
  test caught the original behaviour; the code was wrong. FR-066 lists "expired"
  among the confirmations that may never authorize, and an approval valid
  indefinitely would let someone who approved and walked away authorize whatever
  happened next.
- **Two test-isolation defects were fixed along the way**, both of which passed
  in isolation and failed only in a full-suite run: the adapter lane purged
  `actionwitness_core` from `sys.modules` without restoring it, and the leak
  checks scanned global `sys.modules` rather than what the import under test
  actually pulled in.
- **The isolation probes now run from a neutral directory.** `python -c` puts
  the working directory on `sys.path`, and this repository has bare
  `integrations/` and `packages/` folders at its top level, so a probe run from
  the root reported a leak that was a directory on disk rather than anything
  installed.

### Not done here

- `duplicate_on_retry`, `checkout_without_confirmation`, `undeclared_side_effect`
  and `tool_surface_poisoned` injectors (Tier 2/Tier 3, per BUILD_ORDER).
- The harness side of everything: workspace persistence, run lifecycle, guidance,
  and the WebMCP surface are 004-006.
