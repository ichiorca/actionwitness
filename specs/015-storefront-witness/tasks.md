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
- [x] T9 — The operator journey in the browser: an Audit view with its own rail
      entry; authorization for one origin behind an explicit affirmation; the
      pack catalogue offered and never auto-selected (FR-161); a collector
      generated *from the chosen pack* that bakes in FR-162's never-invoked list
      and reads `cart.js` in the operator's own session (§25.8); transcript
      submission; and the merchant report with its limits rendered.
      **Why a snippet and not a button:** a document can enumerate only its own
      `modelContext`, and `cart.js` is a read of the caller's own session —
      neither crosses an origin, and the one design that would appear to (a
      server-side fetch) is what §12.17 forbids. The generated collector runs on
      the storefront and the transcript comes back; that is the trust boundary,
      not a workaround for it.
      Collector text lives in `webmcp/auditCollector.ts` so the isolation gate
      still covers the component that displays it.
