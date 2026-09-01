# 003 — Standalone Buggy Store and its adapter (M2)

**Source:** `docs/BUILD_ORDER.md` §7/M2 · functional spec v1.9 §13, App. D.2
**Goal:** create the real target boundary before the harness orchestrates it —
a Buggy Store that runs and tests with every assurance package absent, plus the
adapter that reaches it only through its versioned HTTP API.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M2; nothing here is invented.

## Scope (implementation areas)

**Buggy Store (`examples/buggy_store`)**

- Immutable catalog models and seeded products (§13.1: mug, notebook, tote;
  `SAVE20`; `line_key` as the canonical object key under `target.cart.items`).
- A standalone SQLite-backed per-workspace repository for canonical cart and
  order state with decimal-string money, monotonic `state_version`, and
  idempotency records. It declares its own async SQLite dependency and takes no
  assurance dependency.
- Normal idempotent absolute cart mutation, discount application, confirmation
  request/decision/cancel, and protected checkout.
- `pre_fix`/`post_fix` scenario selection and the Tier 1
  `discount_reported_but_not_applied` fault (§13.3). The other injected profiles
  stay disabled until their Tier 3 work is complete.
- The complete `/demo/api/v1` surface (§15.5) with Pydantic validation, stable
  errors, and an ordinary human UI.
- Run-lock policy stays outside the store's business layer: the harness
  authorizes whether a human or agent mutation may be dispatched.

**Adapter (`integrations/buggy_store`)**

- The five allowlisted `TargetToolSpec` records and schemas from Appendix D.2:
  `search_catalog`, `get_cart`, `update_cart`, `apply_discount`,
  `proceed_to_checkout`.
- The deterministic effect map of §13.4.
- `prepare`/`execute`/`observe` only through the target API client chosen in
  ADR-0001 (one injected `httpx.AsyncClient`; ASGI transport in tests).
- Canonical target state normalized under `target`, with provider
  `buggy_store_state` and `state_version` as observation metadata.
- Buggy Store contract templates in the integration package. All three required
  prebuilt contracts are seeded and exposed in Tier 1; the retry contract
  exercises correct idempotent behavior, and the deliberately broken
  duplicate-retry profile stays unavailable until its Tier 3 injector and
  acceptance test ship.

## Acceptance criteria / exit gate

1. Buggy Store runs and tests with all assurance packages absent.
2. Normal retries return the first persisted result; conflicting request-ID
   reuse returns a non-retryable conflict.
3. A direct target API test proves the discount fault reports success without
   changing canonical state only in `pre_fix`.
4. Adapter conformance tests prove allowlisting, schema validation, effect
   metadata, prepare/execute/observe behavior, and no service import.
5. One target call is traced end to end: harness-facing arguments → adapter HTTP
   request → store mutation → authoritative adapter observation.

## Non-goals

- No harness persistence, workspace isolation, or API orchestration (004).
- No run lifecycle, guidance, or verification endpoints (005).
- No UI or WebMCP registration (006).
- The `duplicate_on_retry`, `checkout_without_confirmation`,
  `undeclared_side_effect`, and `tool_surface_poisoned` profiles stay disabled.

## Implementation order (normative)

1. seeded catalog and canonical-state models → 2. standalone repository with
idempotency records → 3. cart mutation, discount, confirmation, checkout →
4. `/demo/api/v1` surface and human UI → 5. scenario selection and the discount
fault → 6. adapter tool specs, effect map, and observation mapping →
7. end-to-end trace through the versioned API.
