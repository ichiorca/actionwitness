/**
 * §9.5's `idempotent_by_request_id`, both ways round.
 *
 * "Every logical mutation has a stable idempotency key. A retry reuses it only
 * for identical intent" is a constitutional rail, and the only honest way to
 * test a rail is to show it holding *and* to show what it catches. So this file
 * runs the same journey twice: once against a store that replays the first
 * result, and once against the injected profile that applies the mutation again
 * while its response stays syntactically valid.
 *
 * The pairing matters. A suite with only the passing half would be satisfied by
 * a policy that never fails, and a suite with only the failing half would be
 * satisfied by one that always does.
 */

import {
  MUG,
  TEMPLATE_RETRY_SAFE,
  UPDATE_CART,
  VERIFY_OUTCOME,
  expect,
  test,
  textOf,
} from "../support/harness";

const DUPLICATE_FAULT = "duplicate_on_retry";
const RETRY_KEY = "e2e-idempotent-retry-key";

/** Set two mugs, then repeat the identical request under the same key. */
async function setThenRetry(
  workspace: import("../support/harness").Workspace,
  agent: import("../support/harness").Agent,
): Promise<Record<string, unknown>[]> {
  await workspace.open();
  await workspace.arm();
  const first = await agent.call(UPDATE_CART, {
    product_id: MUG,
    quantity: 2,
    request_id: RETRY_KEY,
  });
  // Byte-identical intent under a byte-identical key. Appendix D.2's rule is
  // that this returns the first persisted result and does not mutate again.
  const second = await agent.call(UPDATE_CART, {
    product_id: MUG,
    quantity: 2,
    request_id: RETRY_KEY,
  });
  await agent.call(VERIFY_OUTCOME);
  await workspace.expectTerminalPhase();
  return [first, second];
}

test.describe("a correct store", () => {
  test("replays the first result and changes the cart once", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_RETRY_SAFE);
    await harness.setScenarioMode("pre_fix");
    await harness.setFailureProfile(null);

    const [first, second] = await setThenRetry(workspace, agent);

    // Both calls report success — a replay is not an error, and a client that
    // treated it as one would be taught to invent a new key, which is the
    // failure mode the rail exists to prevent.
    expect((first?.["reported"] as Record<string, unknown>)["status"]).toBe("success");
    expect((second?.["reported"] as Record<string, unknown>)["status"]).toBe("success");

    // The cart changed once. The contract asserts the quantity, the line count
    // *and* the subtotal, because a duplicate that doubled the line would still
    // leave one line.
    await workspace.expectPhase("passed");
    await expect(workspace.findings).toContainText("mug-quantity-after-retry");
    await expect(workspace.findings).toContainText("subtotal-charged-once");

    // The retry is on the record as its own invocation: the journey being
    // asserted includes it, so a timeline that coalesced the two would lose the
    // evidence the policy is judged from.
    await expect(
      workspace.timeline.locator("li[data-event-type='tool_invocation_completed']"),
    ).toHaveCount(2);
  });
});

test.describe("a store that duplicates on retry", () => {
  test("fails the run and classifies it as an idempotency violation", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_RETRY_SAFE);
    await harness.setScenarioMode("pre_fix");
    await harness.setFailureProfile(DUPLICATE_FAULT);

    await setThenRetry(workspace, agent);
    await workspace.expectPhase("failed");

    const runId = String(
      ((await harness.workspace())["active_run"] as Record<string, unknown>)["id"],
    );
    const findings = (await harness.findings(runId))["findings"] as Record<string, unknown>[];
    const classifications = findings
      .filter((finding) => finding["status"] === "failed")
      .map((finding) => finding["classification"]);

    // §17's classification, not merely "something differed": a duplicated
    // mutation and a wrong total are different defects with different fixes,
    // and a report that called both `assertion_mismatch` would say nothing
    // useful about either.
    expect(classifications, JSON.stringify(findings)).toContain("idempotency_violation");
    await expect(workspace.findings).toContainText("idempotency_violation");
  });

  test("still reports the duplicated call as a success, which is the point", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_RETRY_SAFE);
    await harness.setScenarioMode("pre_fix");
    await harness.setFailureProfile(DUPLICATE_FAULT);

    const [, second] = await setThenRetry(workspace, agent);

    // §13.3: "the tool response stays syntactically valid". The self-report is
    // the channel under test, so it must look exactly as it does when nothing
    // is wrong — the disagreement is only visible against the observation.
    expect((second?.["reported"] as Record<string, unknown>)["status"]).toBe("success");
    await expect(
      workspace.timeline.locator("li[data-event-type='tool_invocation_completed']"),
    ).toHaveCount(2);
    await expect(workspace.timeline).toContainText("reported: success");
  });
});

test.describe("the same key with different intent", () => {
  test("is refused rather than silently replayed", async ({ workspace, agent, harness }) => {
    await harness.selectTemplate(TEMPLATE_RETRY_SAFE);
    await harness.setFailureProfile(null);

    await workspace.open();
    await workspace.arm();
    await agent.call(UPDATE_CART, { product_id: MUG, quantity: 2, request_id: RETRY_KEY });

    // The constitution's rail: "key reuse with changed intent fails closed". A
    // replay here would apply the *first* quantity while the caller believed it
    // had asked for a different one — a silent divergence between what was
    // requested and what happened, which is the whole subject of this product.
    const conflicting = await agent.invoke(UPDATE_CART, {
      product_id: MUG,
      quantity: 3,
      request_id: RETRY_KEY,
    });
    // The refusal reaches the agent *as* a refusal. The route answers 200 for
    // an invocation that completed the round trip — the status is about the
    // harness — and the tool's own outcome lives in the terminal event. Until
    // the adapter read it, this resolved as an ordinary value and an agent
    // branching on `isError` was told a refused mutation had worked.
    expect(conflicting.isError, textOf(conflicting)).toBe(true);
    // The server's own summary, and the stable token to branch on.
    expect(textOf(conflicting)).toContain("already used");
    expect(textOf(conflicting)).toContain("idempotency_key_reused");

    // And the first mutation stands: a refused retry must not undo anything.
    const cart = await agent.call("get_cart");
    expect(JSON.stringify(cart)).toContain("50.00");
  });
});
