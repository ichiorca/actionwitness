# 009 — plan

## Deploy audit (2026-09-01, pre-drafted in parallel with 007)

What exists and what is missing, verified in the working tree:

- **`render.yaml` is essentially ready**: Docker runtime, `/healthz` health
  check, `HARNESS_PUBLIC_ORIGIN` + optional Shopify vars as dashboard-set
  (`sync: false`). Its own comment carries the operational rule: a paid /
  no-sleep instance from final recording through Sep 21. Remaining operator
  inputs: create the service, set `HARNESS_PUBLIC_ORIGIN` to the **deployed
  origin** (the Tier 1 gate run proved origin validation refuses mutations
  from any other origin — the dev-time lesson that becomes a production
  requirement).
- **`Dockerfile` is entirely TODO** — a commented skeleton. The frontend
  stage is now unblocked (both lockfiles committed: harness at 006/T11, store
  at 003/T7), so `npm ci` is the reproducible path in the image too.
- **The single-origin mount does not exist in code.** `create_app` serves
  `/api/v1` and nothing else; static assets and `/demo` mounting are new work
  (T3), and the §25.11 constraint decides the design: the store may NOT be
  imported into the service process. Two honest options — (a) run the store
  as a second in-container process on a loopback port and reverse-proxy
  `/demo/*` from the service; (b) a process supervisor with an fronting
  router. Option (a) keeps one listener on `$PORT` and reuses the existing
  HTTPX client discipline; prefer it unless T3 finds a blocker, and record
  the choice in ADR-0006 (BUILD_ORDER's deployment-composition record).
- **Single worker is load-bearing, not a default**: SQLite + `BEGIN
  IMMEDIATE` (ADR-0003) assumes one process; the CMD must pin `--workers 1`
  and the plan documents why it must never be "tuned up".
- **`HARNESS_PUBLIC_ORIGIN` dual role**: cookie `Secure` flag and the origin
  allowlist both key off it; production must set it to the exact public
  origin, and the health endpoint should report which origin is configured
  (without echoing secrets).
- **CI does not exist** (`.github/` absent). The lanes are already local
  commands (README documents all of them), so T6 is mostly transcription;
  the eval-CLI fixture lane lands with 007 and should be wired here once its
  CLI merges — sequence T6 after 007 closes.
- **AC-01's checklist row** (deferred at the Tier 1 gate) closes in T11
  against the deployed URL; reuse `docs/tier-1-gate-checklist.md` and add
  the Tier 2 manual rows rather than writing a second checklist.

Order of work: T1–T3 (image + composition) are the critical path and gate
everything deployed; T4–T5 ride along in the same service changes; T6–T8 are
parallel-safe; T9–T13 need the operator (Render account, dashboard env,
rollback rehearsal, browser evidence).

Risks:

- **`/demo` proxying must not leak the workspace header semantics**: the
  storefront joins a workspace via its localStorage identity; the proxy must
  pass `X-Workspace-Id` through untouched and add nothing.
- **Seed-on-startup vs migrations**: startup runs the 004 ordered runner and
  seeds only when absent; a redeploy must be a no-op against existing data
  (exit-gate item 5's forward compatibility).
- **The image must not contain the planning docs**: `docs/BUILD_ORDER.md`
  and the functional spec are untracked but PRESENT in a working tree —
  `.dockerignore` (currently absent) must exclude them and the rig dirs, or
  a `COPY . .` ships them into the artifact (constitution §8: no private
  fixtures or local paths in release artifacts).

## Deviations and decisions worth an operator's eye

_To be recorded per task, anchored to spec sections — the 002–007 convention._

### Carried forward, still open

- ADR-0004 integer bound; `maximum_mutations` mapping (M11 — moot if Tier 3
  is cut, record the cut instead); ownerless-confirmation dialog affordance;
  `ExecutionContext.human_consent_granted` shape; store project-allocated
  endpoints + `X-Workspace-Id` (T3's proxy design touches this — confirm
  then); `runs.fault_active`; FR-039 lease surface; store-frontend lint
  (this milestone's T6 is the natural home).
