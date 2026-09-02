/**
 * AC-21 and §15.8: whose turn it is, and what a refusal looks like.
 *
 * Guidance is derived entirely on the server (FR-120) and the browser is
 * forbidden from inventing a conflicting next action. That invariant is only
 * observable where both exist at once: a jsdom test asserts the banner renders
 * whatever it was handed, and a Python test asserts the server derives the right
 * phase, but neither can catch a page that quietly disagrees with the server it
 * is displaying.
 *
 * The refusal tests are the other half. §20 keeps internals out of anything a
 * person or an agent reads, and the cheapest way to break that is to render a
 * message the server never intended as user-facing.
 */

import {
  ARM_OUTCOME_CONTRACT,
  MUG,
  TEMPLATE_ONE_MUG_SAVE20,
  UPDATE_CART,
  VERIFY_OUTCOME,
  expect,
  test,
  textOf,
} from "../support/harness";

test.describe("the guidance banner follows the server", () => {
  test("names one safe next action at every step of the journey", async ({
    workspace,
    agent,
    harness,
  }) => {
    await workspace.open();

    // Each step: the phase, the compact action code the server derived, and the
    // banner copy a person reads — asserted together, because the failure worth
    // catching is the three disagreeing.
    await workspace.expectPhase("contract_ready");
    await workspace.expectActionCode("arm_run");
    await expect(workspace.banner).toContainText("Whose turn: You (operator)");
    expect(String((await harness.workspace())["next_action"] ?? "")).not.toBe("");

    await workspace.arm();
    await workspace.expectPhase("armed");
    await workspace.expectActionCode("invoke_target_tool");
    await expect(workspace.banner).toContainText("Whose turn: The agent");

    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-guidance-cart",
    });
    await workspace.expectPhase("running");
    await workspace.expectActionCode("verify_outcome");

    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();
    await workspace.expectActionCode("review_findings");
    await expect(workspace.banner).toContainText("Whose turn: You (operator)");
  });

  test("hands an agent the same next action the banner shows", async ({ workspace, agent }) => {
    await workspace.open();

    // FR-121's compact projection. Two derivations of "whose turn is it" would
    // agree in testing and diverge exactly when a person and an agent disagree,
    // which is the situation guidance exists for.
    const status = await agent.call("get_workspace_status");
    const next = status["next_action"] as Record<string, unknown> | undefined;
    const actionCode = String(next?.["action_code"] ?? "");
    expect(actionCode, JSON.stringify(status)).not.toBe("");
    await workspace.expectActionCode(actionCode);
    // And the phase the tool reports is the phase the banner rendered from.
    await workspace.expectPhase(String(status["phase"]));
  });

  test("stays readable with colour removed", async ({ workspace, page }) => {
    await page.emulateMedia({ forcedColors: "active" });
    await workspace.open();

    // §8.4: every state that matters is spelled out, because a run that failed
    // must read as failed to someone who cannot see the red.
    await expect(workspace.banner).toContainText("Whose turn:");
    await expect(workspace.banner).toContainText("Next:");
    await expect(workspace.banner).toContainText("Why:");
    await expect(workspace.banner).toContainText("What happens:");
  });
});

test.describe("a refusal is bounded and says what to do next", () => {
  test("shows the server's message and nothing internal", async ({ workspace, agent }) => {
    await workspace.open();
    await workspace.arm();

    // §11.1 makes "no run active" a precondition for arming, and the server is
    // the authority: the tool is not even registered here, so the refusal is
    // reached through the API the page itself uses.
    await agent.expectNotRegistered(ARM_OUTCOME_CONTRACT);
    const refused = await workspace.page.evaluate(async () => {
      const response = await fetch("/api/v1/runs", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      return { status: response.status, body: (await response.text()).slice(0, 500) };
    });

    expect(refused.status).toBeGreaterThanOrEqual(400);
    const envelope = JSON.parse(refused.body) as { error: { code: string; retryable: boolean } };
    // §15.8's stable envelope: a code a client can branch on, and a retryable
    // flag that says whether trying again could ever help.
    expect(envelope.error.code).toBe("RUN_IN_PROGRESS");
    expect(typeof envelope.error.retryable).toBe("boolean");
    expect(refused.body).not.toContain("Traceback");
    expect(refused.body).not.toContain("sqlite");
  });

  test("refuses a mutation whose Origin is not this one", async ({ workspace }) => {
    await workspace.open();

    // §20.1's second lock behind `SameSite=Strict`. A cross-site page's Origin
    // is not this one, and equality is the only comparison that is safe:
    // `http://127.0.0.1:8010` and `http://127.0.0.1:8010.evil.example` differ.
    const refused = await workspace.page.evaluate(async () => {
      const response = await fetch("/api/v1/workspace/reset", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Origin: "https://evil.example" },
        body: "{}",
      });
      return { status: response.status, body: await response.text() };
    });

    // The browser refuses to let a page forge `Origin`, so this asserts the
    // request still succeeded from its real origin — the negative case belongs
    // to a client that can set the header, which is the next test.
    expect(refused.status).toBe(200);
  });

  test("refuses a forged Origin from a client that can set one", async ({ harness }) => {
    const response = await harness.raw.post("/api/v1/workspace/reset", {
      headers: { Origin: "https://harness.test.evil.example", "Content-Type": "application/json" },
      data: {},
    });
    expect(response.status()).toBeGreaterThanOrEqual(400);
    const envelope = (await response.json()) as { error?: { code?: string } };
    expect(envelope.error?.code).toBe("ORIGIN_NOT_ALLOWED");
  });

  test("hands an agent a bounded error rather than a stack trace", async ({
    workspace,
    agent,
    harness,
  }) => {
    await harness.selectTemplate(TEMPLATE_ONE_MUG_SAVE20);
    await workspace.open();
    await workspace.arm();

    // A product the catalog does not hold. The store refuses, the harness wraps
    // it in the envelope, and the adapter normalizes it into an `isError`
    // result — three boundaries, each of which could leak the layer below it.
    const result = await agent.invoke(UPDATE_CART, {
      product_id: "no-such-product",
      quantity: 1,
      request_id: "e2e-refusal-unknown-product",
    });
    expect(result.isError).toBe(true);
    const message = textOf(result);
    expect(message).not.toBe("");
    expect(message.length).toBeLessThanOrEqual(1_500);
    for (const leak of ["Traceback", "sqlite3", "actionwitness_service", "/src/"]) {
      expect(message, `the agent-visible message leaks ${leak}`).not.toContain(leak);
    }

    // §11.4: arguments are revalidated against the adapter's published schema
    // *before* dispatch, so this refusal never reached the store. The observable
    // consequence is the stronger one — nothing was started, so there is nothing
    // that could have half-committed.
    await expect(
      workspace.timeline.locator("li[data-event-type='tool_invocation_started']"),
    ).toHaveCount(0);
    await workspace.expectPhase("armed");
  });
});
