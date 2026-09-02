/**
 * AC-06 and §14: consent, in a browser, with a promise genuinely held open.
 *
 * §14.3 requires the agent's `execute` promise to stay pending while a human
 * decides. That sentence describes a relationship between three things — a
 * browser-held promise, a committed server row, and a dialog — and no existing
 * layer holds all three at once. jsdom can hold the promise but not the server;
 * pytest can hold the server but not the promise.
 *
 * So every test here asserts on the *pending* state as well as the outcome. A
 * suite that only checked "approve creates an order" would pass against an
 * implementation that resolved the promise immediately and created the order
 * afterwards — which is precisely the bug consent exists to prevent.
 */

import {
  MUG,
  PROCEED_TO_CHECKOUT,
  type ToolResult,
  TEMPLATE_CONFIRMED_CHECKOUT,
  UPDATE_CART,
  VERIFY_OUTCOME,
  expect,
  test,
  textOf,
} from "../support/harness";

test.beforeEach(async ({ harness }) => {
  await harness.selectTemplate(TEMPLATE_CONFIRMED_CHECKOUT);
  await harness.setScenarioMode("pre_fix");
  await harness.setFailureProfile(null);
});

/** Arm, put a mug in the cart, and leave checkout pending on a human. */
async function reachPendingCheckout(
  workspace: { open(): Promise<void>; arm(): Promise<void>; dialog: import("@playwright/test").Locator },
  agent: {
    call(name: string, args?: Record<string, unknown>): Promise<Record<string, unknown>>;
    start(handle: string, name: string, args?: Record<string, unknown>): Promise<void>;
  },
  handle: string,
  requestId: string,
): Promise<void> {
  await workspace.open();
  await workspace.arm();
  await agent.call(UPDATE_CART, { product_id: MUG, quantity: 1, request_id: `${requestId}-cart` });
  await agent.start(handle, PROCEED_TO_CHECKOUT, { request_id: requestId });
  await expect(workspace.dialog).toBeVisible();
}

test.describe("a protected mutation waits for a person", () => {
  test("holds the agent's promise open while the dialog is unanswered", async ({
    workspace,
    agent,
  }) => {
    await reachPendingCheckout(workspace, agent, "checkout", "e2e-confirm-approve");

    // The dialog says what will happen, in words, before anything has.
    await expect(workspace.dialog).toContainText(PROCEED_TO_CHECKOUT);
    await expect(workspace.dialog).toContainText("Nothing has changed yet");
    await expect(workspace.dialog).toContainText("Waiting for your decision");

    // §14.3: still pending. This is the assertion the whole feature turns on.
    expect(await agent.isPending("checkout")).toBe(true);
    await workspace.expectPhase("awaiting_confirmation");

    await workspace.dialog.getByRole("button", { name: "Approve once" }).click();

    const settled = await agent.settled("checkout");
    expect(settled.state, JSON.stringify(settled)).toBe("fulfilled");
    await expect(workspace.dialog).toBeHidden();

    // Approval performs the action once, and the run can then be verified
    // against a contract that requires the order to exist behind consent.
    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();
    await expect(workspace.findings).toContainText("order-created");
    await workspace.expectPhase("passed");
  });

  test("refuses the action on denial, and says so without calling it a failure", async ({
    workspace,
    agent,
    harness,
  }) => {
    await reachPendingCheckout(workspace, agent, "checkout", "e2e-confirm-deny");

    await workspace.dialog.getByRole("button", { name: "Deny" }).click();

    // §14.8 and §11.4 together: the handler throws, and the adapter normalizes
    // the throw into an `isError` result rather than a rejected promise. So the
    // invocation *settles* — an agent must not read a denial as an order, and it
    // must not read one as a transport failure either.
    const settled = await agent.settled("checkout");
    expect(settled.state, JSON.stringify(settled)).toBe("fulfilled");
    const refusal = settled.value as ToolResult;
    expect(refusal.isError).toBe(true);
    expect(textOf(refusal)).not.toBe("");

    await expect(workspace.dialog).toBeHidden();

    const status = await harness.workspace();
    const runId = String((status["active_run"] as Record<string, unknown>)["id"]);
    const events = (await harness.events(runId))["events"] as Record<string, unknown>[];
    const types = events.map((event) => event["event_type"]);
    expect(types).toContain("confirmation_denied");
    expect(types).not.toContain("confirmation_approved");
  });

  test("cancels the pending decision when the agent abandons the call", async ({
    workspace,
    agent,
    harness,
  }) => {
    await reachPendingCheckout(workspace, agent, "checkout", "e2e-confirm-cancel");

    // §14.9: the invocation is abandoned, so the request is cancelled rather
    // than left for a person to answer on nobody's behalf. This is the path
    // ADR-0002's native registration exists for — the pinned hook forwards no
    // per-invocation signal, so an agent registered through it could not do
    // this at all.
    await agent.abort("checkout");

    const settled = await agent.settled("checkout");
    expect(settled.state, JSON.stringify(settled)).toBe("fulfilled");
    const cancelled = settled.value as ToolResult;
    expect(cancelled.isError).toBe(true);
    expect(textOf(cancelled)).toContain("cancelled");

    // The dialog goes because the server cancelled the confirmation, not
    // because the page hid it: the next read of the run carries no pending
    // confirmation.
    await expect(workspace.dialog).toBeHidden();

    const status = await harness.workspace();
    const runId = String((status["active_run"] as Record<string, unknown>)["id"]);
    expect((await harness.run(runId))["pending_confirmation"]).toBeNull();
    const types = ((await harness.events(runId))["events"] as Record<string, unknown>[]).map(
      (event) => event["event_type"],
    );
    expect(types).toContain("confirmation_cancelled");
  });
});

test.describe("the dialog is operable without a mouse", () => {
  test("takes focus itself rather than preselecting a choice", async ({ workspace, agent }) => {
    await reachPendingCheckout(workspace, agent, "checkout", "e2e-confirm-focus");

    // §14.4: "no option is preselected". A focused Approve is a consent flow a
    // stray Enter completes, whatever the styling says. jsdom cannot tell a
    // focused button from an unfocused one the way a real browser can.
    await expect(workspace.dialog).toBeFocused();
    await expect(workspace.dialog.getByRole("button", { name: "Approve once" })).not.toBeFocused();
    await expect(workspace.dialog.getByRole("button", { name: "Deny" })).not.toBeFocused();

    await workspace.page.keyboard.press("Tab");
    await expect(workspace.dialog.getByRole("button", { name: "Approve once" })).toBeFocused();
    await workspace.page.keyboard.press("Tab");
    await expect(workspace.dialog.getByRole("button", { name: "Deny" })).toBeFocused();

    // The trap wraps at both ends. A trap that only caught forward Tab lets
    // Shift+Tab walk out of the modal — the same bug facing the other way.
    await workspace.page.keyboard.press("Tab");
    await expect(workspace.dialog.getByRole("button", { name: "Approve once" })).toBeFocused();
    await workspace.page.keyboard.press("Shift+Tab");
    await expect(workspace.dialog.getByRole("button", { name: "Deny" })).toBeFocused();

    await agent.abort("checkout");
  });

  test("can be answered from the keyboard alone", async ({ workspace, agent }) => {
    await reachPendingCheckout(workspace, agent, "checkout", "e2e-confirm-keyboard");

    await workspace.page.keyboard.press("Tab");
    await workspace.page.keyboard.press("Enter");

    const settled = await agent.settled("checkout");
    expect(settled.state).toBe("fulfilled");
  });

  test("names the actor and the consequence without relying on colour", async ({
    workspace,
    agent,
  }) => {
    await reachPendingCheckout(workspace, agent, "checkout", "e2e-confirm-copy");

    // AC-21: who is acting, what is being asked, and what it will cost, all as
    // text. Rendered in a monochrome emulation so anything carried only by
    // colour disappears and the assertions have to survive it.
    await workspace.page.emulateMedia({ forcedColors: "active" });

    await expect(workspace.dialog).toContainText("Approve this action?");
    await expect(workspace.dialog).toContainText("The agent wants to:");
    await expect(workspace.dialog).toContainText("Expires at:");
    await expect(workspace.dialog).toContainText("What it affects:");
    // The guidance banner agrees about whose turn it is.
    await workspace.expectActionCode("decide_confirmation");

    await agent.abort("checkout");
  });
});

test.describe("a second tab", () => {
  test("shows the pending decision read-only and offers no controls", async ({
    browser,
    workspace,
    agent,
    clientAddress,
  }) => {
    await reachPendingCheckout(workspace, agent, "checkout", "e2e-confirm-second-tab");

    // §14.14: the same confirmation and the same authority — what the second
    // tab lacks is the pending promise, so a decision made there would resolve
    // nothing here. Same cookie jar, so it is the same workspace.
    const second = await browser.newContext({
      storageState: await workspace.page.context().storageState(),
      extraHTTPHeaders: { "X-Forwarded-For": clientAddress },
      baseURL: workspace.page.url().replace(/\/$/, ""),
    });
    try {
      const other = await second.newPage();
      await other.goto("/");
      const dialog = other.getByRole("dialog");
      await expect(dialog).toBeVisible();
      await expect(dialog).toContainText("pending in another tab");
      expect(await dialog.getByRole("button", { name: "Approve once" }).count()).toBe(0);
      expect(await dialog.getByRole("button", { name: "Deny" }).count()).toBe(0);
    } finally {
      await second.close();
    }

    await agent.abort("checkout");
  });
});
