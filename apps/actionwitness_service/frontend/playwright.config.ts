/**
 * The Tier 3 automated browser lane (spec §26, §7.5).
 *
 * §26 is explicit about this lane's standing: "Automated browser tests in CI are
 * Tier 3 and conditional: they run only where a flagged or origin-trial browser
 * build can be provisioned, and their absence shall never fail the
 * release-gating suite." So this is a separate command (`npm run test:e2e`)
 * outside `npm test`, outside `uv run pytest -q`, and outside the CI jobs that
 * gate a release. Nothing here can make a green release go red by being absent.
 *
 * ## Why it is composed rather than served by Vite
 *
 * `scripts/build-e2e-static.mjs` assembles §29.1's one-origin tree and the
 * harness serves it: `/` is the workspace, `/api/v1` its API, `/demo` the
 * storefront, `/demo/api/v1` the store's own API behind the harness proxy. The
 * behaviours this lane exists to cover — the `SameSite=Strict` workspace cookie,
 * `Origin` validation on mutations, the proxy's header allowlist, the storefront
 * sharing an origin with the harness while sharing no workspace — do not exist
 * under two Vite dev servers.
 *
 * ## Why it is serial, and shares one workspace
 *
 * FR-009's limits are real and are not relaxed for tests: 10 workspace creations
 * per hour per peer, 120 requests per minute with a burst of 30. Every fresh
 * browser context that loads `/` mints a workspace, so a suite of per-test
 * contexts would exhaust the hour's allowance before it finished. `globalSetup`
 * mints one workspace, and the `workspace` fixture resets and purges it between
 * tests — which is FR-013's documented recovery path, exercised on every test
 * rather than once. Tests that genuinely need a second workspace ask for one
 * explicitly and stay well inside the allowance.
 *
 * Weakening either limit to make the lane easier would be the exact trade the
 * constitution forbids: "No feature may weaken validation, consent, source
 * independence, evidence integrity, idempotency, or workspace isolation to
 * improve demo reliability."
 */

import { defineConfig, devices } from "@playwright/test";

/** Deliberately not 8000/8001, so a running dev stack is never the thing tested. */
export const HARNESS_PORT = 8010;
export const STORE_PORT = 8011;
export const HARNESS_URL = `http://127.0.0.1:${String(HARNESS_PORT)}`;

/** Written by `globalSetup`, consumed as every test's starting cookie jar. */
export const WORKSPACE_STATE = "./e2e/.auth/workspace.json";

const E2E_ROOT = "./.e2e";
const REPO_ROOT = "../../..";

export default defineConfig({
  testDir: "./e2e/specs",
  globalSetup: "./e2e/support/globalSetup.ts",
  outputDir: "./.e2e/results",

  // Serial by construction, not by accident: one shared workspace and a
  // per-peer request budget both make concurrent workers wrong here.
  workers: 1,
  fullyParallel: false,

  // No retries. A lane that passes on the second attempt is a quarantined
  // failure wearing a green tick, and the constitution forbids those.
  retries: 0,
  forbidOnly: process.env["CI"] !== undefined,

  timeout: 90_000,
  expect: { timeout: 20_000 },

  reporter: process.env["CI"] === undefined ? [["list"]] : [["list"], ["html", { open: "never" }]],

  use: {
    baseURL: HARNESS_URL,
    storageState: WORKSPACE_STATE,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    // Web-first assertions wait; nothing in this lane sleeps.
    actionTimeout: 20_000,
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  webServer: [
    {
      // The demo target, as its own process on loopback. §29.1 and ADR-0006:
      // co-location must not bypass the versioned target API, so the harness
      // reaches it over HTTP exactly as the adapter does in production.
      command: "uv run buggy-store",
      cwd: REPO_ROOT,
      url: `http://127.0.0.1:${String(STORE_PORT)}/healthz`,
      reuseExistingServer: process.env["CI"] === undefined,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        BUGGY_STORE_PORT: String(STORE_PORT),
        BUGGY_STORE_DATABASE: `apps/actionwitness_service/frontend/${E2E_ROOT.slice(2)}/buggy-store.sqlite3`,
      },
    },
    {
      // One worker, not a default: ADR-0003's `BEGIN IMMEDIATE` lock model
      // assumes a single writer process against one SQLite file.
      command:
        "uv run uvicorn actionwitness_service.api.app:create_app --factory " +
        `--host 127.0.0.1 --port ${String(HARNESS_PORT)} --workers 1`,
      cwd: REPO_ROOT,
      url: `${HARNESS_URL}/healthz`,
      reuseExistingServer: process.env["CI"] === undefined,
      timeout: 120_000,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        // A developer's root `.env` may name a deployed public origin or enable
        // optional integrations. The E2E stack is a hermetic local deployment;
        // make its complete configuration the mapping below instead of quietly
        // inheriting machine-local values through the service's `.env` loader.
        HARNESS_ENV_FILE: `apps/actionwitness_service/frontend/${E2E_ROOT.slice(2)}/no-env`,
        // `local` omits only the cookie's `Secure` attribute (FR-005); the
        // lane runs over plain HTTP on loopback, and `HttpOnly` and
        // `SameSite=Strict` stay on, which is what the cookie tests assert.
        HARNESS_ENV: "local",
        HARNESS_DATABASE_PATH: `apps/actionwitness_service/frontend/${E2E_ROOT.slice(2)}/actionwitness.sqlite3`,
        HARNESS_ARTIFACT_ROOT: `apps/actionwitness_service/frontend/${E2E_ROOT.slice(2)}/artifacts`,
        HARNESS_STATIC_ROOT: `apps/actionwitness_service/frontend/${E2E_ROOT.slice(2)}/static`,
        BUGGY_STORE_ENABLED: "true",
        BUGGY_STORE_BASE_URL: `http://127.0.0.1:${String(STORE_PORT)}`,
        // §20.1's trusted-proxy configuration, which the deployed service also
        // runs with. It is what lets each test present as its own client (see
        // `CLIENT_HEADER` in `e2e/support/harness.ts`) instead of the whole
        // suite spending one browser's request allowance — and it means the
        // key derivation itself is exercised, which nothing else covers
        // end to end.
        HARNESS_TRUSTED_PROXIES: "127.0.0.1",
      },
    },
  ],
});
