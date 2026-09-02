/**
 * AC-08/AC-12/AC-15 from the browser: a failed run becomes a portable case.
 *
 * The eval machinery is covered thoroughly in Python and through the CLI. What
 * is not covered anywhere else is the browser half of the journey §26.4 asks an
 * operator to walk by hand: fail a run, cut a case from it with the agent tool
 * the workspace publishes, and replay it — with the case generated from evidence
 * this browser actually produced rather than from a fixture.
 *
 * Both halves of the journey are exercised here, because they used to be one:
 * `run_regression_eval` is declared `enabled: evalCaseId !== null`, and `App`
 * supplied no case id, so the tool could never register — an agent could cut a
 * case and then had no way to replay it. The panel that gives a person the same
 * two actions was exported, unit-tested, and never rendered either.
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
  textOf,
} from "../support/harness";

const DISCOUNT_FAULT = "discount_reported_but_not_applied";

/**
 * The content hash of the case cut from the run this workspace is pointing at.
 *
 * By source run rather than by position: cases accumulate here, and §15.4's
 * `ORDER BY created_at DESC` ties between any two cut in the same second.
 */
async function createdCaseHash(
  harness: import("../support/harness").HarnessApi,
): Promise<string> {
  const runId = String(
    ((await harness.workspace())["active_run"] as Record<string, unknown>)["id"],
  );
  const response = await harness.raw.get("/api/v1/evals");
  expect(response.ok(), await response.text()).toBeTruthy();
  const body = (await response.json()) as {
    cases?: { content_hash: string; source_run_id: string }[];
  };
  const match = (body.cases ?? []).find((entry) => entry.source_run_id === runId);
  expect(match, `no case was cut from ${runId}`).toBeDefined();
  return match?.content_hash ?? "";
}

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
  test("publishes neither tool before there is a failure to reproduce", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();

    // Stated rather than assumed. FR-013's purge removes terminal runs and
    // their artifacts, not the cases cut from them, so cases accumulate across
    // this file — and every later test has to tolerate that. This one needs an
    // empty list, so it says so and fails legibly rather than mysteriously if a
    // future spec starts leaving one behind.
    await expect(
      workspace.panel("Regression evals").locator("li"),
      "this test needs a workspace with no regression cases yet",
    ).toHaveCount(0);

    // FR-080: only a failed or warning-bearing run can produce a case. Offering
    // the tool earlier would invite an agent to ask for something the server
    // refuses.
    await agent.expectNotRegistered(CREATE_REGRESSION_EVAL);
    // And nothing to replay, so nothing offers to.
    await agent.expectNotRegistered("run_regression_eval");

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

  test("publishes the replay tool once a case exists, and replays through it", async ({
    workspace,
    agent,
  }) => {
    await failedRun(workspace, agent, "e2e-eval-replay-tool");

    // A second sitting before the expensive part. A replay restores a fixture
    // and re-runs a journey server-side, and the page keeps polling throughout,
    // so a full journey *and* a replay on one client's FR-009 allowance runs it
    // out — and the first casualty is the page going stale mid-assertion.
    await workspace.newSession();

    await agent.call(CREATE_REGRESSION_EVAL);

    // AC-22 measures "every capability is reachable by tool" against the §11.1
    // table. An agent that can cut a case and not replay it has half of one.
    await agent.expectRegistered("run_regression_eval");
    const replay = await agent.invoke("run_regression_eval", {
      environment: "reproduce_source",
    });
    expect(replay.isError, textOf(replay)).toBeFalsy();

    // Asserted on the text rather than parsed. §11.4 bounds a tool result at
    // 1,500 characters and grants only `get_run_findings` the larger budget, so
    // a replay report arrives truncated — deliberately. The two fields that
    // matter lead the document, and the full report is a fetch away.
    const text = textOf(replay);
    // §24.3 again, through the tool this time: a reproduced failure is a
    // *passing* replay whose target *failed*.
    expect(text).toContain('"status":"passed"');
    expect(text).toContain('"overall_result":"failed"');
  });
});

test.describe("the same two actions, without an agent", () => {
  test("lists the case and replays it from the panel", async ({ workspace, agent, harness }) => {
    await failedRun(workspace, agent, "e2e-eval-human-path");
    // A second sitting, for the same reason as above: a replay is a long
    // server-side operation and the page polls throughout it.
    await workspace.newSession();

    const panel = workspace.panel("Regression evals");
    // FR-080 gates the control on an eligible run, and this one failed.
    const create = panel.getByRole("button", { name: /Create a regression/ });
    await expect(create).toBeEnabled();
    await create.click();

    // The case, identified the way it is stored — by content hash, not by name.
    await expect(panel).toContainText("sha256:");

    // Scoped by content hash rather than by position. Cases accumulate across
    // this file — FR-013's purge removes terminal runs, not the cases cut from
    // them — and `ORDER BY created_at DESC` ties between cases cut in the same
    // second, so "the first row" is not stably the one this test just made.
    const hash = await createdCaseHash(harness);
    const row = panel.locator("li").filter({ hasText: hash });
    await expect(row).toHaveCount(1);
    await row.getByRole("button", { name: "Replay against source" }).click();

    // Two labelled facts, never one merged verdict: a UI that showed a single
    // number would report the product's best evidence as a broken build.
    await expect(row).toContainText("Eval:");
    await expect(row).toContainText("Target outcome:");
    await expect(row).toContainText("reproduce_source");
  });

  test("does not offer to cut a case from a run that passed", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.setFailureProfile(null);
    await workspace.open();
    await workspace.arm();
    await agent.call(SEARCH_CATALOG, { query: "mug" });
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-eval-passing-cart",
    });
    await agent.call(APPLY_DISCOUNT, { code: SAVE20 });
    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();
    await workspace.expectPhase("passed");

    // A passing run has no failure to reproduce. The server refuses; this keeps
    // the control out of the way rather than inviting the refusal.
    await expect(
      workspace.panel("Regression evals").getByRole("button", { name: /Create a regression/ }),
    ).toBeDisabled();
  });
});
