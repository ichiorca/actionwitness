/**
 * AC-04: a syntactically successful tool response contradicted by observed state.
 *
 * This is the product's central claim, and it is the one claim no single layer
 * can demonstrate on its own. The Python suite proves the engine reaches the
 * right verdict from recorded evidence; the vitest suite proves the panels
 * render a verdict handed to them. Neither watches an agent call a tool in a
 * browser, receive `"status": "success"`, and then watch the harness contradict
 * it from an observation the tool did not produce.
 *
 * So the assertions here are deliberately layered: the tool's own words, the
 * independent observation, the timeline that keeps them distinguishable, and the
 * finding that names the disagreement.
 */

import {
  APPLY_DISCOUNT,
  GET_RUN_FINDINGS,
  MUG,
  SAVE20,
  SEARCH_CATALOG,
  TEMPLATE_ONE_MUG_SAVE20,
  UPDATE_CART,
  VERIFY_OUTCOME,
  bodyOf,
  expect,
  test,
} from "../support/harness";

const DISCOUNT_FAULT = "discount_reported_but_not_applied";

test.describe("journey A — the discount that was reported but never applied", () => {
  test.beforeEach(async ({ harness }) => {
    await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
    await harness.setScenarioMode("pre_fix");
    await harness.setFailureProfile(DISCOUNT_FAULT);
  });

  test("reports success, observes no discount, and fails the run for it", async ({
    workspace,
    agent,
    harness,
  }) => {
    await workspace.open();
    await workspace.arm();
    await workspace.expectPhase("armed");

    const runId = String((await harness.workspace())["active_run_id"] ?? "");
    await agent.call(SEARCH_CATALOG, { query: "mug" });
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-false-success-cart",
    });

    // The call under test. §13.3: the response is a valid success and the
    // canonical cart keeps no discount and an unchanged total.
    const discount = await agent.call(APPLY_DISCOUNT, { code: SAVE20 });

    // What the tool said about itself.
    const reported = discount["reported"] as Record<string, unknown>;
    expect(reported["status"]).toBe("success");

    // What was independently observed, in the same response and kept separate
    // from the self-report — the distinction §23.1 exists to preserve.
    expect(discount).toHaveProperty("observed");

    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();

    // The verdict, spelled out in words rather than signalled by colour.
    await expect(workspace.findings).toContainText("failed");
    await expect(workspace.findings).toContainText("discounted-total");
    await expect(workspace.findings).toContainText("false_success_or_state_mismatch");

    // The assertion that failed did so on the *total*, not on the mug: the
    // cart mutation was correct and only the discount was a lie.
    await expect(workspace.findings).toContainText("mug-quantity");
    const findings = await harness.findings(await currentRunId(harness, runId));
    const failed = (findings["findings"] as Record<string, unknown>[]).filter(
      (finding) => finding["status"] === "failed",
    );
    expect(failed.map((finding) => finding["check_id"])).toContain("discounted-total");
    expect(failed.map((finding) => finding["check_id"])).not.toContain("mug-quantity");
  });

  test("keeps the self-report and the observation distinguishable on the timeline", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await workspace.arm();

    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-timeline-cart",
    });
    await agent.call(APPLY_DISCOUNT, { code: SAVE20 });

    // §23.1: the timeline labels the tool's claim as a claim. A timeline that
    // showed one status would hide the disagreement the run is judged on.
    const completed = workspace.timeline.locator(
      "li[data-event-type='tool_invocation_completed']",
    );
    await expect(completed.first()).toContainText("reported:");
    await expect(
      workspace.timeline.locator("li[data-event-type='snapshot_captured']").first(),
    ).toBeVisible();
  });

  test("hands an agent the same findings the panel shows", async ({ workspace, agent }) => {
    await workspace.open();
    await workspace.arm();
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-findings-parity",
    });
    await agent.call(APPLY_DISCOUNT, { code: SAVE20 });
    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();

    // AC-22: the structured finding an agent reads and the panel a person reads
    // are one view of one report. Two renderings that could disagree would make
    // "who is right" a question nobody can answer from the screen.
    const result = await agent.invoke(GET_RUN_FINDINGS, { limit: 10 });
    const page = bodyOf(result);
    expect(page["overallResult"]).toBe("failed");

    const checkIds = (page["findings"] as Record<string, unknown>[]).map(
      (finding) => finding["checkId"],
    );
    expect(checkIds).toContain("discounted-total");

    for (const checkId of checkIds) {
      await expect(workspace.findings).toContainText(String(checkId));
    }
  });
});

test.describe("the same journey without the fault", () => {
  test("passes when the store applies the discount it reports", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
    // `post_fix` keeps the profile recorded as the comparison fault and lets the
    // adapter disable it (FR-011), which is what makes a matched pair differ in
    // exactly one variable.
    await harness.setFailureProfile(DISCOUNT_FAULT);
    await harness.setScenarioMode("post_fix");

    await workspace.open();
    await workspace.arm();
    await agent.call(SEARCH_CATALOG, { query: "mug" });
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-post-fix-cart",
    });
    await agent.call(APPLY_DISCOUNT, { code: SAVE20 });
    await agent.call(VERIFY_OUTCOME);

    await workspace.expectTerminalPhase();
    // The counterfactual that makes the failing run mean something: the same
    // contract, the same calls, and a pass.
    await expect(workspace.findings).toContainText("Result:");
    await expect(workspace.findings).not.toContainText("false_success_or_state_mismatch");
    await workspace.expectPhase("passed");
  });
});

/** The run id the workspace is pointing at, preferring the one already read. */
async function currentRunId(
  harness: { workspace(): Promise<Record<string, unknown>> },
  fallback: string,
): Promise<string> {
  const status = await harness.workspace();
  const active = status["active_run"];
  if (typeof active === "object" && active !== null) {
    const id = (active as Record<string, unknown>)["id"];
    if (typeof id === "string") {
      return id;
    }
  }
  return fallback;
}
