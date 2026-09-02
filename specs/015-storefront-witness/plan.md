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

**T1 — the authorization is recorded on the audit row, not on the run
timeline (§22, FR-110).** `events.run_id` is `NOT NULL`, and an operator
asserts an authorized origin *before* any run exists — the assertion is the
thing that lets a run be created at all. Recording it as a timeline event
would have meant either inventing a run to hang it on or relaxing the
column. Neither is acceptable, so the assertion lives on the
`external_audits` row (with a partial unique index enforcing one live audit
per workspace) and `append_authorization_event` writes the timeline entry
once a run exists to carry it. The provenance is not lost; it is recorded in
the only place that can hold it before a run.

**T2 — the external adapter implements observation only (§12.17, §21.1).**
`ExternalAuditAdapter` has no `execute` and no `prepare`. The 002 target
protocol assumes a target the harness drives; §12.17's audit reads a surface
the harness must never drive, and the tools are invoked by the operator's
browser rather than by us. Giving it a stub `execute` would have put a
dispatch path into the one module that must not have one. It advertises
`supported_scenario_modes=("external_current",)` and injects no fault, so no
scenario can arm it.

**T2 — `normalize()` refuses a payload that looks like a tool response.**
Not asked for by the spec. `_SELF_REPORT_MARKERS` rejects an observation
carrying `status`, `isError`, `result` and friends, because the single
failure this whole spec exists to catch is a self-report being promoted to
an observation (constitution §5), and the audit path is the one place where
both shapes are in scope at the same moment. Fails closed rather than
normalizing.

**T3 — the cart assertion uses `changed_by: 1`, not `changed`.** There is no
`changed` operator in the assertion vocabulary (equals, not_equals, exists,
absent, contains, unchanged, changed_by, count_equals). The cart pass
therefore asserts the item count changed by exactly one, which is a stronger
statement than "changed" and is what the journey actually does.

**T5 — `SUMMARY_FORBIDDEN_WORDS` bans "critical", which is also an ordinary
English word.** Deliberately over-restrictive: it is a harness
classification term, and a merchant summary that used it would read as one.
The cost is that the summary cannot say "critical" in its plain sense
either; the copy says "fix this first" instead, which is more useful anyway.

**T7 — three tests were numbered against the wrong criterion.** The
FR-161/FR-162 checks (the report names its pack; no pack can dispatch a
consequential tool; every outcome has merchant copy) were written under a
"criterion 5" heading. Criterion 5 is "full suite, architecture lane, both
frontend gates green". They are now numbered under criteria 2, 3 and 4,
where they actually belong, and criterion 5 follows 013's and 014's
precedent of being discharged by running the lanes.

**T7 — `EXIT_GATE_013` and `EXIT_GATE_014` were never registered in `MAPS`.**
Found while adding 015's map. Both dictionaries were defined and neither was
in `MAPS`, so the traceability gate has been parametrizing over 003–010 only
and 013's and 014's criteria have not been checked since they landed. Both
are now registered, with their published-criteria counts, and both pass
unchanged — no criterion was actually uncovered. Flagged because the gate
was silently narrower than it looked for two milestones, which is precisely
the failure the gate exists to prevent.

**T7 — the product copy lives in docs, not in the UI.** `docs/storefront-
witness.md` plus a README section, held by
`tests/architecture/test_product_copy_claims.py`. The merchant-facing text
that ships *inside* the product is the report itself, and that is already
enforced word by word in `audit_report.py`. A third copy of the same claims
in the React bundle would be a third place for them to drift.

**Not a deviation, recorded because it looks like one: 015 does not use
014's surface capture.** The plan allows either; discovery here is a plain
enumerated tool list. 014's baseline machinery is about detecting a surface
changing *mid-run*, which an external audit of somebody else's store has no
authority to arm.
