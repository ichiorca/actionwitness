/**
 * AC-08/AC-12/AC-15 from the browser: a failed run becomes a portable case.
 *
 * The eval machinery is covered thoroughly in Python and through the CLI. What
 * is not covered anywhere else is the browser half of the journey §26.4 asks an
 * operator to walk by hand: fail a run, cut a case from it with the agent tool
 * the workspace publishes, and replay it — with the case generated from evidence
 * this browser actually produced rather than from a fixture.
 *
 * The replay is driven through the API rather than through a tool, and that is
 * a finding rather than a shortcut: `run_regression_eval` is declared with
 * `enabled: evalCaseId !== null`, and `App` never supplies an `evalCaseId`, so
 * the tool cannot register in the shipped page. The test asserts what is
 * actually true and says why.
 */

import {
  APPLY_DISCOUNT,
  CREATE_REGRESSION_EVAL,
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

test.beforeEach(async ({ harness }) => {
  await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
  await harness.setScenarioMode("pre_fix");
  await harness.setFailureProfile(DISCOUNT_FAULT);
});

/** Fail the canonical journey, so there is something to reproduce. */
async function failedRun(
  workspace: import("../support/harness").Workspace,
  agent: import("../support/harness").Agent,
  requestId: string,
): Promise<void> {
  await workspace.open();
  await workspace.arm();
  // The whole journey the contract expects. Skipping the search would add
  // `missing_expected_tool` to the failure, and the case would then be
  // reproducing two defects rather than the one under test.
  await agent.call(SEARCH_CATALOG, { query: "mug" });
  await agent.call(UPDATE_CART, { product_id: MUG, quantity: 1, request_id: `${requestId}-cart` });
  await agent.call(APPLY_DISCOUNT, { code: SAVE20 });
  await agent.call(VERIFY_OUTCOME);
  await workspace.expectTerminalPhase();
  await workspace.expectPhase("failed");
}

test.describe("cutting a case from a failure", () => {
  test("publishes the tool only once there is a failure to reproduce", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    // FR-080: only a failed or warning-bearing run can produce a case. Offering
    // the tool earlier would invite an agent to ask for something the server
    // refuses.
    await agent.expectNotRegistered(CREATE_REGRESSION_EVAL);

    await failedRun(workspace, agent, "e2e-eval-eligibility");
    await agent.expectRegistered(CREATE_REGRESSION_EVAL);
  });

  test("generates a self-contained case, and repeats return the same one", async ({
    workspace,
    agent,
  }) => {
    await failedRun(workspace, agent, "e2e-eval-create");

    const created = await agent.call(CREATE_REGRESSION_EVAL);
    expect(created["created"]).toBe(true);
    const caseId = String(created["eval_case_id"]);
    expect(caseId).not.toBe("");
    // The case is traceable to the failure it came from, and carries its own
    // content hash — the two properties that make it portable rather than a
    // pointer into this workspace.
    expect(String(created["source_run_id"])).not.toBe("");
    expect(String(created["content_hash"])).toMatch(/^sha256:/);

    // FR-080: an identical repeat returns the existing case. Answering a repeat
    // with a conflict would teach a client to treat idempotence as an error.
    const again = await agent.call(CREATE_REGRESSION_EVAL);
    expect(again["created"]).toBe(false);
    expect(String(again["eval_case_id"])).toBe(caseId);
    expect(String(again["content_hash"])).toBe(String(created["content_hash"]));
  });

  test("replays the case and reproduces the same classification", async ({
    workspace,
    agent,
    harness,
  }) => {
    await failedRun(workspace, agent, "e2e-eval-replay");
    const caseId = String((await agent.call(CREATE_REGRESSION_EVAL))["eval_case_id"]);

    // Replayed through the API because the page publishes no tool for it — see
    // this file's header. The replay itself is the product behaviour under
    // test: a restored fixture, an isolated workspace, and the same verdict.
    const response = await harness.raw.post(`/api/v1/evals/${caseId}/runs`, {
      data: { environment: "reproduce_source" },
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    const replay = (await response.json()) as Record<string, unknown>;

    // §24.3: a reproduced failure is a *passing* eval whose target *failed*.
    // Collapsing those two into one number would report the product's best
    // evidence as a broken build.
    expect(replay["status"]).toBe("passed");
    expect(JSON.stringify(replay)).toContain("false_success_or_state_mismatch");
  });

  test("does not publish the replay tool, because the page never names a case", async ({
    workspace,
    agent,
  }) => {
    await failedRun(workspace, agent, "e2e-eval-replay-tool");
    await agent.call(CREATE_REGRESSION_EVAL);

    // Recorded as a fact about the shipped surface rather than as an
    // aspiration: `run_regression_eval` is declared `enabled: evalCaseId !==
    // null` and `App` passes no `evalCaseId`, so the tool can never register.
    // AC-22 measures "every capability is reachable by tool" against the §11.1
    // table, and this is the gap.
    await agent.expectNotRegistered("run_regression_eval");
  });
});
