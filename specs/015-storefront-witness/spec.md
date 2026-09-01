# 015 — Storefront Witness (§12.17, FR-110, round-2 feature #1)

**Source:** functional spec v1.9 §12.17 (external audit), FR-110
(authorized-origin assertion), §5 (site-owner persona), §25.8 ·
`docs/actionwitness-top3-features-round2.md` §0–§2/#1
**Goal:** audit a live WebMCP surface the operator is authorized on but did
not build — the population that was *given* agent tools without asking —
and produce a report a site owner, not a harness engineer, can act on. The
`external_audit` module has shipped as fail-closed configuration since
001-T8; this spec makes it real.

> **Draft.** Staged for operator rename per `specs/README.md`. Shares
> normalization engineering with the reserved 011 (Shopify dev-store proof)
> but is NOT entry-gated on AC-17: the audit path needs an authorized
> origin, not an owned store.

## Scope

- **Authorized-origin flow (FR-110)**: the operator supplies one origin they
  own or administer and asserts that authorization; recorded with the
  workspace; nothing proceeds without it. Never a crawler; one origin at a
  time; `EXTERNAL_AUDIT_ALLOWED_ORIGINS` remains server-controlled.
- **External surface discovery**: enumerate the origin's registered tools
  through the same browser adapter (`getTools()`), classified as an
  external, untrusted surface; 014's capture machinery records it when both
  specs are live.
- **Shopify ten-tool contract pack**: built-in contracts for the publicly
  documented tool names (`search_catalog`, `browse_store`, `get_product`,
  `show_variant`, `get_cart`, `update_cart`, `cancel_cart`,
  `proceed_to_checkout`, `manage_orders`,
  `search_shop_policies_and_faqs`) — read and cart-only assertions;
  `proceed_to_checkout` and `manage_orders` are enumerated as
  NEVER-INVOKED entries whose presence is reported, not exercised.
- **Observation**: for Shopify-shaped origins, the locale-aware
  `GET /cart.js` session read (the §25.8 mechanism) normalized into
  `target.cart`; for arbitrary origins, observation is whatever §12.17
  permits — and when no independent channel exists, the report says
  `observation_unavailable` rather than trusting tool output.
- **The merchant report**: which agent tools exist, which respond, which
  silently fail (reported success vs observed cart), and what an agent
  could do to business state — consequence-first language per the §5
  persona; no harness vocabulary in the summary section.

## Guardrails (non-negotiable, from the source document and §20)

- Operator-asserted authorized origin only. Read/cart-only. Never checkout,
  never an order, no crawling, no scanning of unowned brands.
- Public breakage findings cited in product copy are *published third-party
  reports*, never presented as this product's own scans.

## Acceptance criteria / exit gate

1. With the module unconfigured, nothing about the feature is reachable and
   the capability surface reports it unavailable (the 009-T12 mechanism).
2. An audit against a controlled fixture page (a local storefront serving a
   deliberately half-broken WebMCP surface) produces the merchant report:
   working tools green, silently-failing cart tool red with reported-vs-
   observed evidence, never-invoked tools listed as present-but-unexercised.
3. A non-allowlisted origin refuses at every layer.
4. The report renders without WebMCP and without the harness UI vocabulary.
5. Full suite, architecture lane, both frontend gates green.

## Non-goals

- No live scans of third-party brands, ever, in any test or demo.
- No dev-store pairing/theme bridge (that remains 011's scope if revived).
- No claim of damage that has not happened (round-2 §0's counterweight).
