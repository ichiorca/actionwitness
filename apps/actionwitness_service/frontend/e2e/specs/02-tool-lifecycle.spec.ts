/**
 * §11.5's tool lifecycle, read from the browser's registry (FR-003, FR-167).
 *
 * The vitest suite covers this against a double it also owns. What it cannot
 * cover is the property FR-167 actually states: reconciliation must come from
 * `getTools()` rather than from component mount state. Against a double those
 * two agree by construction. Against a real registry — one another script on the
 * origin can also write to — they can disagree, and this is where that shows.
 *
 * Every assertion here reads the registry or the rendered page. Nothing reaches
 * into React.
 */

import {
  APPLY_DISCOUNT,
  ARM_OUTCOME_CONTRACT,
  GET_CART,
  GET_RUN_FINDINGS,
  GET_WORKSPACE_STATUS,
  LIST_CONTRACT_TEMPLATES,
  MUG,
  PROCEED_TO_CHECKOUT,
  RESET_WORKSPACE,
  SEARCH_CATALOG,
  UPDATE_CART,
  VERIFY_OUTCOME,
  expect,
  test,
} from "../support/harness";

const TARGET_TOOLS = [SEARCH_CATALOG, GET_CART, UPDATE_CART, APPLY_DISCOUNT, PROCEED_TO_CHECKOUT];

test.describe("registration follows the server's phase", () => {
  test("publishes only the always-available tools before a run is armed", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await workspace.expectPhase("contract_ready");

    await agent.expectRegistered(GET_WORKSPACE_STATUS);
    await agent.expectRegistered(LIST_CONTRACT_TEMPLATES);
    await agent.expectRegistered(ARM_OUTCOME_CONTRACT);

    // §11.2: target tools require an armed or running run. Offering one here
    // would invite an agent into an action the server refuses.
    const names = await agent.toolNames();
    for (const tool of TARGET_TOOLS) {
      expect(names, `${tool} must not be registered before arming`).not.toContain(tool);
    }
    // §11.1: verification needs a run, findings need a terminal one.
    expect(names).not.toContain(VERIFY_OUTCOME);
    expect(names).not.toContain(GET_RUN_FINDINGS);
  });

  test("publishes the target tools once a run is armed, and retires arming", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await workspace.arm();
    await workspace.expectPhase("armed");

    for (const tool of TARGET_TOOLS) {
      await agent.expectRegistered(tool);
    }
    // A second arm would be a second run against the same contract. §11.1 makes
    // "no run active" a precondition, so the tool leaves the surface.
    await agent.expectNotRegistered(ARM_OUTCOME_CONTRACT);
    // And verification is not offered yet: `armed` means the initial
    // observation exists and nothing has happened, so there is no outcome to
    // verify. The tool arrives with the first target action.
    await agent.expectNotRegistered(VERIFY_OUTCOME);
  });

  test("publishes verification only once the agent has acted", async ({ workspace, agent }) => {
    await workspace.open();
    await workspace.arm();
    await agent.expectNotRegistered(VERIFY_OUTCOME);

    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-lifecycle-verify",
    });

    await workspace.expectPhase("running");
    await agent.expectRegistered(VERIFY_OUTCOME);
  });

  test("retires the target tools and publishes findings once the run is terminal", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await workspace.arm();
    await agent.call(UPDATE_CART, {
      product_id: MUG,
      quantity: 1,
      request_id: "e2e-lifecycle-1",
    });
    await workspace.expectPhase("running");

    await agent.call(VERIFY_OUTCOME);
    await workspace.expectTerminalPhase();

    await agent.expectRegistered(GET_RUN_FINDINGS);
    await agent.expectRegistered(RESET_WORKSPACE);
    for (const tool of TARGET_TOOLS) {
      await agent.expectNotRegistered(tool);
    }
  });

  test("unregisters everything when the page goes away", async ({ page, workspace, agent }) => {
    await workspace.open();
    await agent.expectRegistered(GET_WORKSPACE_STATUS);

    // Same-document navigation would keep the registry; a real navigation
    // discards it along with the page, which is what a `registerTool` signal
    // aborting on unmount has to survive being confused with. Re-installing the
    // agent on the new document and finding it empty is the assertion.
    await page.goto("/demo");
    await expect(page.getByRole("heading", { name: "Buggy Store", level: 1 })).toBeVisible();
    // The storefront declares no WebMCP at all (§26.7 gates the manifest and
    // the source), so nothing should have registered on this document.
    expect(await agent.toolNames()).toEqual([]);
  });
});

test.describe("the registration panel reconciles against the browser, not the components", () => {
  test("names a tool the browser reports that this page never declared", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await agent.expectRegistered(GET_WORKSPACE_STATUS);

    // A third-party script on the origin registering its own tool: exactly what
    // FR-167 says mount state cannot see.
    await agent.injectTool(
      "totally_unrelated_tool",
      "Registered by something other than this application.",
    );

    const panel = workspace.panel("Tool registration");
    await expect(panel).toContainText("Not declared by this page");
    await expect(panel).toContainText("totally_unrelated_tool");
    // The panel reports and refuses to judge: whether the surface is acceptable
    // is `stable_tool_surface`'s answer, from recorded evidence.
    await expect(panel).toContainText("stable_tool_surface");
  });

  test("renders an injected tool name as text rather than as markup", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await agent.expectRegistered(GET_WORKSPACE_STATUS);

    // The registry is writable by any script on the origin, so its contents are
    // untrusted input. A name that got interpreted would be stored XSS with an
    // unusually convenient delivery mechanism.
    await agent.injectTool("<img src=x onerror=alert(1)>", "hostile name");

    const panel = workspace.panel("Tool registration");
    await expect(panel).toContainText("<img src=x onerror=alert(1)>");
    expect(await panel.locator("img").count()).toBe(0);
  });
});

test.describe("result normalization crosses the real boundary", () => {
  test("bounds a successful result and labels a refusal as an error", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await workspace.arm();

    const ok = await agent.invoke(SEARCH_CATALOG, { query: "mug" });
    expect(ok.isError).toBeFalsy();
    expect(ok.content[0]?.type).toBe("text");

    // §11.4: a server refusal reaches the agent as `isError`, carrying the
    // envelope's message and nothing internal. `quantity: 99` is over §13.1's
    // cap of five, so the refusal comes from the store's own validation.
    const refused = await agent.invoke(UPDATE_CART, {
      product_id: MUG,
      quantity: 99,
      request_id: "e2e-over-cap",
    });
    expect(refused.isError).toBe(true);
    expect(refused.content[0]?.text ?? "").not.toContain("Traceback");
  });
});
