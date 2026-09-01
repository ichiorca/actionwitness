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

_No entries: implementation has not begun._
