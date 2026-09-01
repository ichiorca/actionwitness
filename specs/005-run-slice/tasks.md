# 005 — tasks

Cite the T-ID in every commit that advances it.

- [x] T1 — Arm as one transaction: authorize the workspace, validate the
      immutable run configuration (FR-012), capture one authoritative initial
      observation, validate preconditions, and create the run, the `before`
      snapshot, and the arming events together. A run that exists without its
      baseline snapshot must not be reachable.
- [x] T2 — Derive guidance at arming and append the first guidance events, using
      the server-derived `GuidanceState` rather than a client-supplied one.
- [x] T3 — Generic target-tool invocation: Python validation against the
      adapter's published schema, run-state checks, event-cap reservation
      (FR-008), the start event, adapter dispatch, immediate authoritative effect
      observation, and exactly one terminal event. No branch on a tool name.
- [x] T4 — Persist invocation evidence: redacted inputs, bounded output, status,
      timing, request and correlation IDs, state versions and hashes, and
      effect-path evidence. Redaction happens before persistence, hashing, or
      export (§20.3).
- [x] T5 — The exclusive run mutation lease and the atomic verification race
      gate: a target action arriving after verification begins loses cleanly with
      `RUN_ALREADY_VERIFYING` and leaves no partial snapshot.
- [x] T6 — Final observation capture and verification: assertion, trajectory, and
      policy evaluation through the core, findings persisted, and the immutable
      terminal transition. The core owns the verdict; this task owns the I/O.
- [x] T7 — The layered outcome report (§23): observed trajectory, execution,
      business outcome, and model selection, composed through the core's
      `compose_outcome_report`.
- [x] T8 — The false-success classifier (§12, §22): the last relevant
      intended-effect action and its immediate authoritative post-call evidence.
      An adapter that declares no effect map loses causal attribution and nothing
      else.
- [x] T9 — Server-derived guidance through the run: `GuidanceState`, append-only
      guidance events, and the compact `next_action` projection, replacing 004's
      deliberately minimal placeholder rather than sitting beside it.
- [x] T10 — Scenario switch and reset through the adapter, reseeding managed
      target state where supported (FR-013).
- [x] T11 — Matched `pre_fix`/`post_fix` comparison using controlled-input hashes
      and actual trajectory identity. A mismatched rerun stays valid and returns
      `not_comparable` naming the differing fields. (Took `GET
      /runs/{run_id}/comparison` from T12: `not_comparable` is untestable
      without a surface to return it on — see the plan's ledger.)
- [x] T12 — Paged events (§15.3, `after_sequence` and `limit`) and the report
      endpoint. (The comparison endpoint landed in T11.)
- [x] T13 — Verify the full exit gate: Journey A fails with
      `false_success_or_state_mismatch` in `pre_fix` and passes in `post_fix`;
      the report shows trajectory pass, execution pass, business outcome fail,
      and model selection `not_evaluated`; a late action loses with
      `RUN_ALREADY_VERIFYING` and no partial snapshot; a mismatched rerun returns
      `not_comparable`; AC-03, AC-04, AC-11, AC-19, and the API portion of AC-20
      pass. Extend the architecture lane's exit-gate traceability map to 005.
