# The automated browser lane

Playwright end-to-end tests that drive the **composed** deployment: the harness
UI at `/`, its API at `/api/v1`, the storefront at `/demo`, and the store's own
API at `/demo/api/v1` behind the harness proxy — one origin, two processes, one
SQLite file each.

```bash
npm run test:e2e          # build the composed tree, start both services, run
npm run test:e2e:ui       # the same, in Playwright's UI mode
npm run typecheck:e2e     # strict tsc over e2e/ and playwright.config.ts
```

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
`Workspace` page object addressed by role and accessible name.

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

Findings in the product, recorded here because they are the reason several
assertions below read the way they do. None are fixed by this change.

1. **A `toolchange` that arrives during an in-flight capture is dropped.**
   `webmcp/surface.ts` guards with `if (inFlight) return` and schedules nothing
   afterwards, so a mid-run tool injection can go unrecorded and
   `stable_tool_surface` passes. Observed intermittently with the
   `tool_surface_poisoned` profile, whose look-alike registers in the same
   commit that arms the run — racing the baseline POST it is supposed to follow.
2. **Surface captures race verification.** The witness is debounced
   (`TOOLCHANGE_QUIET_PERIOD_MS`) and `verify_outcome` waits for nothing, so a
   delta recorded after the run is sealed does not reach the verdict.
   `06-tool-surface` waits for the capture to be on the record before verifying,
   which is why it is deterministic.
3. **Tool hints are read from the wrong level.** `describeTool` takes
   `readOnlyHint` / `untrustedContentHint` from the top of a `getTools()`
   descriptor, but `webmcp-types` nests them under `annotations`. Captured
   surfaces therefore always carry `null` for both, and `hint_change` — listed
   in `one_mug_stable_surface`'s `failing_delta_kinds` — cannot fire. The jsdom
   double nests them per the spec, so the vitest suite cannot see this.
4. **The timeline's error state is never rendered.** `useRunTimeline` computes an
   `error`; `App` passes only `events`, `runStatus` and `polling` to
   `RunTimeline`. A dropped connection is invisible to the reader — confirmed in
   the browser with `setOffline(true)` in `09-timeline-and-recovery`.
5. **A failed target invocation reaches the agent as a non-error result.** The
   invoke route answers `200` for a completed invocation whose `terminal_event`
   is `tool_invocation_failed`, and the adapter normalizes a resolved value as a
   success — so an agent branching on `isError` reads a refused, key-reused
   mutation as one that worked (`14-idempotency`).
6. **`run_regression_eval` can never register.** `App` calls
   `useHarnessToolset(status, refresh)` with two arguments, so `evalCaseId` is
   always `null` and the tool's `enabled` is always false. AC-22 measures "every
   capability is reachable by tool" (`12-regression-eval`).
7. **`EvalPanel` and `BenchmarkPanel` are exported and unit-tested but never
   rendered** by `App`, and there is no audit panel for the §15.9 routes. Those
   surfaces have no human path in the shipped page, so no browser test can reach
   them.
8. **`GET /` is inside the request bucket.** FR-009 exempts health and static
   only, so a burst of navigations serves the JSON error envelope as the page
   body rather than a readable refusal. Per spec, but worth a look.

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
