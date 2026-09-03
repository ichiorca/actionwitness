# 011 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

## Blocked, deliberately

**Nothing here may be implemented yet.** BUILD_ORDER §7/M10's entry condition is
"AC-17 is green and the exact development-store configuration is locked", and
§7/M9 ends with "if it does not [pass], do not start Shopify work." AC-17 is
unproven: 010-T11 is an operator gate awaiting a real credential, a real
generation, and a human approving the variants.

This document is planning, not work. The distinction matters and is enforced:
`tests/integration/test_010_exit_gate.py::test_gate_6_shopify_work_has_not_started`
fails if any task below is ticked. Authoring the plan while the gate is closed
is how the work is ready the day it opens; ticking a task before then is the
thing the gate forbids.

**Two conditions, and the second is easy to forget.** AC-17 is the loud one. The
quiet one is "the exact development-store configuration is locked" — a store
origin, a variant id, and a currency that the operator has fixed and the server
holds. Starting without it means the first refusal test has nothing to be exact
*about*.

## What makes this milestone different

**This is the first target the project does not control.** Every earlier
milestone observed the Buggy Store, whose failures we injected on purpose. A
Shopify development store has its own behaviour, its own locale handling, and
its own release cadence. The harness cannot make it fail usefully; it can only
observe it honestly.

**The credential model inverts.** Until now the harness held a cookie and the
target held nothing. Here the *theme bridge* holds a short-lived bearer
credential issued for one pairing, the harness validates an exact `Origin`, and
neither side may persist a raw token. §15.7 is explicit that bridge endpoints
"never authorize with a caller-supplied workspace ID" — the pairing is the
identity, not the workspace.

**The most likely dishonest outcome is a claim about trajectory.** The bridge
does not intercept Shopify's internal WebMCP dispatch, so what tools Shopify's
own agent selected is simply not observable from here. §12.9 and AC-18 both
require tool selection, observed trajectory, and execution to stay
`not_evaluated`. The temptation is to infer them from the cart having changed —
which would be exactly the self-report-as-proof error the product exists to
refuse, committed against a target we cannot re-run.

**Cart-only is a safety boundary, not a scope preference.** The project rules
fix one authorized development store, one configured variant and currency, and
cart-only behaviour. Checkout navigation, order creation, payment scope, and
Admin API use are refused rather than merely unimplemented, because an
accidental order on somebody's store is not recoverable by a code change.

**Money and locale are where correctness quietly fails.** `/cart.js` is
locale-aware and returns money in minor units; a normalizer that assumed a
currency or divided by 100 in the wrong place would produce totals that look
plausible and are wrong. Exact decimals and the configured currency, or a
refusal.

---

1. **Locked configuration.** Store origin, test variant id, and expected
   currency held server-side and never accepted from a client. Absent
   configuration disables the module and leaves every other tier working.
2. **Pairing.** One-time hashed credential, exact HTTPS-origin validation,
   in-memory bridge-session credential, 15-minute expiry, exact-origin CORS.
3. **The theme bridge.** Removes the URL fragment immediately; stores no raw
   credential or cart token.
4. **Bounded observation.** Locale-aware same-session `/cart.js` before and
   after, bounded and redacted.
5. **Normalization.** Money, variant, quantity, currency into `target.cart`
   with `platform_session_api` provenance and `shopify_cart_state` as the
   observation provider.
6. **Arming.** Only after an empty initial cart passes; `verify_shopify_outcome`
   registered only while the pairing is valid.
7. **Atomic finalization.** Pairing and run finalized together; unregistration
   on every terminal and teardown state (§16.5).
8. **Honest non-evaluation.** Selection, trajectory, and execution stay
   `not_evaluated` without correlated evaluator evidence.
9. **Refusals.** Checkout navigation, order/payment scope, unexpected variants,
   conflicting second observations, expired or reused credentials,
   non-configured origins — each with its own test.
10. **The exit gate.** AC-18 with no Admin, customer, payment, or
    production-store credential; extend the traceability map to 011.

---

## Carried forward

- **The `shopify` integration is an unmounted scaffold.** The project rules are
  explicit that `integrations/shopify`, its FastAPI router, and
  `shopify_bridge` are scaffolds with no behaviour, and that a session must
  re-scan manifests and imports before Shopify work. Treat nothing there as
  working until read.
- **010's D-series applies to correlation, not to this milestone.** AC-18's
  trajectory clause is satisfied by leaving those layers `not_evaluated`, which
  needs no evaluator report at all.

---

## Deviations ledger (implementation)

Each departure from the spec, anchored to the section it departs from, with what
was taken and why.

_Preamble: implementation began ahead of the entry condition by explicit
operator decision. AC-17 remains unproven and the store configuration stays
locked to the one authorized development store; nothing in `tasks.md` is
ticked until AC-17 actually passes (`test_gate_6_shopify_work_has_not_started`
enforces exactly that)._

- **D1 — §13.5, `target.cart.totals_consistent`.** §9.4's eight operators
  compare a path to a literal and cannot compare a path to a *sum* of other
  paths, so "internal arithmetic consistency" is computed in exact `Decimal`
  during normalization and published as one boolean the contract asserts.
  Safe: the arithmetic runs over the same independently observed `/cart.js`
  payload, nothing from a self-report. It is a finding, not a refusal — an
  internally inconsistent cart fails the contract rather than being rejected
  as malformed input.
- **D2 — §9.1/§10.2, `ShopifyAdapter.tool_specs()` returns `()`.** Stronger
  than FR-114 asks. §10.2 then refuses any contract for this target that names
  a tool at all, so no `forbidden_tool` / idempotency / confirmation policy can
  ever be attached — including via a caller-supplied `contract_id`. A
  `forbidden_tool: proceed_to_checkout` would report *satisfied* on the
  strength of having observed nothing. Checkout is refused instead by the
  witnessed `page.checkout_navigation_observed` assertion and FR-114's
  missing-final-observation rule.
- **D3 — §13.5 with FR-021/FR-110, "parameterized" means server expansion,
  not form parameters.** `ContractTemplate.parameters` is correctly empty;
  `variant_id` and `expected_currency` are *required* arguments to
  `templates.expand`, supplied from `ShopifyAdapter.contract_parameters()` by
  the composition root (spread last, so nothing arriving from a request can
  survive the merge). A template that published them as form parameters would
  render the deployment's locked configuration as fields a caller fills in.
