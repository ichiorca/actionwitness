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
- **No one-click audit.** The workspace now walks the pass — assert the origin,
  choose the pack, submit the transcript, read the sealed report — but the
  collection itself stays in the operator's hands: the app generates a collector
  script, and the operator runs it in the storefront's own console, in their own
  session, filling in the tool arguments only they can know. See the status note
  at the end of the next section — the console step still asks for a technical
  operator, and saying otherwise would be the same kind of confident-and-wrong
  the feature exists to catch.

## What an audit actually does

Each step below is a call a client makes, in order, against `/api/v1/audits`.

1. **A contract pack is offered.** `GET /audits/packs` returns the built-in packs
   as a static catalogue — the read-only pass and the cart pass — each carrying the
   tools a surface must publish for the pack to apply and the tools the pack will
   never invoke. It is a catalogue, not a query: nothing is submitted, so there is
   no request here that names an origin or a tool list, and nothing for a scanner
   to use.
2. **The operator asserts an authorized origin.** `POST /audits`. One origin,
   supplied by a human, recorded against the workspace, checked against a
   server-controlled allowlist that no request body can widen. Nothing proceeds
   without it.
3. **The published tools are enumerated** in the operator's own browser, on the
   storefront itself, as an external, untrusted surface.
4. **The pack is selected explicitly.** The submission names it, and the server
   re-checks that the enumerated surface actually satisfies it — a cart pack sent
   against a surface with no cart tool is refused as a selection error rather than
   run, because run anyway it would report the cart tool "absent" and read as a
   finding about the storefront. Nothing picks a pack on the operator's behalf:
   choosing would decide, against a store somebody depends on, whether a write path
   gets exercised. `proceed_to_checkout` and `manage_orders` are enumerated,
   reported as reachable, and never invoked — exercising them could create a real
   order against a real customer's account.
5. **The transcript is submitted and judged.** `POST /audits/current/evidence`
   carries what the browser saw: the enumeration, what each exercised tool
   *claimed*, and the raw cart reads before and after. Every field is untrusted.
   The body is size-capped before it is parsed, because the cart payload is the one
   part of the request the audited storefront controls rather than the operator.
   Each tool's claim is then checked against the independent read, taken through
   the platform's own session API in the operator's browser rather than by the
   harness reaching out to the site. Where no independent channel exists the tool is
   reported as *not checked* — never as passing; where the read arrives malformed
   the submission is refused outright, because a broken read and an absent channel
   are different facts and collapsing the first into the second would report a
   storefront as unobservable when the submission was simply wrong.
6. **The report is composed for the shop owner and sealed**: which tools worked,
   which reported success while the store did not change, which failed outright,
   which were deliberately left alone, and what to do first. The engineer-grade
   evidence — the tool's exact words beside the observed cart, before and after —
   sits underneath for whoever the owner forwards it to. The composed report is
   written as an immutable artifact, hashed, and recorded in the same transaction
   that marks the audit complete, so the workspace never holds a finished audit
   pointing at a report that is not there.
7. **The report is read back verified, not recomposed.** `GET
   /audits/current/report` serves the stored bytes after checking them against the
   hash that was recorded — readable, decodable, the same document, and the same
   canonical bytes. A report that fails any of those is **refused rather than
   served with a caveat**, and the refusal names neither the file nor the hash,
   which together are what somebody would need to forge a replacement. This is the
   document an owner is most likely to forward to somebody else, which is the worst
   possible place to make an exception to the rule that an integrity failure is an
   explicit non-pass.
8. **The audit ends.** `completed` on submission, or `cancelled` through `POST
   /audits/current/cancel` for one begun against the wrong origin. Both are
   terminal, and terminal is what frees the workspace's single live-audit slot for
   the next audit. A cancelled audit is not resumed — it is re-authorized, so
   nothing can quietly continue against an origin whose assertion was withdrawn.

**Status: the server path is complete, and the workspace now carries the
operator-facing half.** Every step above is reachable over HTTP and covered end to
end by `tests/integration/test_external_audit_pass.py`, which drives the API and
imports no application module. The workspace's audit view asserts the origin,
offers the packs, submits the transcript, and reads the sealed report back. The
enumeration, the tool exercise, and the `cart.js` read in steps 3 through 5 still
happen on the storefront itself: the app generates a collector script from the
chosen pack — with the never-invoked tools baked in rather than left to whoever
pastes it — and the operator runs it in the storefront's console in their own
session, supplying the tool arguments only they can know. A page can enumerate
only its own tools and read only its own cart, so the pasted snippet is not a
missing convenience; it is the trust boundary, stated here rather than left for a
reader to infer from a screenshot.

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
