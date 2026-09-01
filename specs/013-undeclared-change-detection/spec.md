# 013 — Undeclared-change detection (FR-157, §9.10, round-2 feature #2)

**Source:** functional spec v1.9 §9.10, FR-157, §22–23 · `docs/actionwitness-top3-features-round2.md` §2/#2
**Goal:** make the `no_undeclared_changes` policy actually evaluate. Today an
agent that correctly performs the contracted task — and *also* mutates state
the contract never mentioned — passes with every critical assertion green;
the policy honestly reports `not_evaluated` because no milestone delivered
the changed-path computation.

> **Draft.** Staged for operator rename per `specs/README.md`. This spec
> completes existing v1.9 requirements; nothing here is off-spec.

## Scope

- **Canonical recursive diff** over the existing before/after snapshots:
  deterministic changed-path set (added / removed / value-changed), built on
  the RFC 8785 canonicalization already mandated, ordered deterministically.
- **Declared/undeclared partition (§9.10)**: a changed path is *declared*
  when covered by an assertion path, inside the declared tool-effect prefix
  of a tool that actually ran, or matched by the policy's `allow_paths`
  escape hatch; otherwise *undeclared*.
- **Policy evaluation**: `no_undeclared_changes` fails on any undeclared
  path; `not_evaluated` remains only for the genuinely missing-snapshot
  case. Classification `undeclared_state_change` and the already-modelled
  `undeclared_changes` report block are populated, never invented.
- **Replay parity**: an eval case carrying the policy replays with the same
  partition (007's minimizer already preserves complete canonical state for
  exactly this policy).
- **The demonstration**: the Buggy Store's third Tier 1 failure profile,
  `undeclared_side_effect` — currently recognised and refused — gets its
  injector, its contract template (closing 003's deliberate third-template
  gap), and an acceptance test in which every named assertion passes and the
  run still fails on the side effect.

## Acceptance criteria / exit gate

1. Same snapshots → byte-identical changed-path set and partition.
2. A run whose only defect is an unnamed-path mutation fails with
   `undeclared_state_change`; the report names the paths; the UI shows a
   "changed outside contract" panel.
3. `allow_paths` admits declared churn without widening anything else.
4. A generated eval case reproduces the same classification in replay.
5. The `undeclared_side_effect` template ships, and the template-honesty
   test that forbade claiming an uninjectable profile now covers three.
6. Full suite, architecture lane, both frontend gates green.

## Non-goals

- No tool-surface concerns (014). No external targets (015).
