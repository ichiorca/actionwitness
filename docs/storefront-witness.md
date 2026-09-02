# Storefront Witness — auditing agent tools you did not install

Some storefronts have agent tools on them that their owners never installed, cannot
test, and cannot monitor. Storefront Witness is for those owners: it checks whether
those tools do what they say, on a store the operator is authorized on, and returns
a report written for the person who owns the shop.

This page is the product copy for that feature. It also states, in one place, what
this project claims and what it does not — because the claim is the part that is
easy to get wrong, and the whole feature is an argument that a confident report is
not the same thing as a true one.

## Why the feature exists

The situation it was built for is documented publicly by other people. Every claim
below is **published third-party reporting**, cited as such. ActionWitness has run
no scan of any brand it does not own, and the sources here are other people's work,
not ours.

**Verified first-hand against the primary source:**

- Shopify's developer changelog, **5 Aug 2026**, states the storefront agent tools
  are "live today on every Liquid storefront" and on the Hydrogen developer
  preview, with "nothing to install or configure". Ten tools are named:
  `search_catalog`, `browse_store`, `get_product`, `show_variant`, `get_cart`,
  `update_cart`, `cancel_cart`, `proceed_to_checkout`, `manage_orders`,
  `search_shop_policies_and_faqs`. No merchant opt-out is described for Liquid;
  Hydrogen developers have `webMcp={false}`.
- An independent tester published results against three live storefronts
  (Allbirds, Brooklinen, Partake Foods) on **6 Aug 2026**, one day after the
  announcement: the read path worked, while adding to the cart, reading the cart,
  and proceeding to checkout all failed on the same internal error. That report is
  theirs. We reproduce none of it, and this product ships no test or demo that
  contacts those stores.

**Reported by others and not verified by us** — repeated here because it is part of
the public record, and flagged because it has not been checked against a primary
source by this project:

- A source-code search returning ~222,974 **pages** carrying the tool loader. That
  is a page count, not a store count, and must not be restated as one.
- A July 2026 report that the injected loader leaked global variables and broke
  storefront JavaScript, acknowledged the same day, with no public confirmation of
  a fix.
- Three loader versions in six weeks, including a tool renamed mid-flight
  (`create_checkout` → `proceed_to_checkout`), with no merchant version pinning.

The pattern in the first-hand report is this product's thesis, happening to someone
else, in public: **the read path answers correctly while the buying path is
silently broken, and the site owner has no way to find out.** That is precisely the
failure a call-level evaluator cannot see, because the tool's answer is
well-formed, prompt, and wrong.

## What this feature does not claim

Stated plainly, because the temptation runs the other way.

- **No damage is claimed.** WebMCP adoption outside demos is negligible, no
  mainstream agent consumes these tools in production today, and the loader
  registers only where a model-context API already exists. Real-world exposure
  right now is near zero. What shipped, shipped; what it cost merchants so far is
  not something anyone can currently measure, and this project does not pretend to.
- **No scan of anyone's store.** ActionWitness audits one origin at a time, only
  after an operator asserts they are authorized on it, and only against a
  server-configured allowlist. There is no crawler, no discovery, no brand
  monitoring, and no code path that could become one — no module in the audit
  imports an HTTP client at all, which is checked by
  `tests/architecture/test_audit_guardrails.py`.
- **No dispute-rate or fraud statistics.** Figures circulating about agent-mediated
  transaction disputes do not survive checking against their claimed source. There
  is no credible public data, so this project quotes none.
- **No claim to replace call-level evaluation.** Storefront Witness answers "did
  the tool's claim match the store?"; it does not score tool selection or arguments.
  Run both.
- **A clean audit is not a guarantee.** It is evidence about the journey that was
  tried. The report says so in its own voice rather than in a footnote, because a
  merchant who reads a pass as a warranty stops looking.

## What an audit actually does

1. **The operator asserts an authorized origin.** One origin, supplied by a human,
   recorded against the workspace, checked against a server-controlled allowlist
   that no request body can widen. Nothing proceeds without it.
2. **The published tools are enumerated** through the browser adapter, as an
   external, untrusted surface.
3. **A contract pack is chosen explicitly** — the read-only pass or the cart pass —
   and the report names which one ran, so a reader can tell what the result covers.
   `proceed_to_checkout` and `manage_orders` are enumerated, reported as reachable,
   and never invoked: exercising them could create a real order against a real
   customer's account.
4. **Each tool's claim is checked against an independent read** of the cart, taken
   through the platform's own session API in the operator's browser rather than by
   the harness reaching out to the site. Where no independent channel exists, the
   tool is reported as *not checked* — never as passing.
5. **The report is composed for the shop owner**: which tools worked, which
   reported success while the store did not change, which failed outright, which
   were deliberately left alone, and what to do first. The engineer-grade evidence —
   the tool's exact words beside the observed cart, before and after — sits
   underneath for whoever the owner forwards it to.

## The guardrails, and why they are shaped this way

| Guardrail | How it is held |
|---|---|
| Authorized origin only | Operator assertion recorded per workspace; server-controlled allowlist; exact-origin match |
| One origin, never a list | The request model has no field that accepts a collection — a list of origins is a scan queue with a friendlier name |
| No outbound request to the audited site | No audit module imports a network client; the observation arrives from the operator's own browser, already authenticated as itself |
| Never checkout, never an order | `proceed_to_checkout` and `manage_orders` are in a never-invoked set no shipped contract pack can dispatch |
| Off unless configured | The public deployment ships with `external_audit` disabled, so an anonymous workspace cannot assert authorization for anything |

Each row is a test rather than a promise; the mapping is in
`tests/architecture/test_exit_gate_traceability.py` under `EXIT_GATE_015`.

## Sources

The Shopify developer changelog entry of 5 Aug 2026 and the independent storefront
testing published 6 Aug 2026 are the two first-hand sources behind the claims in
"Why the feature exists". Full citations, the evidence tiers they were sorted into,
and the claims this project decided not to repeat are in
`docs/actionwitness-top3-features-round2.md` §0 and §5.
