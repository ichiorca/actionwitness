# 012 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

## Blocked, and differently from 011

§7.3 locks the Tier 3 order: live LLM benchmark, then the authorized external
surface including the Shopify proof, then this. AC-17 is unproven (010-T11) and
AC-18 has not been attempted (011), so nothing here starts yet.

But the block is softer than 011's, and it is worth being precise about why.
BUILD_ORDER states no hard "do not start" for M11 the way §7/M9 does for Shopify
work; what it fixes is *priority*. So the risk here is not a forbidden action —
it is the ordinary one of spending the last of the budget on polish while a
release gate is open.

## What makes this milestone different

**Every item is optional and each one is a whole.** BUILD_ORDER: "Implement only
complete features, in this order." The seven items are independent, so the
milestone can ship one, four, or none — and each shipped item carries its own
acceptance criterion. There is no partial credit and no "mostly working".

**The dangerous outcome is a live control over an unfinished feature.** This is
the only milestone whose central rule is about what happens when work is *cut*:
"remove or visibly disable its control and tool registration. Do not expose a
partially implemented feature." A greyed-out button that still registers a
WebMCP tool is exactly the failure — an agent discovers a capability the product
cannot honour, and the agent has no way to tell that from a bug. 009-T12 already
built cut hygiene as a gate; this milestone is the one that will actually
exercise it.

**Two items are fault injectors, and they make earlier claims testable.**
`duplicate_on_retry` (AC-05) and `checkout_without_confirmation` (AC-07) are how
idempotency and consent stop being properties the code asserts about itself and
become properties observed under a fault. They come first in the order for that
reason, not because they are easiest.

**Cancellation is a correctness property, not a nicety.** AC-14's `AbortSignal`
propagation decides whether a cancelled invocation leaves a half-finished
mutation or a clean refusal. The constitution already requires cancellation to
propagate through I/O and partially completed operations to stay visible; this
item is where that is proved at the invocation boundary.

**SSE is last and conditional for a stated reason.** "Only if polling is already
stable and all earlier gates remain green." Polling is the Tier 1 transport and
has passing tests; SSE is a second transport with its own reconnection and
stale-response semantics. Adding it while anything earlier is red would mean
debugging two timelines at once.

**`toolchange` reconciliation touches the surface the product witnesses.**
Item 6 sits close to the `tool_surface_poisoned` machinery 007 already carries
in its case format. Reconciliation that silently accepted a changed surface
would undermine `stable_tool_surface`; whatever ships here must feed that policy
rather than route around it.

---

1. **`duplicate_on_retry` injector** plus idempotency evidence, and AC-05.
2. **`checkout_without_confirmation` injector**, and AC-07.
3. **Observed-trajectory edge cases**, and AC-13.
4. **Invocation `AbortSignal` propagation**, and AC-14.
5. **Flat declarative contract-instantiation form**, and AC-02.
6. **`getTools()` / `toolchange` reconciliation**, feeding `stable_tool_surface`
   rather than bypassing it.
7. **SSE**, only on the stated condition.
8. **Cut hygiene for everything not shipped**, verified rather than asserted.
9. **The exit gate**: the criteria of shipped items green, nothing partial
   exposed, product copy claiming nothing unshipped, and the traceability map
   extended to 012.

---

## Carried forward

- **Cut hygiene already has a gate.** 009-T12 built it; this milestone should
  extend it rather than write a second one.
- **AC-05 and AC-07 have long-standing homes.** The Buggy Store already
  publishes `idempotent_by_request_id` retry semantics and a confirmation
  policy; these injectors exercise the existing contracts rather than
  introducing new ones.
- **§26.2 asks for one integration test per shipped item**, through the real
  boundary — not a unit test of the injector in isolation.

---

## Deviations ledger (implementation)

Each departure from the spec, anchored to the section it departs from, with what
was taken and why.

### D1 — AC-07 is unreachable through a store-side injector (T2 cut)

**FR-060/§13.3/AC-07.** AC-07 asks that "when an order is created without
approval, verification reports `missing_confirmation`". That classification is
produced only by `requires_confirmation`, which flags a *successful completion of
the protected tool with no approval event preceding it on the same correlation*.

The harness's confirmation gate and that policy read **the same source**:
`invocation_service` calls `confirmation_requirement(document, tool_name)`, and
`_evaluate_requires_confirmation` reads the same contract policy. So:

- if the armed contract protects `proceed_to_checkout`, the harness pauses for a
  human, records an approval, and the policy passes — the store is never reached
  without consent, and the adapter's own bridge refuses anyway
  (`MissingHumanConsent`);
- if it does not protect it, `_starts_for(policy.tool)` finds no attempts and
  FR-060 passes *vacuously*.

Neither branch can produce `missing_confirmation`, and no behaviour injected into
the Buggy Store changes that: a fault that created the order during some other
tool leaves `proceed_to_checkout` unattempted, and one that created it at
confirmation-request time fails the `order-not-created` assertion as
`assertion_mismatch` instead.

**Taken: T2 is cut, not faked.** `checkout_without_confirmation` stays
unimplemented and is still refused by name with its description, which is
already M11's required cut posture — the control is visibly disabled rather than
half-built. No test claims AC-07.

**This is arguably the product working.** The harness *prevents* the unsafe
checkout rather than merely detecting it, and §23.1's `blocked_safely` exists to
report exactly that. AC-07 appears to assume a path where the protected tool can
complete unconsented, which this design does not have.

**What would settle it, for the operator.** Either (a) AC-07 is satisfied by the
harness blocking the action, and the criterion should be restated in those terms;
or (b) the demonstration wants a target reached *outside* the harness gate — an
external caller hitting the store's `/checkout` directly — which is a different
journey than the one FR-060 evaluates and needs its own contract. Both are
specification decisions rather than implementation choices.

### D2 — AC-02's browser half is a checklist, not a test (T5)

**AC-02/§25.2.** AC-02 reads "when inspected in Chrome DevTools, then the
expected native, imperative, and declarative tools are visible with valid
schemas". The automated frontend suite runs in jsdom, which has no WebMCP
implementation — and the declarative mechanism has *nothing to intercept*: a
tool exists because the browser parsed `toolname` off the markup, not because
anything called `registerTool`. No test in this repository can assert that a
browser registered it.

**Taken: the testable half is tested and the rest is a checklist.**
`src/components/contractForm.test.tsx` pins every annotation §25.2 requires,
`preventDefault`, one submit path shared by agent and human, `respondWith`,
`toolactivated`/`toolcancel`, and the disabled-not-removed treatment of an
unallowlisted control. `tests/browser/ac02-registration-checklist.md` carries
the DevTools run, in the same form and for the same reason as ADR-0002's spike
checklist (§26.4 makes WebMCP browser checks manual; §7.5 forbids letting a
flagged browser gate the suite).

**AC-02 is therefore green for what the repository can reach and open on the
operator's run.** Recorded rather than claimed: the milestone's rule is that a
shipped item carries its acceptance criterion, and half of this one needs a
person at a browser.

### D3 — the appendix's template identifiers are illustrative (T5)

**Appendix G.** The `create_outcome_contract` schema in Appendix G enumerates
`save20_no_checkout`, `idempotent_cart_retry`, `confirmed_checkout`, and
`shopify_exact_cart`. None of those is a template this build publishes; the real
identifiers are `one_mug_save20_no_checkout`, `retry_safe_cart_update`,
`confirmed_checkout_only`, `one_mug_no_side_effects`, and (from 014)
`one_mug_stable_surface`.

**Taken: the enum is derived from the published registry, not transcribed.**
Hard-coding the appendix's names would offer an agent four templates the server
cannot expand and hide the five it can. The appendix is read as a shape — one
`template_id` chosen from a closed published set, plus three optional scalars —
and that shape is implemented exactly. No behaviour differs; the names do.

### D6 — SSE ships server-side; the workspace stays on polling (T7)

**§15.3 / §7.3.** The entry condition was checked rather than assumed: all eight
exit gates plus the architecture lane were green (239 tests) before any of this
was written, and the polling transport's own tests pass unchanged.

§15.3's normative sentence is about the API — "use the event sequence as the SSE
`id`, honor `Last-Event-ID`, and retain the paged endpoint as fallback" — and
all three ship with tests. What is **not** done is switching the React workspace
onto `EventSource`.

**Taken deliberately, for two reasons.** §15.3 makes polling normative and SSE
an enhancement, so a client that never negotiates a stream cannot tell this
shipped; nothing is half-exposed. And the plan's own warning for this item is
that SSE is "a second transport with its own reconnection and stale-response
semantics" — putting one into the workspace at the end of the milestone, when
the timeline view is stable and tested, buys nothing the requirement asks for.

There is also a testing argument. jsdom has no `EventSource`, so a client-side
suite would be a double exercising a double, while resume is exactly what *can*
be tested honestly through the real ASGI app — which is where it now is.

**If the operator wants the workspace switched, that is a follow-up**, and the
server contract it would consume is complete and covered.

### D7 — a rail test was vacuous, and the mutation proved it (T7)

**Constitution §17 / §6.** The first version of the SSE rail test asserted the
obvious form of "no transaction across SSE delivery": open a stream, leave it
suspended, write to the database, require the write to succeed.

It passed against an implementation **deliberately broken** to hold its unit of
work across every yield. `Database.reading()` opens a fresh connection and
issues no `BEGIN`; under WAL a reader does not block a writer, so there was no
writer to starve and nothing for the assertion to detect. The test would have
sat in the suite looking like proof of a rail it could not see.

**Taken: the test was replaced with one that measures the hazard that is real
here** — connections, one per open stream, held as long as a tab stays open with
nothing bounding how many tabs there are (§21).
`test_no_unit_of_work_is_held_across_a_yield` counts open units of work at the
point the generator suspends. It was re-run against the same mutation and
fails, so its teeth are demonstrated rather than assumed.

The module docstring was corrected too: it had asserted the writer-starvation
mechanism as fact, and a comment that explains a mechanism which does not exist
is worse than no comment.

### D5 — two `getTools()` readers existed, and were merged (T6)

**Constitution §1 / §25.1 / FR-003.** 014 built the surface capture with its own
`getTools()` call and its own `toolchange` listener in `webmcp/surface.ts`,
while `webmcp/adapter.ts` already had a second pair for the registration view.
Both were reasonable in isolation. Together they were the exact failure T6 is
named against: the page could show "all registered" from one read while the
evidence recorded something else from another, and a person would have no way to
know which was true.

**Taken: one read, one subscription, both in the adapter.** `describeTool`,
`readSurface`, and a new `subscribeToToolChange` moved to `adapter.ts` — the
home the constitution names — and `surface.ts` now consumes them. The witness's
debounce, in-flight guard, and stale-drop semantics are untouched; its 14 tests
pass unchanged.

**A gate now holds it.** `tests/architecture/test_webmcp_adapter_isolation.py`
confines direct WebMCP access to the adapter and asserts `getTools()` has
exactly one caller. It scans code with comments stripped, because these modules
explain the browser API constantly and a raw-text scan would flag the
explanation as the violation — which teaches people to delete the explanation.

Two exceptions are allowlisted **by name, with reasons**, rather than left
tacit: `integrations/buggyStore/poisoned.ts` (§13.3's injected fault
impersonates a *hostile* registrar, and a hostile page would not use this app's
adapter — routing it through would make the injected attack weaker than the real
one it stands in for) and `spike/**` (ADR-0002's decision harness, a separate
Vite entry point deliberately outside the product surface). A third test fails
if an allowlisted path stops existing, so a stale entry cannot silently
re-permit whatever later takes that path.

### D4 — `contract_name` is bounded twice, at different limits (T5)

**Appendix G / §20.2.** Appendix G bounds `contract_name` at 80 characters, which
is also the core's `MAX_NAME_LENGTH`. The request model bounds it at 200 instead,
and the expansion enforces the 80.

Deliberate: the two bounds answer different questions. 200 is the *safety* bound
— nothing unbounded may reach a column, a log line, or an error message — and 80
is the *domain* bound, which belongs with the template that knows what a contract
name is. Enforcing only the safety bound at the boundary means an 81-character
name is refused by the expansion with a field-level message naming
`contract_name`, rather than by FastAPI with a message about string length. Both
arrive as the same `CONTRACT_VALIDATION_FAILED` envelope, so a client parses one
shape either way.
