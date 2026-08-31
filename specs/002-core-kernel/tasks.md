# 002 — tasks

Cite the T-ID in every commit that advances it.

- [x] T1 — Shared kernel: frozen base models (`extra="forbid"`), Decimal
      money, UTC instants, injected clock/ID/randomness protocols; unit tests
      for immutability and rejection of unknown fields.
- [x] T2 — Closed enums for run/eval/benchmark states, events, severities,
      operators, policy types, source classifications (extends 001-T7
      registry); exhaustiveness tests.
- [x] T3 — Restricted dotted-path type: parse, validate, resolve exactly;
      structured errors; hostile-input tests.
- [x] T4 — RFC 8785 canonical JSON + SHA-256 hashing per ADR-0004; published
      + repository vectors green; non-finite rejection.
- [x] T5 — Redaction: default + contract-specified, applied before hash and
      persistence models; bounded summaries; tests prove redacted fields
      never reach canonical bytes.
- [x] T6 — Contract models: limits, expected-tool semantics, immutable
      records, schema export; unsafe-size and unknown-version rejection.
- [x] T7 — Ports: descriptor/selection/toolspec/context/result/observation
      models + adapter and repository protocols; tool-report vs observation
      as distinct types with no cross-construction.
- [x] T8 — Assertion operators (all eight) + severity aggregation +
      deterministic primary-failure ordering; table-driven tests incl.
      tie-breaks.
- [x] T9 — Trajectory evaluation: expected-tool multiset/subsequence checks;
      confirmation policy; every policy type evaluated or explicitly
      `not_evaluated`.
- [x] T10 — Classification: generic + causal incl.
      `false_success_or_state_mismatch`; journeys transition validation
      (pure); invalid-transition tests.
- [x] T11 — Layered report models (five layers distinct) + byte-identical
      serialization test.
- [x] T12 — In-memory non-commerce adapter fixture evaluating
      `target.ticket.status` end-to-end through public protocols only.
- [x] T13 — Core-isolation job: install only `actionwitness_core` in a clean
      venv, run its suite; wire into the architecture lane; full exit-gate
      verification.
