# 009 — tasks

Cite the T-ID in every commit that advances it.

- [ ] T1 — Dockerfile frontend stage: `npm ci && npm run build` for BOTH
      frontends from their committed lockfiles; assets staged into
      per-application directories.
- [ ] T2 — Dockerfile runtime stage: `uv sync --frozen --no-dev` installing
      the distributions separately; schema/seed initialization on startup
      (ordered runner, seed only when absent, redeploy-safe); CMD with one
      Uvicorn worker bound to `$PORT`. Add `.dockerignore` excluding the
      untracked planning docs, rig directories, node_modules, and venvs.
- [ ] T3 — Single-origin composition per §25.11 and ADR-0006 (record it):
      service serves `/` (harness assets), `/api/v1`, and reverse-proxies
      `/demo` + `/demo/api/v1` to the in-container store process with no
      direct import; `X-Workspace-Id` passes through untouched.
- [ ] T4 — Structured logging: identifiers, status, duration, classification;
      a test proves no payload, secret, or credential reaches a log line.
- [ ] T5 — Production security posture: `Secure` cookie outside local dev,
      origin policy and CORS from `HARNESS_PUBLIC_ORIGIN`,
      `Permissions-Policy`, trusted-proxy handling for rate-limit keys;
      quotas and cleanup verified against the deployed configuration.
- [ ] T6 — CI workflow: architecture + core-only + store-only isolation,
      Python unit/integration, both frontend typecheck+test+build pairs
      (store lint gap closes here), eval CLI fixture lane (after 007 lands),
      Docker build. No network or wall-clock dependence in required jobs.
- [ ] T7 — Secret and dependency review job; release-artifact hygiene check
      (no secrets, local paths, private fixtures, or build debris in the
      image).
- [ ] T8 — README §29.2 completion: 60-second judge path, architecture
      diagram, core-only install, standalone store run, browser setup,
      security and data-handling notes, adapter skeleton, eval schema + CLI
      usage, evaluator fixture import, known limitations, screenshots,
      attribution.
- [ ] T9 — (operator gate) Deploy to Render: build once, set dashboard env
      (`HARNESS_PUBLIC_ORIGIN` = deployed origin), verify `/healthz`,
      rehearse rollback, retain the previous deploy.
- [ ] T10 — AC-10 clean-checkout job: CI executes the documented README
      commands from a fresh clone and passes.
- [ ] T11 — (operator gate) AC-01 + deployed manual acceptance: the live URL
      loads credential-free and reports WebMCP support; Tier 1 and Tier 2
      manual checklists pass against the deployed URL; screenshots/video
      evidence saved; close the deferred AC-01 row in
      docs/tier-1-gate-checklist.md.
- [ ] T12 — Cut hygiene: every cut Tier 3 feature's control and tool
      registration removed or visibly disabled; product copy claims nothing
      unshipped (constitution §8).
- [ ] T13 — Exit gate roll-up: staging image == production image, health
      signals visible, forward-compatible database, release-artifact
      hygiene; extend the architecture lane's traceability map to 009.
