/**
 * Non-gating, deterministic browser recording configuration for the Devpost film.
 *
 * It inherits the exact composed service/store deployment used by the 79 browser
 * journeys, but writes fresh 1920x1080 source video under the ignored video capture
 * directory. Product tests keep video disabled; this file exists only for selected
 * takes whose timing is deliberately paced for a human viewer.
 */

import { defineConfig, devices } from "@playwright/test";

import base from "./playwright.config";

const captureRoot = "../../../videos/actionwitness-demo/capture";

export default defineConfig({
  ...base,
  testDir: "./e2e/demo-captures",
  outputDir: `${captureRoot}/raw`,
  retries: 0,
  reporter: [
    ["list"],
    ["json", { outputFile: `${captureRoot}/capture-report.json` }],
  ],
  use: {
    ...base.use,
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    screenshot: "on",
    trace: "on",
    video: {
      mode: "on",
      size: { width: 1920, height: 1080 },
    },
  },
  projects: [
    {
      name: "chromium-demo-capture",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1920, height: 1080 },
        deviceScaleFactor: 1,
        video: {
          mode: "on",
          size: { width: 1920, height: 1080 },
        },
      },
    },
  ],
});
