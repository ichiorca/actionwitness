# 003 — tasks

Cite the T-ID in every commit that advances it.

- [x] T1 — Seeded catalog and canonical-state models (§13.1, §13.2): immutable
      products, `line_key` as the canonical cart key, decimal-string money,
      `preferences` present; unit tests for decimal arithmetic and the exact
      state shape.
- [x] T2 — Standalone per-workspace SQLite repository with monotonic
      `state_version` and idempotency records, following ADR-0003 (WAL, foreign
      keys, busy timeout, `BEGIN IMMEDIATE`); its own async driver declared in
      its own `pyproject.toml`; isolation tests across two workspaces.
- [x] T3 — Absolute idempotent cart mutation: identical `(request_id, payload)`
      returns the first persisted result; reuse with a different payload returns
      a non-retryable `IDEMPOTENCY_KEY_REUSED` conflict and mutates nothing.
- [x] T4 — Discount application, including the repeated-code no-op reporting
      `already_applied`; decimal totals asserted against §13.2.
- [x] T5 — Confirmation request, decision, and cancel, plus protected checkout
      that consumes a valid single-use approval; denial, expiry, and cancel
      create no order.
- [x] T6 — The `/demo/api/v1` surface from §15.5 with Pydantic validation and
      stable errors; API tests through the real entry point.
- [ ] T7 — The ordinary human storefront UI, usable with no harness and no
      WebMCP; its own frontend tests and build.
- [x] T8 — `pre_fix`/`post_fix` scenario selection and the Tier 1
      `discount_reported_but_not_applied` fault; a direct target API test proves
      the tool reports success while canonical state is unchanged, in `pre_fix`
      only. The other four profiles stay disabled and visibly labelled.
- [ ] T9 — The store runs and tests with every assurance package absent: extend
      the architecture lane with a standalone install/run job for
      `examples/buggy_store`, mirroring the core-only job from 002-T13.
- [x] T10 — Adapter: the five Appendix D.2 `TargetToolSpec` records and schemas,
      the §13.4 effect map, and allowlist enforcement; conformance tests
      including rejection of a tool outside the allowlist.
- [x] T11 — Adapter `prepare`/`execute`/`observe` through the ADR-0001 injected
      `httpx.AsyncClient` (ASGI transport in tests); canonical observation
      mapped under `target` with provider `buggy_store_state` and
      `state_version` as metadata; a test proves no store service import.
- [x] T12 — Buggy Store contract templates in the integration package: all three
      required prebuilt contracts seeded and exposed, with the retry contract
      exercising correct idempotent behavior and the broken duplicate-retry
      profile unavailable.
- [x] T13 — Trace one target call end to end — harness-facing arguments →
      adapter HTTP request → store mutation → authoritative adapter observation
      — and verify the full exit gate.
