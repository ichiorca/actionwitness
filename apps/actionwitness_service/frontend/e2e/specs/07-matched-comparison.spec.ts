/**
 * AC-20: a matched pre/post pair, and a mismatched one that stays a valid run.
 *
 * The pair is the product's evidence that a fix worked. It only means anything
 * if the two runs differ in exactly one variable, which is why the negative case
 * matters as much as the positive one — FR-019 is explicit that a mismatched
 * rerun is still a good run, and a suite that only proved the happy pair would
 * leave the door open to "fixing" a mismatch by weakening what was tested.
 *
 * Note what the agent has to do here that a person cannot: binding a comparison
 * source is an argument to `arm_outcome_contract`, and the Arm button offers no
 * way to supply it.
 */

import {
  APPLY_DISCOUNT,
  ARM_OUTCOME_CONTRACT,
  MUG,
  SAVE20,
  SEARCH_CATALOG,
  TEMPLATE_ONE_MUG_SAVE20,
  UPDATE_CART,
  VERIFY_OUTCOME,
  expect,
  test,
} from "../support/harness";

const DISCOUNT_FAULT = "discount_reported_but_not_applied";

/** Drive the canonical journey to a verdict and return the run's id. */
async function completeJourney(
  workspace: import("../support/harness").Workspace,
  agent: import("../support/harness").Agent,
  harness: import("../support/harness").HarnessApi,
  requestId: string,
  armWith?: string,
): Promise<string> {
  if (armWith === undefined) {
    await workspace.arm();
  } else {
    await agent.call(ARM_OUTCOME_CONTRACT, { comparison_source_run_id: armWith });
  }
  // The contract's `expected_tools` names all three calls, so a journey that
  // skipped the search would fail on `missing_expected_tool` and the pair would
  // be comparing the wrong thing.
  await agent.call(SEARCH_CATALOG, { query: "mug" });
  await agent.call(UPDATE_CART, { product_id: MUG, quantity: 1, request_id: `${requestId}-cart` });
  await agent.call(APPLY_DISCOUNT, { code: SAVE20 });
  await agent.call(VERIFY_OUTCOME);
  await workspace.expectTerminalPhase();
  const status = await harness.workspace();
  return String((status["active_run"] as Record<string, unknown>)["id"]);
}

test.describe("a matched pre/post pair", () => {
  test("differs in scenario mode alone, and reports what the fix resolved", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
    await harness.setFailureProfile(DISCOUNT_FAULT);
    await harness.setScenarioMode("pre_fix");

    await workspace.open();
    const failing = await completeJourney(workspace, agent, harness, "e2e-pair-pre");
    await workspace.expectPhase("failed");

    // A reset that *keeps* completed runs: the failing run is the evidence the
    // second one is compared against, and purging it would delete the point.
    await harness.reset(false);
    // FR-011 keeps the profile recorded as the comparison fault and lets the
    // adapter disable it — one variable, changed on purpose.
    await harness.setScenarioMode("post_fix");
    // A second sitting: same operator, same workspace, its own request budget.
    await workspace.newSession();
    await workspace.open();

    const passing = await completeJourney(workspace, agent, harness, "e2e-pair-post", failing);
    expect(passing).not.toBe(failing);
    await workspace.expectPhase("passed");

    const panel = workspace.panel("Comparison");
    await expect(panel).toContainText("differ only in scenario mode");
    await expect(panel).toContainText("Resolved:");
    await expect(panel).toContainText("false_success_or_state_mismatch");
    await expect(panel).toContainText("Introduced:");
    await expect(panel).toContainText("nothing");

    // §15.3: the source run is not rewritten by being compared against.
    const source = await harness.run(failing);
    expect(source["overall_result"]).toBe("failed");
    expect(source["comparison_source_run_id"]).toBeNull();
  });

  test("refuses to compare two runs that differ in more than the mode", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
    await harness.setFailureProfile(DISCOUNT_FAULT);
    await harness.setScenarioMode("pre_fix");

    await workspace.open();
    const first = await completeJourney(workspace, agent, harness, "e2e-mismatch-1");

    await harness.reset(false);
    // A different contract *and* a different mode: two variables, so the pair
    // says nothing about the fix.
    await harness.selectTemplate("retry_safe_cart_update");
    await harness.setScenarioMode("post_fix");
    await workspace.newSession();
    await workspace.open();

    await agent.call(ARM_OUTCOME_CONTRACT, { comparison_source_run_id: first });
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 2,
      request_id: "e2e-mismatch-2-cart",
    });
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 2,
      request_id: "e2e-mismatch-2-cart",
    });
    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();

    const panel = workspace.panel("Comparison");
    // FR-019: still a valid run with its own verdict; it simply is not the
    // other one's counterpart.
    await expect(panel).toContainText("cannot be compared");
    await expect(panel).toContainText("Differs in:");
    // The comparison names the *content* that differs rather than the row id:
    // two runs against re-seeded copies of the same template share no id, so an
    // id comparison would call every pair incomparable.
    await expect(panel).toContainText("contract_content_hash");
    await workspace.expectPhase("passed");
  });
});

test.describe("a run armed without a source", () => {
  test("says so rather than showing an empty comparison", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
    await harness.setFailureProfile(null);
    await workspace.open();
    await completeJourney(workspace, agent, harness, "e2e-no-source");

    // "No pair exists" is not an error the user has to dismiss, and it is not
    // an empty comparison either.
    await expect(workspace.panel("Comparison")).toContainText(
      "not armed against a comparison source",
    );
    expect(await workspace.alerts.count()).toBe(0);
  });
});
