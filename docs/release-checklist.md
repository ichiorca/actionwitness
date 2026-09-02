# Release checklist — operator-attested (spec 009 / BUILD_ORDER M8)

Three of 009's six exit criteria cannot be discharged from a terminal. Criterion 2
needs a Render account, a real deploy, and a rollback actually rehearsed; criteria
3 and 4 need a human driving a browser against the deployed URL. Faking any of them
with a test that asserts a constant would be worse than leaving them open, so they
are **operator-attested**: run them by hand, record the result, and date it.

`tests/architecture/test_exit_gate_traceability.py` names this file as the coverage
for those criteria — a deleted checklist fails that gate rather than leaving the map
looking complete.

This closes the AC-01 row deferred from `docs/tier-1-gate-checklist.md`.

## What is already automated

Do not re-check these by hand. They run on every `uv run pytest -q`:

| Criterion | Covered by |
|---|---|
| 1 — a fresh checkout follows the README | `tests/architecture/test_readme_commands.py`, and the `readme-clean-checkout` CI job |
| 5a — health and readiness signals are visible | `tests/integration/test_production_security_posture.py` |
| 5b — a redeploy against existing data is a no-op | `tests/integration/test_harness_migrations.py` |
| 6 — no secrets, local paths, private fixtures, or build debris | `tests/architecture/test_release_artifact_hygiene.py`, and the CI `image` job |
| §25.11 — co-location does not bypass the versioned target API | `tests/integration/test_one_origin_composition.py` |
| Production cookie, origin policy, `Permissions-Policy`, CORS, trusted proxies | `tests/integration/test_production_security_posture.py` |
| `Content-Security-Policy` is served, and the bundle stays the shape it assumes | `tests/integration/test_production_security_posture.py`, `tests/architecture/test_bundle_shape.py` |

## Before you deploy

```
uv run pytest -q
uv run pytest tests/architecture -q
uv run python scripts/scan_for_secrets.py
cd apps/actionwitness_service/frontend && npm ci && npm run typecheck && npm run lint && npm test && npm run build
cd ../../../examples/buggy_store/frontend && npm ci && npm run typecheck && npm run lint && npm test && npm run build
docker build -t actionwitness .
```

The image must build from a **clean checkout**, not from your working tree. If
`docker build` succeeds locally but the CI `image` job fails, the difference is
almost always a file `.dockerignore` excludes and your tree has.

---

## Before you call the deploy good — the one-minute console check

The automated gates prove the policy is served and that the bundle needs nothing
the policy forbids. What they cannot prove is that the *deployed* page loads
under it, because no test in this repository runs a browser against the live URL.

Open the deployed URL with DevTools on the Console tab and confirm:

- [ ] The workspace renders — not a blank page with a `Refused to ...` line.
- [ ] `/demo` renders too. One policy covers both bundles.
- [ ] No `Content Security Policy` violation appears in the console during a full
      Journey A run, including the run timeline's `EventSource` connection.
- [ ] If a violation *does* appear: do not add `'unsafe-inline'`. The directive it
      names tells you what the bundle grew, and
      `tests/architecture/test_bundle_shape.py` is where that should have failed —
      fix the gate first, so the next person is told at commit time.

Attested by: ______________________  Date: ____________

---

## Criterion 2 — the artifact deployed is the artifact tested

The rule is one build, promoted. Building twice — once to test, once to deploy —
means the thing you tested is not the thing that is running, and the difference
will be invisible until it matters.

- [ ] The image was built **once** and its digest recorded below.
- [ ] That digest is what staging ran.
- [ ] That same digest is what production runs. (Render: confirm the deploy's
      image digest, not just the commit SHA.)
- [ ] `HARNESS_PUBLIC_ORIGIN` in the Render dashboard is the **exact deployed
      origin**, scheme and host and no trailing slash. `GET /healthz` echoes the
      value the service resolved — check it there rather than in the dashboard,
      because an unparseable value is dropped and reported as `null`.
- [ ] The instance does not cold-start during judging, and stays that way from
      final video recording through September 21 (spec §29.1). A cold start
      during judging is a failed demo. `render.yaml` commits to the **free**
      tier by operator decision, so on that tier this means confirming the
      external `/healthz` pinger is running at a five-minute interval — Render
      spins down after fifteen idle minutes. Tick this by naming which of the
      two holds:
      - [ ] Paid / verified no-sleep tier, **or**
      - [ ] Free tier with the pinger confirmed live: ______________________
- [ ] The previous deploy is retained and visible in the Render dashboard.

**Rollback rehearsed** — not "available", rehearsed:

- [ ] Rolled back to the previous deploy from the dashboard.
- [ ] `GET /healthz` returned `ok` on the rolled-back deploy — including
      `"database": "ok"`, which is read live rather than remembered from
      startup, so it is the field that actually shows the volume came back.
- [ ] A journey still completes on the rolled-back deploy. Start a **fresh**
      journey rather than resuming one from before the rollback: `render.yaml`
      declares no disk, so on the free tier `/data` is ephemeral and every
      workspace, run, finding, and evidence chain is destroyed by the redeploy.
      The forward-compatibility this step checks is the *schema's* — that the
      older image can still read today's tables — not the survival of any row.
- [ ] Rolled forward again.

> **Evidence is ephemeral on this deployment.** No Render disk is attached, so
> the SQLite database and every artifact under `/data` are lost on each deploy,
> restart, and spin-down. Nothing here is a backup, and none of it is intended
> as a record anyone can return to later: export anything a demo or a judge
> needs to keep — the outcome report, the benchmark JSON, the eval case — before
> the next deploy. Attaching a disk is the change that would make this untrue.

```
Image digest: sha256:____________________________________________
Staging deploy id: ______________  Production deploy id: ______________
Previous deploy retained: ______________
```

Attested by: ______________________  Date: ____________

---

## Criterion 3 — AC-01, the live URL loads credential-free

Use a browser profile with **no cookies for this origin and no logged-in
account**. A private window is the easy way.

- [ ] The live URL loads with no credential, no login, and no configuration.
- [ ] The workspace reports its WebMCP support status as a fact — supported or
      not — rather than failing or blocking.
- [ ] Repeat with `chrome://flags/#enable-webmcp-testing` **Disabled**: the page
      still loads and the whole journey is still completable by hand (AC-09).
- [ ] `/demo` loads the storefront, and it works with no harness interaction.
- [ ] Nothing in the page source, the network log, or `/healthz` contains a
      credential, an access token, or a local filesystem path.

```
Live URL: ______________________________________________
Browser + build: ______________________  WebMCP reported as: ______________
```

Attested by: ______________________  Date: ____________

---

## Criterion 4 — Tier 1 and Tier 2 manual acceptance against the deployed URL

Run `docs/tier-1-gate-checklist.md` in full **against the deployed URL** rather
than against localhost. Most of it will pass identically; the ones that do not are
the ones this criterion exists for — origin policy, cookie `Secure`, and anything
that behaves differently behind a proxy.

Tier 1:

- [ ] Journeys A and B complete through real WebMCP tools.
- [ ] The unsupported-browser path completes the manual equivalent.
- [ ] No order exists before approval; one approval produces exactly one order;
      denial, expiry, and cancellation each create none.
- [ ] A `pre_fix` run shows a **successful tool response** beside a **failed
      business outcome**. This is the screenshot.

Tier 2:

- [ ] A failed run produces a regression case, downloadable from the UI.
- [ ] `uv run actionwitness eval run <case>.json --environment current` exits 0
      locally against the downloaded case.
- [ ] An external evaluator report imports and correlates, with unbound trials
      reported as unbound rather than guessed.
- [ ] No third-party credential was used at any point in either tier.

Evidence:

- [ ] Screenshots or a short GIF of the layered failure result saved and added to
      the README (§29.2).
- [ ] Demo video recorded against this deploy.

```
Tier 1 result: ______________  Tier 2 result: ______________
Evidence stored at: ______________________________________________
```

Attested by: ______________________  Date: ____________

---

## After attesting

- [ ] Tick the AC-01 row in `docs/tier-1-gate-checklist.md` and date it there too.
- [ ] Replace the README's "_deployed URL pending_" line with the live URL.
- [ ] Add the screenshots to the README.

## If something here fails

A failure on this checklist is an exit-gate failure, not a note. Record it in
`specs/009-release-hardening/plan.md` under the deviations ledger, and fix it
before declaring M8 green.
