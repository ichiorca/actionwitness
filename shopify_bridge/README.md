# shopify_bridge (Tier 3 priority 2 — scaffold placeholder)

Minimal theme bridge for the authorized Shopify development store
(spec v1.9 §12.12, FR-111/112/115, Appendix D.3). Planned contents per §18:

- `package.json` — build tooling for the bridge script
- `src/bridge.ts` — pairing redemption (URL-fragment credential, removed
  immediately, redeemed once), before/after locale-aware `GET /cart.js`
  capture, HTTPS submission to the exact harness origin, and the
  state-dependent `verify_shopify_outcome` tool registration
- `snippets/actionwitness.liquid` — theme snippet, loaded before
  unrelated third-party scripts, install/removal documented

Do not start this directory until the Tier 2 release gate is green (§7.3/§7.5).
