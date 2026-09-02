# 012 — tasks

Cite the T-ID in every commit that advances it.

**Entry condition (§7.3): the Tier 3 order is locked** — live LLM benchmark,
then the authorized external surface including the Shopify proof, then this
polish. AC-17 is unproven (010-T11) and AC-18 unattempted (011), so nothing
here starts yet.

**Every item is optional; none is partial.** BUILD_ORDER §7/M11: "Implement only
complete features, in this order." An item that ships is whole and carries its
acceptance criterion. An item that is cut has its control and tool registration
removed or visibly disabled — a greyed-out button that still registers a WebMCP
tool is the failure this milestone exists to prevent.

- [x] T1 — `duplicate_on_retry` injector plus idempotency evidence; AC-05
      green through the real boundary. First in the order because it turns
      idempotency from a property the code asserts about itself into one
      observed under a fault.
- [ ] T2 — `checkout_without_confirmation` injector; AC-07 green. The consent
      rail, observed rather than assumed.
- [ ] T3 — Observed-trajectory edge cases; AC-13 green.
- [ ] T4 — Invocation `AbortSignal` propagation; AC-14 green. A cancelled
      invocation leaves a clean refusal rather than a half-finished mutation,
      and a partially completed operation stays visible.
- [ ] T5 — Flat declarative contract-instantiation form; AC-02 green.
- [ ] T6 — `getTools()` / `toolchange` reconciliation, feeding
      `stable_tool_surface` rather than routing around it. Reconciliation that
      silently accepted a changed surface would undermine the policy 007's case
      format already carries.
- [ ] T7 — SSE, **only if** polling is already stable and every earlier gate is
      green. A second transport brings its own reconnection and stale-response
      semantics; adding it while anything earlier is red means debugging two
      timelines at once.
- [ ] T8 — Cut hygiene for every item not shipped: control and tool
      registration removed or visibly disabled, verified rather than asserted.
      Extend 009-T12's existing gate rather than writing a second one.
- [ ] T9 — The exit gate: the acceptance criteria of shipped items green
      (AC-02, AC-05, AC-07, AC-13, AC-14 **for what ships**); no partially
      implemented feature exposed; product copy claiming nothing unshipped
      (constitution §8); every earlier gate still green. Extend the
      architecture lane's traceability map to 012.
