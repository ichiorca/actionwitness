# 005 — Complete the Tier 1 outcome-run vertical slice (M4)

**Source:** `docs/BUILD_ORDER.md` §7/M4 · functional spec v1.9 §6, §12, §16, §22–23
**Goal:** make Journey A work through FastAPI before adding browser-agent polish.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M4; nothing here is invented.

## Scope (implementation areas)

**Arming**

- Arm as one transaction: authorize, validate immutable configuration, capture
  one authoritative initial observation, validate preconditions, create
  run/snapshot/events, and derive guidance.

**Invocation**

- Generic target-tool invocation with Python validation, run-state checks, event
  cap reservation, start event, adapter dispatch, immediate authoritative effect
  observation, and exactly one terminal event.
- Persist redacted inputs, bounded output, status, timing, request/correlation
  IDs, state versions/hashes, and effect-path evidence.
- Enforce the exclusive run mutation lease and the atomic verification race gate.

**Verification and classification**

- Final observation capture, assertion/trajectory/policy evaluation, findings,
  layered report, and immutable terminal transition.
- The false-success classifier, using the last relevant intended-effect action
  and its immediate authoritative post-call evidence.

**Guidance and comparison**

- Server-derived `GuidanceState`, append-only guidance events, and the same
  compact `next_action` projection returned by tools.
- Scenario switch/reset and matched `pre_fix`/`post_fix` comparison using
  controlled-input hashes and actual trajectory identity.

**API surface**

- Paged events and report/comparison endpoints.

## Acceptance criteria / exit gate

1. API-level Journey A fails with `false_success_or_state_mismatch` in `pre_fix`
   and passes in `post_fix`.
2. The report shows observed trajectory pass, execution pass, business outcome
   fail, and model selection `not_evaluated` for the source run.
3. New target actions lose cleanly to verification with `RUN_ALREADY_VERIFYING`
   and no partial snapshot.
4. A mismatched rerun remains valid but returns `not_comparable` with the
   differing fields.
5. AC-03, AC-04, AC-11, AC-19, and the API portion of AC-20 pass in automated
   integration tests.

## Non-goals

- No UI, WebMCP registration, or confirmation dialog (006).
- No eval case generation, fixture replay, or CLI exit codes (007).
- No evaluator report import, binding, or benchmark matrix (008).
- No Shopify pairing, theme bridge, or `target.cart` observation (011).

## Implementation order (normative)

1. arm as one transaction → 2. generic target-tool invocation → 3. invocation
evidence persistence → 4. the exclusive run mutation lease and the verification
race gate → 5. final observation, evaluation, findings, and the layered report →
6. the false-success classifier → 7. server-derived guidance and `next_action` →
8. scenario switch/reset and matched comparison → 9. paged events and
report/comparison endpoints.
