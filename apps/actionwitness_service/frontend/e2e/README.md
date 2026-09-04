# The automated browser lane

Playwright end-to-end tests that drive the **composed** deployment: the harness
UI at `/`, its API at `/api/v1`, the storefront at `/demo`, and the store's own
API at `/demo/api/v1` behind the harness proxy — one origin, two processes, one
SQLite file each.

```bash
npm run test:e2e          # build the composed tree, start both services, run
npm run test:e2e:ui       # the same, in Playwright's UI mode
npm run typecheck:e2e     # strict tsc over e2e/ and every playwright*.config.ts
```

`e2e/specs/` is the lane this README describes and the only directory
`npm run test:e2e` runs (`playwright.config.ts`). Two non-gating capture lanes
live beside it, each with its own config: `demo-captures/`
(`npm run capture:demo`, `playwright.demo.config.ts`) records paced demo footage
against the same composed deployment, and `shopify-demo/`
(`npm run capture:shopify-demo`, `playwright.shopify-demo.config.ts`) records
the live Shopify development-store proof against a deployed origin, starting no
local servers. Neither is part of any gate below.

## Where this lane sits

Spec §26: *"Automated browser tests in CI are Tier 3 and conditional: they run
only where a flagged or origin-trial browser build can be provisioned, and their
absence shall never fail the release-gating suite."*

So this lane is **not** release-gating and is deliberately outside every gate:

| Gate | Includes this lane? |
|---|---|
| `uv run pytest -q` | No — no Python lane collects it |
| `uv run pytest tests/architecture -q` | No |
| `npm test` (vitest) | No — separate command, separate config |
| `npm run typecheck` | No — `tsconfig.json` covers `src/` only |
| CI jobs in `.github/workflows/ci.yml` | No |

`tests/browser/` stays what §26.4 and §7.5 make it: a **manual** checklist
against the pinned Chrome build, with an architecture gate forbidding an
automation driver inside it. Nothing here changes that, and nothing here claims
to replace it — see "What this lane does not prove".

## What it exercises that nothing else can

Every other layer stops at a boundary this one crosses:

- **Python** (`tests/`) covers every route, service and evaluation path, but
  never through a browser.
- **Vitest/jsdom** (`src/**/*.test.tsx`) covers components and hooks against a
  `document.modelContext` double it also owns, with `fetch` stubbed.

The seam between them — a real browser registry, real HTTP, real cookies, the
real proxy, real SQLite — had no coverage at all. That seam is where the
product's central claims live:

| Spec | What it holds |
|---|---|
| `01-workspace-boot` | AC-01; one-origin composition, security headers, cookie attributes, WebMCP present *and* absent |
| `02-tool-lifecycle` | §11.5 phase-driven registration, FR-003 reconciliation against `getTools()`, untrusted tool names rendered as text |
| `03-false-success` | AC-04 — a tool reports success and the observation contradicts it |
| `04-confirmation` | AC-06, §14.3 — the agent's promise stays pending across a human decision; deny, cancel, focus, keyboard, second tab |
| `05-undeclared-change` | AC-24 — every declared assertion passes and the run still fails |
| `06-tool-surface` | AC-25 — a look-alike registered mid-run, and the side-by-side diff |
| `07-matched-comparison` | AC-20 — a matched pre/post pair, and a mismatched one that stays valid |
| `08-one-origin-storefront` | §29.1/ADR-0006 — the storefront alone, and the boundary between the two applications |
| `09-timeline-and-recovery` | §15.3 polling over a connection that really drops; FR-013 reset |
| `10-guidance-and-refusals` | AC-21 guidance; §15.8 envelopes; origin validation; no internals in agent-visible text |
| `11-workspace-isolation` | AC-11 — two browsers, two workspaces; FR-009's limits and their `Retry-After` |
| `12-regression-eval` | AC-08/12/15 — a failed run becomes a portable case, replayed |
| `13-contract-authoring` | §25.2/FR-021 — the declarative form, its allowlist, and the server's re-check |
| `14-idempotency` | §9.5 — an identical retry that changes state once, a duplicating one, and a reused key with changed intent |

## How it works

**`support/webmcpAgent.ts`** installs a conformant `document.modelContext`
before the bundle boots. Chromium ships no WebMCP without the flag ADR-0002
pins, and §26.4 keeps the flagged build manual, so the registry is the one thing
this lane substitutes — exactly as `src/test/modelContextDouble.ts` does for
jsdom. Everything above and below it is production code.

**`support/harness.ts`** holds the fixtures: a purged workspace before every
test (FR-013's own reset path), an `Agent` that behaves like an agent, and a
`Workspace` page object addressed by role and accessible name. The workspace
splits a Workflow view from an Administration view behind a left rail, and
hidden regions leave the accessibility tree — so the page object switches
views the way a person does (`showWorkflow` / `showAdministration`) before
touching a panel that lives on the other one.

**Rate limits are respected, never relaxed.** FR-009 allows 120 requests a
minute per client with a burst of 30, and ten workspace creations an hour. The
suite therefore:

- mints **one** workspace in `globalSetup` and shares it, resetting between
  tests, rather than one per test;
- runs the service the way it is deployed — behind a trusted loopback proxy —
  and gives each test's browser and API client their own simulated address, so
  each gets exactly one real user's allowance (this also exercises
  `client_key`'s trusted-proxy branch, which nothing else covers);
- honours `Retry-After` when a limit is reached instead of retrying blindly.

Weakening either limit for the lane's convenience would be the trade the
constitution names outright: *"No feature may weaken validation, consent, source
independence, evidence integrity, idempotency, or workspace isolation to improve
demo reliability."*

**No sleeps, no retries.** `retries: 0` — a lane that goes green on the second
attempt is a quarantined failure wearing a tick. Every wait is a web-first
assertion or an `expect.poll`; the only `waitForTimeout` in the suite is the
interval a `429` response explicitly asked for.

**Serial by configuration.** `workers: 1`, `fullyParallel: false` — one shared
workspace and a per-client request budget both make concurrency wrong here.

## What building this lane surfaced

Eight defects, none of which any existing layer could see. All are fixed; each
now has a regression test at the layer that owns it, and the browser test that
found it.

1. **A `toolchange` during an in-flight capture was dropped.**
   `webmcp/surface.ts` guarded with `if (inFlight) return` and scheduled nothing
   afterwards, so a mid-run injection could go unrecorded and
   `stable_tool_surface` passed. Reproduced with the `tool_surface_poisoned`
   profile, whose look-alike registers in the same commit that arms the run —
   inside the baseline's own POST. Now deferred and re-captured.
2. **Surface captures raced verification.** The witness is debounced and
   `verify_outcome` waited for nothing, so a delta recorded after the run sealed
   never reached the verdict. `useToolSurfaceWitness` now returns a `flush()`
   that every verifying path awaits.
3. **Tool hints were read from the wrong level.** `describeTool` took
   `readOnlyHint` / `untrustedContentHint` from the top of a `getTools()`
   descriptor; `webmcp-types` nests them under `annotations`. Every captured
   hint was `null`, so `hint_change` — listed in `one_mug_stable_surface`'s
   `failing_delta_kinds` — could never fire. The jsdom double nests them per the
   spec, which is why the vitest suite could not see it.
4. **The timeline's error was computed and never rendered.** A dropped
   connection looked exactly like a quiet run. `RunTimeline` now says so, and
   keeps the events it already had.
5. **A failed invocation reached the agent as a success.** The invoke route
   answers `200` for a completed round trip, and the tool's own outcome lives in
   `terminal_event` — which nothing read, so a mutation refused for a reused
   idempotency key normalized as a success. The bridge now throws, and the
   adapter's existing `isError` path carries it.
6. **`run_regression_eval` could never register.** `App` supplied no case id, so
   an agent could cut a regression case and had no way to replay it. The toolset
   now remembers the case it created, and a caller's selection wins.
7. **`EvalPanel` was exported, unit-tested, and never rendered.** The regression
   surface had no human path at all. It is now wired to §15.4 through
   `api/evals.ts`. `BenchmarkPanel` was likewise unrendered, and there was no
   panel for the §15.9 audit routes — both since closed: `BenchmarkSection`
   supplies the suite-creation and import flows around `BenchmarkPanel`, and
   `AuditSection` owns the audit view.
8. **`GET /` was inside the request bucket**, so a burst of ordinary navigations
   answered with the JSON envelope rendered as the page body. It is a static
   `index.html`, which FR-009 exempts; it was metered only because
   `startswith("/")` matches everything. The two buckets are now decided
   independently, so `/` is unmetered per minute while still spending the
   stricter workspace-creation allowance it issues cookies from.

## What this lane does not prove

- **That the pinned Chrome build behaves this way.** The registry here is a
  conformant substitute, not Chrome's implementation. ADR-0002's differences —
  `executeTool` forwarding no per-invocation context, in particular — are
  encoded (`invokePinned` passes no second argument), but
  `tests/browser/webmcp-spike-checklist.md` remains the evidence for the real
  browser.
- **Anything about deployment beyond this composition.** It runs the same shape
  the Dockerfile builds, on loopback, over plain HTTP with `HARNESS_ENV=local`.

## Files this lane writes

Everything lands under `apps/actionwitness_service/frontend/.e2e/`, which is
git-ignored: both SQLite databases, the artifact root, the assembled static
tree, and Playwright's traces and screenshots. The databases are discarded on
every run, so the suite cannot become order-dependent.

**Run it through `npm run test:e2e`, not `npx playwright test`.** The wipe lives
in `scripts/build-e2e-static.mjs`, which the npm script chains and a bare
Playwright invocation skips. State then carries between runs, and the first
thing to break is a per-workspace ceiling: `EVAL_CASES_PER_WORKSPACE` is ten,
`12-regression-eval` cuts about four per run, and FR-013's purge removes
terminal runs but not the cases cut from them — so the third or fourth
back-to-back bare run starts failing on a limit rather than on a defect. Use
`AW_E2E_SKIP_BUILD=1 npm run test:e2e` to iterate: it reuses the bundles and
still discards the databases.
