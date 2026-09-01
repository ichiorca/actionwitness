# 010 — tasks

Cite the T-ID in every commit that advances it.

**Entry condition:** M6, M7, and AC-16 green (BUILD_ORDER §7/M9).

- [x] T1 — The configured live backend: one explicitly configured LLM backend
      behind a pinned `webmcp-evals` configuration, resolved as a module like
      every other. Absent configuration disables the live path and leaves the
      Tier 2 import path fully working (FR-096).
- [x] T2 — Credential handling: supplied only through a developer environment
      or deployment secret, retained only in the evaluator process
      environment. Never through the browser, a WebMCP argument, a committed
      file, or an uploaded benchmark manifest — one test per prohibited place.
- [x] T3 — Intent generation: from one canonical contract intent, up to six
      paraphrased, ambiguous, and adversarial variants, with Python
      schema-validating length and character limits.
- [x] T4 — Variant screening: reject any variant containing secrets or
      instructions to bypass confirmation, before a human is asked to review.
      A model-authored variant is untrusted text, never an instruction.
- [x] T5 — Explicit human approval, recorded, and prior to freezing.
- [x] T6 — Freeze approved variants into the content-hashed benchmark manifest
      before trials begin; generation is not rerun between repetitions.
- [ ] T7 — Live trials: at least three scenarios with at least three completed
      live trials each, imported through the **unchanged** M7 path. A change
      needed here is a finding about 008, not a patch.
- [ ] T8 — Parameter capture: persist exported model and evaluator parameters
      exactly; unsupported values remain `null`, never inferred.
- [ ] T9 — The `live_model_run` artifact: persisted as an immutable benchmark
      source, finalized and precomputed before the demo is recorded, and never
      interchangeable with a fixture-backed suite.
- [ ] T10 — Offline fallback: the checked-in fixture keeps the matrix UI and
      deterministic verification reproducible with no credential, quota, or
      network, and stays labeled `recorded_fixture` — including on the demo
      path where a live run has just failed.
- [ ] T11 — (operator gate) Execute the live run against the configured
      backend with a real credential, review and approve the variants, and
      retain the resulting artifact.
- [ ] T12 — The exit gate: the suite is labeled `live_model_run`; exported
      parameters are recorded without invention; each eligible trial binds
      exactly and produces the dual-layer matrix and silent-outcome-defect
      evidence; the credential is confined to the evaluator process; the CI
      fixture path still passes labeled `recorded_fixture`; AC-17 passes.
      Extend the architecture lane's traceability map to 010. **If AC-17 does
      not pass, Shopify work (011/M10) does not start.**
