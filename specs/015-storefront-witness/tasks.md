# 015 — tasks

Cite the T-ID in every commit that advances it.

- [x] T1 — `external_audit` module enablement: per-workspace origin
      assertion (FR-110), server-controlled allowlist, exact-origin
      validation, capability/module reporting.
- [x] T2 — External target adapter through the public ports: Shopify-shaped
      `cart.js` observation normalized to `target.cart`;
      `observation_unavailable` for channels §12.17 does not name; no
      self-report promotion.
- [x] T3 — Shopify ten-tool contract pack as data; `proceed_to_checkout`
      and `manage_orders` as present-but-never-invoked entries; pack test
      asserting no contract can dispatch them.
- [x] T4 — Controlled fixture page with a half-broken surface (read path
      works, cart tool lies); e2e audit journey against it.
- [x] T5 — Merchant-readable report rendering: §5 persona, consequence-first
      summary, engineer-grade evidence behind it.
- [x] T6 — Guardrail tests: unconfigured module unreachable, non-allowlisted
      origin refused at every layer, no crawling affordance exists.
- [x] T7 — Product copy: the Shopify context cited as published third-party
      reporting; no unverified claims (round-2 §5's evidence rules). Exit
      gate; traceability map extended.
- [x] T8 — The audit pass over the API: `GET /audits/packs` (FR-161's offer),
      `POST /audits/current/evidence` (browser transcript in, sealed report
      out, FR-117's cap enforced before parsing), `GET /audits/current/report`,
      `POST /audits/current/cancel`. §22's terminal `completed`/`cancelled`
      transitions, so an audit stops holding the workspace's live slot. Closes
      the gap T1–T7 left: every piece existed and no endpoint joined them, so
      the classifier and report composer were reachable only from tests and an
      authorized audit could never finish. Exit-gate criterion 2 now also
      drives the journey through HTTP.
      **Remaining for the full operator journey:** the browser client that
      enumerates `getTools()`, exercises the surface, reads `cart.js`, and
      POSTs the transcript. The server half is complete and tested; the UI is
      not built.
