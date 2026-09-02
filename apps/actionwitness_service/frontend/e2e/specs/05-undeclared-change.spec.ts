/**
 * AC-24: every declared assertion passes and the run still fails.
 *
 * §9.10's argument is that a contract should constrain what a journey may touch
 * rather than enumerate what must hold, because the interesting damage is
 * always at a path nobody thought to name. Proving that end to end needs the
 * whole stack: a real tool call that quietly rewrites a saved preference, a
 * full-state comparison against the armed snapshot, and a panel that shows the
 * path in front of a person.
 *
 * The waiver test is the other half. A finding that can be silenced without
 * leaving a trace is a finding that will be silenced.
 */

import {
  MUG,
  TEMPLATE_NO_SIDE_EFFECTS,
  UPDATE_CART,
  VERIFY_OUTCOME,
  expect,
  test,
} from "../support/harness";

const SIDE_EFFECT_FAULT = "undeclared_side_effect";

test.beforeEach(async ({ harness }) => {
  await harness.selectTemplate(TEMPLATE_NO_SIDE_EFFECTS);
  await harness.setScenarioMode("pre_fix");
  await harness.setFailureProfile(SIDE_EFFECT_FAULT);
});

test("fails only the undeclared-change policy, and names the path", async ({
  workspace,
  agent,
  harness,
}) => {
  await workspace.open();
  await workspace.arm();

  // One correct mutation. §13.3's injector performs it correctly *and* rewrites
  // a preference the contract never mentions.
  await agent.call(UPDATE_CART, {
    product_id: MUG,
    quantity: 1,
    request_id: "e2e-undeclared-cart",
  });
  await agent.call(VERIFY_OUTCOME);
  await workspace.expectTerminalPhase();

  // Every cart assertion held: the mutation itself was right.
  const status = await harness.workspace();
  const runId = String((status["active_run"] as Record<string, unknown>)["id"]);
  const findings = (await harness.findings(runId))["findings"] as Record<string, unknown>[];

  const byId = new Map(findings.map((finding) => [finding["check_id"], finding]));
  expect(byId.get("mug-quantity")?.["status"], JSON.stringify(findings)).toBe("passed");
  expect(byId.get("cart-has-one-line")?.["status"]).toBe("passed");
  // And the run failed anyway.
  expect(byId.get("no_undeclared_changes")?.["status"]).toBe("failed");
  await workspace.expectPhase("failed");

  // The panel names the path a reviewer reading the assertion list would have
  // called clean.
  const panel = workspace.panel("Changed outside contract");
  await expect(panel).toContainText("failed");
  await expect(panel).toContainText("path");
  await expect(panel).toContainText("preferences");
});

test("says nothing changed outside the contract when no fault is injected", async ({
  workspace,
  agent,
  harness,
}) => {
  await harness.setFailureProfile(null);

  await workspace.open();
  await workspace.arm();
  await agent.call(UPDATE_CART, {
    product_id: MUG,
    quantity: 1,
    request_id: "e2e-undeclared-clean",
  });
  await agent.call(VERIFY_OUTCOME);
  await workspace.expectTerminalPhase();

  // The counterfactual. Without it, the failing test above would be consistent
  // with a policy that fails every run.
  const panel = workspace.panel("Changed outside contract");
  await expect(panel).toContainText("Nothing changed outside what this contract declared");
  await workspace.expectPhase("passed");
});
