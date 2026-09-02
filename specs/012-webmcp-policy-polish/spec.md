# 012 — Tier 3 priority 3: optional WebMCP and policy polish (M11)

**Source:** `docs/BUILD_ORDER.md` §7/M11, §12 row 13 · functional spec v1.9 §26.2, AC-02, AC-05, AC-07, AC-13, AC-14
**Goal:** close the remaining breadth items — each one complete, gated by its own
acceptance criterion, or visibly absent.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M11; nothing here is invented.

> **Entry condition.** §7.3 locks the Tier 3 order: the live LLM benchmark
> first, the authorized external-surface audit including the Shopify proof
> second, and this polish third. AC-17 is unproven (010-T11) and AC-18 has not
> been attempted (011), so this milestone is planned rather than started.

**Every item here is optional, and that is the point.** BUILD_ORDER: "Implement
only complete features, in this order." An item that ships must be whole and
carry its acceptance criterion; an item that is cut must have its control and
tool registration removed or visibly disabled. A half-built feature behind a
live button is the one outcome this milestone forbids outright.

## Scope (implementation areas, in the required order)

1. **`duplicate_on_retry` injector** plus idempotency evidence — AC-05.
2. **`checkout_without_confirmation` injector** — AC-07.
3. **Observed-trajectory edge cases** — AC-13.
4. **Invocation `AbortSignal` propagation** — AC-14.
5. **Flat declarative contract-instantiation form** — AC-02.
6. **`getTools()` / `toolchange` reconciliation.**
7. **SSE**, only if polling is already stable and all earlier gates remain
   green.

## Acceptance criteria / exit gate

1. AC-02, AC-05, AC-07, AC-13, and AC-14 are green **for what ships** — an
   item's criterion is required if and only if the item shipped.
2. Every cut item has its control and tool registration removed or visibly
   disabled; no partially implemented feature is exposed.
3. Product copy claims nothing unshipped (constitution §8).
4. Every earlier gate remains green; SSE ships only if polling is stable and
   nothing earlier regressed.
5. Each shipped item has its own integration test through the real boundary
   (§26.2's "if shipped ... each receive one integration test").

## Non-goals

- No new target, tier, or tenancy.
- No SSE unless the polling timeline is already stable — it is a stretch item,
  not a replacement for the Tier 1 transport.
- No partially implemented feature behind a working control, under any
  schedule pressure.
