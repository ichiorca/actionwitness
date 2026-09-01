# 014 — Tool Surface Witness (§9.5, §16.1, round-2 feature #3)

**Source:** functional spec v1.9 §9.5 (delta kinds), §16.1 (`stable_tool_surface`),
§25.12 · `docs/actionwitness-top3-features-round2.md` §2/#3
**Goal:** capture, hash, and watch the browser's tool surface so a mid-run
registration change is evidence, not an invisible event. The vocabulary
(`SurfaceDeltaKind`), the policy (configurable, fails closed with no
baseline), and the *replay* half (§24.3a surface evidence) shipped in
002/007; the live capture half did not.

> **Draft.** Staged for operator rename per `specs/README.md`.

## Scope

- **Capture at arm**: hash the surface from `getTools()` — name,
  description, `readOnlyHint`, canonicalized `inputSchema` — with the
  RFC 8785 helper; persist as `tool_surface_captured` with the baseline.
- **Watch**: subscribe to `toolchange` (verified live in ADR-0002:
  fires per change, bursts uncoalesced but lossless); re-capture, compute
  deltas in the five §9.5 kinds, record `tool_surface_changed`.
- **Evaluate**: `stable_tool_surface` fails only on *undeclared* deltas —
  state-dependent registration (the 006 phase-driven tool set) is declared
  legitimate churn via the policy's configuration; everything else is
  `tool_surface_mutation`.
- **Verify before invocation**: the per-invocation identity check — the
  descriptor about to be invoked matches the captured baseline entry.
- **The demonstration**: an injected `tool_surface_poisoned` profile where a
  simulated third-party script registers a look-alike `apply_discount`
  mid-run; the cart total comes out CORRECT and the run fails on the
  side-by-side tool-definition diff. Labeled injected unsafe behaviour and
  forbidden against external targets, exactly as §13.3 treats
  `checkout_without_confirmation`.

## Acceptance criteria / exit gate

1. Arming a run persists a surface baseline whose hash is reproducible from
   the recorded definitions.
2. A mid-run registration change produces `tool_surface_changed` with the
   correct §9.5 delta kind; declared churn produces no failure.
3. `stable_tool_surface` passes on a quiet surface, fails with
   `tool_surface_mutation` on an undeclared delta, and still fails closed
   (`observation_unavailable`) when capture never ran.
4. The poisoned-profile journey fails on the definition diff while every
   business assertion passes.
5. Replay of a case carrying surface evidence reproduces the classification
   (the 007 path, now fed by real captures).
6. Full suite, architecture lane, both frontend gates green.

## Non-goals

- No external targets (the profile is demo-only, §13.3 parity).
- No signature/provenance scheme beyond hashing — that is research (MSTI
  paper's unimplemented defenses), not this milestone.
