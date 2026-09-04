/**
 * Fresh product footage for the Devpost film.
 *
 * This is intentionally outside e2e/specs: explicit pacing belongs in media
 * capture, never in the release-gating tests. Every state transition still uses
 * the production page, registered WebMCP surface, public API, real adapters,
 * observer, evaluator, and persistence layer from the composed E2E deployment.
 */

import type { Page } from "@playwright/test";

import {
  APPLY_DISCOUNT,
  GET_RUN_FINDINGS,
  GET_WORKSPACE_STATUS,
  MUG,
  SAVE20,
  SEARCH_CATALOG,
  TEMPLATE_ONE_MUG_SAVE20,
  UPDATE_CART,
  expect,
  test,
  type HarnessApi,
} from "../support/harness";

const DISCOUNT_FAULT = "discount_reported_but_not_applied";
const SELF_TIMELINE_TEMPLATE = "self_completed_run_timeline_is_immutable";
const BENCHMARK_SCENARIO = "adds a mug";
const BENCHMARK_VARIANT = "Please add a ceramic mug to my cart and use the SAVE20 code.";
const CANONICAL_INTENT = "Add one ceramic mug to the cart and apply the SAVE20 discount.";

const evaluatorReport = JSON.stringify({
  config: { reporterSchema: "webmcp-evals/0.0.4", evaluatorVersion: "0.0.4" },
  results: {
    results: [
      {
        test: { name: BENCHMARK_SCENARIO },
        outcome: "pass",
        runIndex: 0,
        trajectory: [
          {
            name: UPDATE_CART,
            arguments: {
              product_id: MUG,
              quantity: 1,
              request_id: "devpost-benchmark-cart",
            },
          },
          { name: APPLY_DISCOUNT, arguments: { code: SAVE20 } },
        ],
      },
    ],
    testCount: 1,
    passCount: 1,
    failCount: 0,
    errorCount: 0,
  },
});

async function hold(page: Page, milliseconds = 1_400): Promise<void> {
  await page.waitForTimeout(milliseconds);
}

async function activeRunId(harness: HarnessApi): Promise<string> {
  const status = await harness.workspace();
  const run = status["active_run"];
  if (typeof run !== "object" || run === null) {
    throw new Error("The capture expected an active run.");
  }
  const id = (run as Record<string, unknown>)["id"];
  if (typeof id !== "string" || id === "") {
    throw new Error("The active run did not expose a stable id.");
  }
  return id;
}

async function caseHashForRun(harness: HarnessApi, sourceRunId: string): Promise<string> {
  const response = await harness.raw.get("/api/v1/evals");
  if (!response.ok()) {
    throw new Error(`Could not list regression cases: ${await response.text()}`);
  }
  const payload = (await response.json()) as {
    cases?: { content_hash?: unknown; source_run_id?: unknown }[];
  };
  const found = (payload.cases ?? []).find((item) => item.source_run_id === sourceRunId);
  if (typeof found?.content_hash !== "string") {
    throw new Error(`No regression case was created from ${sourceRunId}.`);
  }
  return found.content_hash;
}

test.describe("Devpost source footage", () => {
  test.setTimeout(180_000);

  test("01 — live false success and regression replay", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
    await harness.setScenarioMode("pre_fix");
    await harness.setFailureProfile(DISCOUNT_FAULT);

    await workspace.open();
    await hold(workspace.page, 2_200);

    await workspace.panel("Contract").scrollIntoViewIfNeeded();
    await hold(workspace.page);
    await workspace.arm();
    const runId = await activeRunId(harness);
    await hold(workspace.page, 1_800);

    await agent.call(SEARCH_CATALOG, { query: "mug" });
    await hold(workspace.page);
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "devpost-false-success-cart",
    });
    await hold(workspace.page);
    const discount = await agent.call(APPLY_DISCOUNT, { code: SAVE20 });
    expect((discount["reported"] as Record<string, unknown>)["status"]).toBe("success");

    await expect(workspace.timeline).toContainText("reported:");
    await expect(workspace.timeline).toContainText("apply_discount");
    await workspace.timeline.scrollIntoViewIfNeeded();
    await hold(workspace.page, 3_200);

    await workspace.verify();
    await workspace.page.getByRole("button", { name: /Verdict/ }).click();
    await workspace.findings.scrollIntoViewIfNeeded();
    await expect(workspace.findings).toContainText("failed");
    await expect(workspace.findings).toContainText("discounted-total");
    await expect(workspace.findings).toContainText("false_success_or_state_mismatch");
    await hold(workspace.page, 4_200);

    await workspace.newSession();
    await workspace.page.getByRole("button", { name: /Regression/ }).click();
    const panel = workspace.panel("Regression evals");
    await panel.scrollIntoViewIfNeeded();
    const create = panel.getByRole("button", { name: /Create a regression/ });
    await expect(create).toBeEnabled();
    await hold(workspace.page);
    await create.click();

    await expect(panel).toContainText("sha256:");
    const hash = await caseHashForRun(harness, runId);
    const row = panel.locator("li").filter({ hasText: hash });
    await expect(row).toHaveCount(1);
    await expect(row).toContainText("sha256:");
    await row.scrollIntoViewIfNeeded();
    await hold(workspace.page, 2_600);

    await row.getByRole("button", { name: "Replay against source" }).click();
    await expect(row).toContainText("Eval:");
    await expect(row).toContainText("Target outcome:");
    await expect(row).toContainText("reproduce_source");
    await hold(workspace.page, 4_500);
  });

  test("02 — ActionWitness verifies itself over the public API", async ({
    workspace,
    harness,
  }) => {
    await harness.selectTemplate(SELF_TIMELINE_TEMPLATE);

    await workspace.open();
    await workspace.panel("Contract").scrollIntoViewIfNeeded();
    await hold(workspace.page, 2_200);
    await workspace.arm();

    const runId = await activeRunId(harness);
    const target = workspace.panel("Target");
    await target.scrollIntoViewIfNeeded();
    await expect(target).toContainText("Observing:");
    await hold(workspace.page, 3_000);

    for (const toolName of [GET_RUN_FINDINGS, GET_WORKSPACE_STATUS]) {
      const response = await harness.raw.post(
        `/api/v1/runs/${runId}/target-tools/${toolName}:invoke`,
        { data: { arguments: {} } },
      );
      if (!response.ok()) {
        throw new Error(`Self-target tool ${toolName} failed: ${await response.text()}`);
      }
      await hold(workspace.page, 1_600);
    }

    await expect(workspace.timeline).toContainText(GET_RUN_FINDINGS);
    await expect(workspace.timeline).toContainText(GET_WORKSPACE_STATUS);
    await workspace.timeline.scrollIntoViewIfNeeded();
    await hold(workspace.page, 3_200);

    // These self-target calls intentionally arrive over the public API rather
    // than through a page-owned callback. Reload once so the operator view reads
    // the server's now-running phase before using its visible Verify control.
    await workspace.open();
    await workspace.expectPhase("running");
    await hold(workspace.page, 1_400);
    await workspace.verify();
    await workspace.page.getByRole("button", { name: /Verdict/ }).click();
    await workspace.findings.scrollIntoViewIfNeeded();
    await expect(workspace.findings).toContainText("passed");
    await hold(workspace.page, 4_200);
  });

  test("03 — frozen intent and repeated-trial correlation", async ({
    workspace,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
    const created = await harness.raw.post("/api/v1/benchmarks", {
      data: {
        source_kind: "recorded_fixture",
        correlation_mode: "imported_trajectory_replay",
        scenarios: [
          {
            scenario_id: BENCHMARK_SCENARIO,
            scenario_mode: "pre_fix",
            failure_profile: DISCOUNT_FAULT,
          },
        ],
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();

    await workspace.open();
    await workspace.page.getByRole("button", { name: /Benchmark/ }).click();
    const manifest = workspace.panel("Frozen intent manifest");
    await manifest.scrollIntoViewIfNeeded();
    await expect(manifest).toContainText("Not frozen");
    await hold(workspace.page, 2_200);

    await manifest.getByLabel("Canonical intent").fill(CANONICAL_INTENT);
    await manifest.getByLabel("Reviewer").fill("Rohit Bajaj");
    await manifest.getByLabel("Variant 1", { exact: true }).fill(BENCHMARK_VARIANT);
    await hold(workspace.page, 1_800);
    await manifest.getByRole("button", { name: "Freeze variant set" }).click();
    await expect(manifest).toContainText("Variant set: Frozen");
    await expect(manifest).toContainText("Rohit Bajaj");
    await expect(manifest).toContainText("Manifest identity");
    await hold(workspace.page, 3_600);

    const suites = workspace.panel("Benchmark suites");
    await suites.scrollIntoViewIfNeeded();
    await suites.getByLabel("Evaluator report (JSON)").setInputFiles({
      name: "devpost-evaluator-pass.json",
      mimeType: "application/json",
      buffer: Buffer.from(evaluatorReport),
    });
    await hold(workspace.page, 1_200);
    await suites.getByRole("button", { name: "Import report" }).click();

    const repeated = workspace.panel("Repeated trials and correlation");
    await repeated.scrollIntoViewIfNeeded();
    await expect(repeated.getByRole("button", { name: "Run repeated trials" })).toBeEnabled();
    await hold(workspace.page, 2_800);
    await repeated.getByRole("button", { name: "Run repeated trials" }).click();
    await expect(repeated).toContainText("3 trials recorded.", { timeout: 60_000 });
    await expect(repeated).toContainText("silent-failure rate of 1");
    await expect(repeated).toContainText("Call passed");
    await expect(repeated).toContainText("Observed state failed");
    await repeated.scrollIntoViewIfNeeded();
    await hold(workspace.page, 5_200);
  });
});
