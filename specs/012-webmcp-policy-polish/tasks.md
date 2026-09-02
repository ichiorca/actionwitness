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
- [x] T6 — `getTools()` / `toolchange` reconciliation (FR-003), feeding
      `stable_tool_surface` rather than routing around it. 014 shipped the
      capture; this adds the reconciliation FR-003 asks for — harness and target
      answered separately, claimed-but-not-reported named, and an undeclared
      tool surfaced with the verdict left to the policy. The two watchers now
      share one `getTools()` call and one `toolchange` subscription, so the view
      a person reads and the evidence the server judges cannot disagree, and
      `tests/architecture/test_webmcp_adapter_isolation.py` keeps a third from
      appearing.
- [x] T7 — SSE. The condition was checked, not assumed: all eight exit gates
      plus the architecture lane were green (239 tests) and the polling
      transport's tests pass unchanged. §15.3's three requirements ship — the
      sequence as the SSE `id`, `Last-Event-ID` resume, and the paged endpoint
      retained as the default for any client that does not negotiate a stream.
      The React workspace deliberately stays on polling (plan.md D6): §15.3
      makes SSE an enhancement, and a second live transport in the UI is the
      risk this item was made conditional over. D7 records a rail test that was
      vacuous and what replaced it.
- [x] T8 — Cut hygiene for every item not shipped, verified rather than
      asserted, by extending 009-T12's gate rather than writing a second one.
      That gate covered optional *modules*; M11's cut is a fault profile, so it
      now also covers those. **The verification found a real defect** (plan.md
      D8): `checkout_without_confirmation` was recorded by the harness with a
      200 and could be armed into a run, whose report would have named an
      active fault the store never injected. Closed by having the adapter
      advertise what it can inject, refused at selection when a target can
      answer and at arming regardless of the operator's order.
- [x] T9 — The exit gate. AC-02, AC-05, AC-13 and AC-14 green for what shipped;
      AC-07 has no row **because the item was cut**, and criterion 2 carries two
      entries so that absence is a proven fact rather than a convenient silence
      — the profile is refused, never recorded, and ships no injector. Product
      copy checked. Every earlier gate green (2664 Python, 167 frontend,
      type-check, lint and build clean). The traceability map now covers 012.

**Milestone status: 5 of 7 items shipped, 1 cut, 1 scoped.** T1, T3, T4, T5 and
T6 shipped whole with their criteria. T2 is cut (D1) and verified absent. T7
shipped server-side with the workspace left on polling (D6). Nothing is
half-exposed, which is the one outcome M11 forbids outright.
