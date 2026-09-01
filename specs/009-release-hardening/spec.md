# 009 — Release hardening, deployment, and submission readiness (M8)

**Source:** `docs/BUILD_ORDER.md` §7/M8, §8–§10, §12 · functional spec v1.9 §29, §26, §28
**Goal:** promote the same tested artifact to the live environment and make it
operable: one Docker image behind one origin, CI that a fresh checkout passes,
a README a judge can follow in 60 seconds, and a rehearsed rollback.

> **Draft.** `spec.md` is operator-owned once approved (`specs/README.md`), so
> this is staged as `spec.md.draft` for the operator to review and rename.
> Transcribed from BUILD_ORDER §7/M8; pre-drafted in parallel with 007 by
> operator instruction (2026-09-01) — the deploy audit that grounds the plan
> is in plan.md.

## Scope (implementation areas)

- **Image** — multi-stage Docker build: both frontends built independently
  (`npm ci && npm run build`, lockfiles committed since 006/003), Python
  distributions installed separately via `uv sync --frozen --no-dev`, schema
  and seed data initialized on startup, one service instance and one Uvicorn
  worker (SQLite, LD-11), platform port bound.
- **Single origin** — mount `/`, `/api/v1`, `/demo`, and `/demo/api/v1`
  behind one origin **without creating a direct service dependency** on the
  Buggy Store: the store stays independently runnable and is reached only
  through its versioned HTTP boundary (§25.11).
- **Operability** — structured logs carrying identifiers, status, duration,
  and classification only; never payloads, secrets, or credentials.
- **Production security** — `Secure` cookie in production, origin policy from
  `HARNESS_PUBLIC_ORIGIN`, `Permissions-Policy`, CORS, trusted-proxy
  handling, quotas and cleanup verified in the deployed environment.
- **CI** — jobs for architecture/core-only/store-only, Python unit and
  integration, both frontend test+build pairs, eval CLI fixtures, secret and
  dependency review, the Docker build, and a deployed health smoke.
- **README (§29.2)** — commands, architecture diagram, 60-second judge path,
  core-only install, standalone target run, browser setup, security notes,
  adapter skeleton, eval schema/CLI, evaluator fixture import, known limits,
  screenshots, license attribution.
- **Release discipline** — build the artifact once, deploy that exact
  artifact, verify `/healthz`, rehearse rollback, retain the previous deploy.
- **Cut hygiene** — every Tier 3 feature that is cut has its control and tool
  registration removed or visibly disabled, never shipped partial (M11 rule).

## Acceptance criteria / exit gate

1. A fresh checkout follows the README successfully (AC-10, via a CI job that
   executes the documented commands).
2. The exact image tested in staging is the image deployed to production;
   rollback is rehearsed and the previous deploy retained.
3. The live URL loads without credentials and reports WebMCP support status
   (AC-01 — closes the row deferred from the Tier 1 gate checklist).
4. The live URL passes Tier 1 and Tier 2 manual acceptance without any
   third-party credential; screenshots/video evidence saved.
5. Health and readiness signals are visible; database changes are
   forward-compatible.
6. Release artifacts contain no secrets, local paths, private fixtures, or
   build debris (constitution §8).

## Non-goals

- No Tier 3 feature work (010–012 have their own specs and entry conditions).
- No multi-instance scaling, external database, or hosted tenancy.
