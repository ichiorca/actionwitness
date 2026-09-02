/**
 * Assemble the composed one-origin tree the e2e lane runs against (spec §29.1).
 *
 * The lane deliberately does NOT drive two Vite dev servers. §29.1's deployment
 * puts the harness at `/`, its API at `/api/v1`, the storefront at `/demo`, and
 * the store's own API at `/demo/api/v1` behind the harness proxy — and half the
 * things worth testing end to end (the workspace cookie's `SameSite=Strict`
 * scope, `Origin` validation, the proxy's header allowlist, the security
 * headers, the storefront and the harness sharing an origin without sharing a
 * workspace) only exist in that shape. A dev-server run would test a topology
 * nobody deploys.
 *
 * So this mirrors the Dockerfile's frontend stage: build both bundles, drop the
 * ADR-0002 spike page, and lay them out as `static/harness` and `static/demo`
 * for `HARNESS_STATIC_ROOT`. The storefront is built with `--base=/demo/` for
 * the same reason the Dockerfile does it — its assets are served from a
 * subdirectory in the composed deployment and from `/` under `npm run dev`, and
 * the base belongs to the build rather than to the checked-in config.
 *
 * **This runs before Playwright, not inside it.** Both application processes are
 * `webServer` entries and Playwright starts them concurrently, so a build that
 * lived inside one of their commands would be racing the other one's database
 * directory. `npm run test:e2e` chains this first; `npx playwright test` on its
 * own reports the missing tree from `globalSetup` with the command to fix it.
 *
 * Databases are removed rather than reused. A run that inherited the previous
 * run's workspaces, runs, and cart state would be order-dependent, which the
 * constitution's quality bars forbid outright.
 *
 * Skip the rebuild with AW_E2E_SKIP_BUILD=1 while iterating on specs: the
 * bundles are reused, and the databases are still discarded.
 */

import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const HARNESS_FRONTEND = resolve(HERE, "..");
const REPO_ROOT = resolve(HARNESS_FRONTEND, "..", "..", "..");
const STORE_FRONTEND = join(REPO_ROOT, "examples", "buggy_store", "frontend");

/** Everything this lane writes lives here, and nothing else does. */
const E2E_ROOT = join(HARNESS_FRONTEND, ".e2e");
const STATIC_ROOT = join(E2E_ROOT, "static");

const npm = process.platform === "win32" ? "npm.cmd" : "npm";

function run(command, args, cwd) {
  process.stdout.write(`[e2e] ${command} ${args.join(" ")}  (${cwd})\n`);
  execFileSync(command, args, { cwd, stdio: "inherit", shell: process.platform === "win32" });
}

function ensureDependencies(frontend) {
  if (!existsSync(join(frontend, "node_modules"))) {
    // `ci` rather than `install`: the lockfile records the tree the gates ran
    // against, and a lane that resolved its own would test a different one.
    run(npm, ["ci"], frontend);
  }
}

function main() {
  // State, not bundles: discarded on every run so the suite cannot become
  // order-dependent, whether or not the bundles are rebuilt.
  for (const name of ["actionwitness.sqlite3", "buggy-store.sqlite3"]) {
    rmSync(join(E2E_ROOT, name), { force: true });
  }
  rmSync(join(E2E_ROOT, "artifacts"), { recursive: true, force: true });
  rmSync(join(E2E_ROOT, "results"), { recursive: true, force: true });
  // Both server processes open files under here before anything else runs, so
  // the directories have to exist before Playwright starts them.
  mkdirSync(join(E2E_ROOT, "artifacts"), { recursive: true });

  if (process.env["AW_E2E_SKIP_BUILD"] === "1" && existsSync(join(STATIC_ROOT, "harness"))) {
    process.stdout.write("[e2e] AW_E2E_SKIP_BUILD=1 — reusing the assembled bundles\n");
    return;
  }

  ensureDependencies(HARNESS_FRONTEND);
  ensureDependencies(STORE_FRONTEND);

  run(npm, ["run", "build"], HARNESS_FRONTEND);
  // The spike is an ADR-0002 decision harness that registers WebMCP tools of
  // its own. The release image strips it for exactly this reason; a lane that
  // shipped it would let a decision tool leak into the surface these tests
  // reconcile against.
  rmSync(join(HARNESS_FRONTEND, "dist", "spike.html"), { force: true });

  run(npm, ["run", "build", "--", "--base=/demo/"], STORE_FRONTEND);

  rmSync(STATIC_ROOT, { recursive: true, force: true });
  mkdirSync(STATIC_ROOT, { recursive: true });
  cpSync(join(HARNESS_FRONTEND, "dist"), join(STATIC_ROOT, "harness"), { recursive: true });
  cpSync(join(STORE_FRONTEND, "dist"), join(STATIC_ROOT, "demo"), { recursive: true });

  process.stdout.write(`[e2e] composed tree ready at ${STATIC_ROOT}\n`);
}

main();
