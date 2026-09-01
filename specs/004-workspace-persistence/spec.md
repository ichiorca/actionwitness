# 004 — Application persistence, workspace isolation, API foundations (M3)

**Source:** `docs/BUILD_ORDER.md` §7/M3 · functional spec v1.9 §15, §17, §20
**Goal:** make workspace state and append-only evidence safe before adding the
full journey.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M3; nothing here is invented.

## Scope (implementation areas)

**Persistence (`apps/actionwitness_service/persistence`)**

- Migrations and schema bootstrap for the Tier 1 tables first: `workspaces`,
  `contracts`, `runs`, `events`, `guidance_events`, `snapshots`, `findings`,
  `confirmation_requests`, and `artifacts` (§17.1). Tier 2 tables arrive in the
  M6/M7 migrations.
- Repository interfaces and unit-of-work boundaries implementing the core's
  `ports` protocols, with WAL, foreign keys, busy timeout, `BEGIN IMMEDIATE`,
  and deterministic event-sequence allocation (ADR-0003).
- A keyed per-workspace async lock manager with bounded cleanup. Database
  transactions remain the final serialization boundary.

**Authorization and request safety**

- Anonymous workspace middleware and opaque cookie creation: `HttpOnly`,
  `SameSite=Strict`, `Secure` in production (FR-005, §20.1).
- Every stateful UI request resolved from that cookie. A workspace ID is never
  accepted as authorization (FR-006, §20.1).
- `Origin` validation on mutations, repository and domain errors mapped to the
  one stable API envelope, and HTTP 409 for invalid transitions (§15.8, §16).

**Limits, cleanup, and availability**

- Per-IP and workspace-creation token buckets, every hard resource ceiling,
  artifact byte accounting, and stale-workspace and eval-workspace cleanup
  (FR-008, FR-009).
- Adapter registry behaviour such that a missing optional target produces a
  bounded unavailable state rather than process failure (§21.1).

**API foundations**

- `/api/v1/workspace` configuration and reset routes (§15.1), and immutable
  contract template selection and reads (§15.2).

## Acceptance criteria / exit gate

1. Two independent clients cannot read or mutate one another's state even with
   known IDs.
2. Cross-workspace run, contract, confirmation, artifact, and reset attempts
   fail.
3. Resource, rate, and lock failures leave no partial target or evidence state.
4. Reset cancels nonterminal state and unresolved confirmations while retaining
   terminal artifacts and the selected contract.
5. The service starts with Buggy Store disabled and reports the adapter as
   unavailable.

## Non-goals

- No run lifecycle, invocation, verification, guidance derivation, or
  comparison endpoints (005).
- No UI, WebMCP registration, or confirmation dialog (006).
- No eval, benchmark, or Shopify tables; they arrive with the milestones that
  own them.

## Implementation order (normative)

1. migrations and schema bootstrap → 2. repositories and the unit of work →
3. the per-workspace lock manager → 4. workspace cookie middleware and
authorization → 5. origin validation and the error envelope → 6. rate limits,
resource ceilings, and cleanup → 7. adapter registry availability →
8. `/api/v1/workspace` and contract template routes.
