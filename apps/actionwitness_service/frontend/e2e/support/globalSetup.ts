/**
 * Mint the one workspace the suite shares, and refuse to start on a broken tree.
 *
 * FR-009 allows ten workspace creations per hour per peer and every fresh
 * browser context that loads `/` mints one, so the suite cannot afford a
 * workspace per test. This takes exactly one, writes its cookie jar to
 * `e2e/.auth/workspace.json`, and the `workspace` fixture resets and purges it
 * between tests instead.
 *
 * The preflight assertions are here rather than in a test because their failure
 * is not a product defect — it is "the composed tree was never built" — and a
 * red test would send the reader looking for a bug that is not there.
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { request } from "@playwright/test";

import { HARNESS_URL } from "../../playwright.config";

const FRONTEND_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** Must agree with `WORKSPACE_STATE` in `playwright.config.ts`. */
export const WORKSPACE_STATE_PATH = resolve(FRONTEND_ROOT, "e2e", ".auth", "workspace.json");

export default async function globalSetup(): Promise<void> {
  const api = await request.newContext({ baseURL: HARNESS_URL });
  try {
    const health = await api.get("/healthz");
    if (!health.ok()) {
      throw new Error(`/healthz answered ${String(health.status())}`);
    }
    const body = (await health.json()) as { assets_mounted?: unknown };
    if (body.assets_mounted !== true) {
      throw new Error(
        "the harness reports no mounted assets — build the composed tree first: " +
          "`node scripts/build-e2e-static.mjs` (npm run test:e2e does this for you)",
      );
    }

    const index = await api.get("/");
    if (!index.ok() || !(await index.text()).includes("<title>ActionWitness</title>")) {
      throw new Error("`/` did not serve the harness bundle");
    }

    // The one creation. Reading the workspace is what mints it, and the
    // `Set-Cookie` comes back on this response.
    const workspace = await api.get("/api/v1/workspace");
    if (!workspace.ok()) {
      throw new Error(`GET /api/v1/workspace answered ${String(workspace.status())}`);
    }

    mkdirSync(dirname(WORKSPACE_STATE_PATH), { recursive: true });
    writeFileSync(WORKSPACE_STATE_PATH, JSON.stringify(await api.storageState(), null, 2), "utf-8");
  } finally {
    await api.dispose();
  }
}
