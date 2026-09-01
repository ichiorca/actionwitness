# 015 — plan

Round-2 costing: ~1–1.5 days net, "most of it already budgeted for Tier 3
#2." The engineering it reuses: the external-target ports (002), the
capability/module reporting (009-T12), the config flags (001-T8), cart
normalization patterns (003), and — when 014 lands first — surface capture.

1. **Module enablement**: `external_audit` goes from config-only to a
   registered module: origin assertion recorded per workspace, allowed
   origins server-controlled, exact-origin comparison (the Tier 1 gate's
   origin lessons apply verbatim).
2. **External target adapter**: implements the public adapter protocol with
   the honest observation story — Shopify-shaped origins get the `cart.js`
   session read; anything else gets `observation_unavailable` unless §12.17
   names a channel. A tool's self-report is never promoted to observation
   (constitution §5; this spec exists because of that rule).
3. **Contract pack**: ten contracts as data, not code; the two consequential
   tools (`proceed_to_checkout`, `manage_orders`) are present-but-never-
   invoked entries — asserting their existence and schemas without ever
   dispatching them.
4. **Fixture page**: a local page registering a deliberately half-broken
   surface (read tools work; `update_cart` reports success and mutates
   nothing) — the Allbirds-shaped failure, reproduced in a fixture we own.
   This is also the e2e test bed; no external network in any required lane.
5. **Merchant report**: a second rendering of the existing layered report —
   same data, §5 persona language, consequences first, evidence links for
   the engineer who follows.

Sequencing and dependencies:

- Independent of AC-17/011. Benefits from 014 (surface capture) but works
  without it (discovery via a plain `getTools()` snapshot).
- **Timing**: post-submission for the build; the *framing* (round-2 §0's
  Shopify story, cited as third-party reporting) can enter the README and
  demo narration now at zero code cost.

Risks: scope creep toward scanning (the guardrails section is the contract);
the never-invoked entries being "helpfully" exercised by a later session
(their contracts carry no invocation and the pack test asserts that);
Shopify's surface renaming again mid-flight (the pack pins the names as of
the Aug 5 changelog and reports absences as absences, not failures).

## Deviations and decisions worth an operator's eye

_Per-task, anchored to spec sections._
