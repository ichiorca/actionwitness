/**
 * Live external-target recording configuration for the Devpost film.
 *
 * This lane intentionally starts no local servers. It records the deployed
 * ActionWitness UI and the one explicitly configured Shopify development store.
 * Configuration is supplied by the operator at invocation time; no .env file is
 * read and no storefront credential belongs in this repository. Trace capture is
 * deliberately disabled because an authenticated storefront trace can retain
 * request metadata from the temporary storage state.
 */

import { defineConfig, devices } from "@playwright/test";

function requireEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (value === undefined || value === "") {
    throw new Error(
      `${name} is required for the Shopify demo capture. Point it at the deployed ActionWitness origin.`,
    );
  }
  return value;
}

const baseURL = requireEnvironment("ACTIONWITNESS_DEMO_BASE_URL");
const storageState = process.env["ACTIONWITNESS_SHOPIFY_STORAGE_STATE"]?.trim();
const captureRoot = "../../../videos/actionwitness-demo/capture";

export default defineConfig({
  testDir: "./e2e/shopify-demo",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  outputDir: `${captureRoot}/shopify-raw`,
  reporter: [
    ["list"],
    ["json", { outputFile: `${captureRoot}/shopify-capture-report.json` }],
  ],
  use: {
    baseURL,
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    screenshot: "on",
    trace: "off",
    video: {
      mode: "on",
      size: { width: 1920, height: 1080 },
    },
    ...(storageState === undefined || storageState === "" ? {} : { storageState }),
  },
  projects: [
    {
      name: "chromium-shopify-demo-capture",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1920, height: 1080 },
        deviceScaleFactor: 1,
      },
    },
  ],
});
