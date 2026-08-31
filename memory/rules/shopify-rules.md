---
title: Shopify integration rules
scope: project
---

NO SHOPIFY SIDE EFFECT WITHOUT VERIFIED AUTHORITY, INDEPENDENT EVIDENCE, AND REPLAY SAFETY.

Violating the letter of these rules is violating the spirit of these rules.

- MUST re-scan manifests and imports before Shopify work; the current `integrations/shopify`, FastAPI router, and `shopify_bridge` are unmounted scaffolds with no Shopify SDK, HTTP client, webhook transport, or bridge implementation.
- MUST preserve the current scope: one authorized development store, one configured variant/currency, cart-only behavior, native Shopify WebMCP mutations, and same-tab Ajax Cart observation. NEVER add Admin, customer, checkout, order, payment, or production-store credentials to this proof path.
- MUST build locale-aware cart reads from `window.Shopify.routes.root + 'cart.js'`; MUST reject redirects or final URLs outside the exact configured HTTPS storefront origin. NEVER treat DOM text or tool output as authoritative state.
- MUST keep store origin, variant, and currency server-controlled. MUST authenticate every bridge request with a short-lived, origin/workspace-bound bearer credential; persist only its hash and NEVER place credentials, cart tokens, or raw payloads in query strings, browser storage, logs, telemetry, reports, or tool results.
- MUST keep any future Shopify client secret and access tokens server-side with least scopes and shop binding. NEVER derive shop identity or resource authority from caller-supplied domains or IDs.
- MUST, if HTTPS webhooks are introduced, verify `X-Shopify-Hmac-SHA256` over the exact raw request bytes with constant-time comparison before parsing or trusting topic, shop, IDs, or payload. NEVER add a deployed verification bypass.
- MUST persist and deduplicate webhook deliveries by `X-Shopify-Webhook-Id`; use `X-Shopify-Event-Id` only for correlation. MUST make downstream effects independently idempotent and tolerate duplicate, stale, out-of-order, and missing deliveries.
- MUST assign distinct durable idempotency keys per logical operation and reuse a key only for identical intent. For Shopify mutations, use idempotency only where the current schema explicitly supports it; after an ambiguous WebMCP timeout, re-observe the same-session cart before retrying.
- MUST rotate future client secrets as a transition: accept old and new webhook secrets during Shopify’s documented overlap, replace dependent access tokens, then revoke the old secret. NEVER switch verification to the new secret prematurely.
- MUST verify Shopify changes with `uv run pytest -q`, `uv run pytest tests/architecture -q`, and, after `npm install`, frontend `npm run test` and `npm run build`.

| Excuse | Reality |
|---|---|
| “The tool returned success.” | Tool output is a claim; require final same-session cart evidence. |
| “Retrying once is harmless.” | A lost response can hide a committed cart mutation; observe before retry. |
| “CORS proves it came from our store.” | CORS is defense in depth, not bridge authentication. |
| “We can add the token now and secure it later.” | The current cart proof requires no Shopify API credential. |

Red flags — STOP: “just replay it”; “the parsed JSON is equivalent”; “CORS is enough”; “temporary client-side secret”; “we’ll dedupe in memory.”

<!-- sources fetched at generation: https://shopify.dev/docs/api/web-mcp, https://shopify.dev/docs/api/ajax, https://shopify.dev/docs/apps/build/security/following-security-best-practices, https://shopify.dev/docs/apps/build/webhooks/verify-deliveries, https://shopify.dev/docs/apps/build/webhooks/delivery-structure, https://shopify.dev/docs/api/usage/implementing-idempotency, https://shopify.dev/docs/apps/build/authentication-authorization/manage-credentials -->
