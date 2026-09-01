# 011 — tasks

Cite the T-ID in every commit that advances it.

**Entry condition (BUILD_ORDER §7/M10): AC-17 green and the exact
development-store configuration locked. Neither holds today** — 010-T11 is an
operator gate awaiting a live run. §7/M9 says "if it does not [pass], do not
start Shopify work", so **no task below may be ticked** until the operator
records that AC-17 passed and the store configuration is fixed.
`test_gate_6_shopify_work_has_not_started` enforces this.

- [ ] T1 — Locked configuration: store origin, test variant id, and expected
      currency resolved server-side as a module, never accepted from a client.
      Absent configuration disables Shopify and leaves every other tier fully
      working.
- [ ] T2 — Pairing: one-time hashed credential, exact HTTPS-origin validation,
      in-memory bridge-session credential, 15-minute expiry, exact-origin CORS.
      No raw credential is persisted, logged, returned by the status endpoint,
      or placed in an artifact.
- [ ] T3 — The theme bridge: removes the URL fragment immediately; stores no
      raw credential and no cart token.
- [ ] T4 — Bounded, locale-aware `/cart.js` observation before and after,
      redacted, built from `window.Shopify.routes.root + 'cart.js'`; reject a
      redirect or final URL outside the exact configured HTTPS origin.
- [ ] T5 — Normalization into `target.cart`: money as exact decimals, variant,
      quantity, and the configured currency, with `platform_session_api`
      provenance and `shopify_cart_state` as the observation provider.
- [ ] T6 — Arming: only after an empty initial cart passes; register
      `verify_shopify_outcome` only while the pairing is valid.
- [ ] T7 — Idempotent capture by `(pairing_id, phase, content_hash)`: a repeat
      with the same hash returns the existing result; a different second
      payload for the same phase returns `409 OBSERVATION_ALREADY_CAPTURED`.
- [ ] T8 — Atomic finalization of pairing and run together; unregister on every
      terminal and teardown state (§16.5).
- [ ] T9 — Honest non-evaluation: model selection, observed trajectory, and
      tool execution stay `not_evaluated` without correlated evaluator
      evidence. Inferring them from a changed cart would be the self-report-as-
      proof error this product exists to refuse.
- [ ] T10 — Refusals, one test each: checkout navigation, order or payment
      scope, an unexpected variant, a conflicting second observation, an
      expired credential, a reused credential, and a non-configured origin.
- [ ] T11 — (operator gate) Run the proof against the authorized development
      store with the locked configuration; retain the evidence.
- [ ] T12 — The exit gate: AC-18 passes with no Shopify Admin API credential,
      customer credential, payment credential, or production-store mutation;
      extend the architecture lane's traceability map to 011.
