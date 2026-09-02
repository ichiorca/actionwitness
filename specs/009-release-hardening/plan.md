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

_Recorded per task, anchored to spec sections — the 002–007 convention._

### Raised by this milestone

- **T2 — `actionwitness_service` declared neither `httpx` nor `starlette`, and
  imports both.** `api/app.py` owns the lifespan HTTPX client (ADR-0001) and
  `api/middleware.py` subclasses Starlette's `BaseHTTPMiddleware`; both resolved
  only because some *other* distribution pulled them in. Invisible in the dev venv,
  fatal in the image, where §29.1 installs the distributions separately. Both are
  now declared and `uv.lock` regenerated. No architecture gate covers declared-vs-
  imported dependencies — worth one, and not added here because it belongs with the
  import-boundary gates rather than with release hardening.

- **T3 — only `/demo/api/v1/**` is proxied; `/demo` and `/demo/assets/**` are
  static.** tasks.md said "reverse-proxies `/demo` + `/demo/api/v1`". The store
  process has no frontend, so a blanket `/demo/**` proxy would forward storefront
  asset requests to a process with no assets. The split follows §29.1 step 4, which
  copies each application's assets into the image rather than into the store.
  Recorded in ADR-0006.

- **T3 — the storefront takes no harness workspace cookie.** `/demo/**` is now
  exempt from `WorkspaceCookieMiddleware`; it carries its own `X-Workspace-Id`
  (§15.5). Rate limiting is deliberately *not* waived. This also fixed a live bug:
  a storefront-only visitor was spending the workspace-**creation** bucket on every
  request without a workspace ever being created, so ordinary demo use could
  exhaust the hourly allowance for real harness visitors.

- **T4 — a 500 previously produced no structured log line at all.** The `Exception`
  handler is installed on Starlette's `ServerErrorMiddleware`, outside every
  application middleware, so the logging layer saw the raised exception and never a
  response. The one request an operator most needs a line for was the only one that
  produced none. Now logged with the status and code the handler is about to send,
  pinned to the real response by a test.

- **T4 — `scope["route"].path` is unusable for a log field.** FastAPI 0.141 keeps an
  included router nested rather than flattening it, so the route object carries a
  prefix-relative path (`/workspace`, not `/api/v1/workspace`) and `root_path` is
  empty. Templates are derived by reducing the real path's identifier segments
  instead. An **unmatched** path is logged as `<unmatched>` rather than raw: every
  segment of it is caller-chosen, and there is no template to reduce it to.

- **T5 — no `Content-Security-Policy` header. CLOSED 2026-09-01.** The original
  objection was not to the policy but to its absence of a gate: "a CSP that
  nothing asserts is a header that breaks the page on a Friday". So the gate came
  first. `tests/architecture/test_bundle_shape.py` asserts what the policy
  assumes — no inline script, no inline style, no `style={{}}`, no CSS-in-JS, no
  `eval`, no off-origin asset, in either frontend — and
  `CONTENT_SECURITY_POLICY` then ships at `default-src 'none'` with no
  `'unsafe-inline'` and no `'unsafe-eval'`. §20.1 still does not require one;
  this is ordinary hardening, and it is now testable rather than hopeful.

- **T5 — no CORS middleware at all.** §20.1 permits cross-origin access only for the
  Shopify bridge routes, which are not mounted. Asserted as an absence
  (`test_no_cors_headers_are_offered_to_a_cross_origin_caller`) because adding
  `CORSMiddleware` "so the frontend works" is a one-line change that would hand
  every origin read access to a workspace's evidence.

- **T12 — the capability surface reported only *targets*, so a cut module was
  invisible rather than visibly disabled.** `config.MODULE_NAMES` has always
  described itself as "every optional module, in the order the capability bar
  reports them", but `capability_report()` covers registered target adapters only —
  a judge could not tell whether Shopify was switched off or had never been built.
  Added `module_report()` and a `modules` block on `GET /api/v1/workspace`,
  additive alongside `capabilities`. **Which** Tier 3 features are cut remains an
  operator decision; the gate asserts only that a module reported unavailable is
  unavailable everywhere.

- **T6 — the store frontend's lint gap is closed.** ESLint added, mirroring the
  harness config, pinned to the same exact versions. It found two real
  `no-base-to-string` defects in `App.test.tsx` (`String(init?.body)` over a
  `BodyInit`); fixed by narrowing rather than by relaxing the rule. The 006 gate's
  deferral note is now closed rather than left describing a gap that no longer
  exists.

- **T7 — the secret scanner uses inline acknowledgement, not path exclusion.**
  Excluding `tests/` wholesale would mean a real credential pasted into a fixture is
  never found, and fixtures are where credentials get pasted. A line may instead
  carry a marker such as `not-a-real-credential`, which is a claim visible in the
  diff.

- **T6 — CI actions are pinned to major tags, not commit SHAs.** Only first-party
  `actions/*` are used and `uv` is installed from PyPI at an exact version, so no
  third-party action runs. SHA-pinning the four GitHub-owned actions is still the
  stricter posture. **Open for an operator decision.**

### Not done in this milestone — operator gates

- **T9 (deploy) and T11 (deployed manual acceptance) are unstarted.** Both need a
  Render account, a real deploy, a rehearsed rollback, and a human with a browser.
  `docs/release-checklist.md` is the attestation surface, and
  `test_exit_gate_traceability.py` names it as the coverage for exit-gate criteria
  2, 3 and 4, so deleting it fails the gate.
- **Screenshots and the demo video** are captured against the deployed URL and are
  therefore part of T11, not of T8. The README says so rather than shipping empty
  image links.
- **The `docker build` was authored but not executed here**: the local Docker daemon
  was not running. The CI `image` job builds it, runs it, and greps the built
  filesystem; until that job runs green the image is unverified.

### Carried forward, still open

- ADR-0004 integer bound; `maximum_mutations` mapping (M11 — moot if Tier 3
  is cut, record the cut instead); ownerless-confirmation dialog affordance;
  `ExecutionContext.human_consent_granted` shape; store project-allocated
  endpoints + `X-Workspace-Id` (T3's proxy design touches this — confirm
  then); `runs.fault_active`; FR-039 lease surface; store-frontend lint
  (this milestone's T6 is the natural home).
