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
- [~] T2 — **CUT** (see plan.md D1). `checkout_without_confirmation` cannot
      produce AC-07's `missing_confirmation`: the harness's confirmation gate
      and the `requires_confirmation` policy read the same contract policy, so a
      protected tool either pauses for a human and passes, or is unprotected and
      passes vacuously. No store-side fault reaches that classification. The
      profile stays unimplemented and refused by name — M11's required cut
      posture — and no test claims AC-07. Needs a specification decision, not
      more code.
- [x] T3 — Observed-trajectory edge cases; AC-13 green.
- [x] T4 — Invocation `AbortSignal` propagation; AC-14 green. A cancelled
      invocation leaves a clean refusal rather than a half-finished mutation,
      and a partially completed operation stays visible.
- [x] T5 — Flat declarative contract-instantiation form; AC-02 green for what
      the repository can reach, and open on the operator's browser run (see
      plan.md D2 and `tests/browser/ac02-registration-checklist.md`). Ships the
      whole path: per-template scalar allowlists and server-side expansion,
      `POST /contracts`, and the annotated form that is the third registration
      mechanism. D3 and D4 record where the implementation reads Appendix G as
      a shape rather than transcribing it.
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
