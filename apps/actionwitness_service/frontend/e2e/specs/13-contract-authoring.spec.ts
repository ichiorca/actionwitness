/**
 * §25.2 and FR-021: the flat declarative form, driven by a person.
 *
 * This form is the third registration mechanism and the only one with no
 * registration call — in a WebMCP browser the tool exists because the markup
 * does. That half belongs to the manual checklist (§26.4): the substitute
 * registry this lane installs implements `registerTool`, `getTools` and
 * `toolchange`, not the browser's declarative form scanning, and pretending
 * otherwise would be this suite testing its own double.
 *
 * What *is* testable here, and untested anywhere else, is everything the form
 * does against the real server: a template expanded into an immutable contract,
 * a refusal mapped back onto the control that caused it, and the allowlist that
 * decides which controls a template even accepts.
 */

import { TEMPLATE_CONFIRMED_CHECKOUT, TEMPLATE_ONE_MUG_SAVE20, expect, test } from "../support/harness";

test.describe("creating a contract from a built-in template", () => {
  test("expands the template and leaves the workspace pointing at the result", async ({
    workspace,
    harness,
  }) => {
    await workspace.open();
    const form = workspace.panel("Create a contract");

    await form.getByLabel("Template").selectOption(TEMPLATE_ONE_MUG_SAVE20);
    await form.getByLabel("Name (optional)").fill("e2e authored contract");
    await form.getByLabel("Quantity").fill("2");
    await form.getByRole("button", { name: "Create contract" }).click();

    // The contract is immutable and identified by its content hash, so the
    // confirmation names both — a display name alone would not tell two
    // expansions of the same template apart.
    await expect(form.getByRole("status")).toContainText("e2e authored contract");
    await expect(form.getByRole("status")).toContainText("sha256:");

    // §6.3 steps 6–7: creating a contract and leaving the workspace pointing at
    // the old one would end the journey a step short of where the form promised
    // to take it.
    await workspace.expectPhase("contract_ready");
    await workspace.expectActionCode("arm_run");
    const selected = String((await harness.workspace())["selected_contract_id"]);
    expect(selected).not.toBe("");
    expect(selected.startsWith("tpl_")).toBe(false);
  });

  test("arms and verifies the contract it just authored", async ({ workspace, agent }) => {
    await workspace.open();
    const form = workspace.panel("Create a contract");
    await form.getByLabel("Template").selectOption(TEMPLATE_ONE_MUG_SAVE20);
    await form.getByLabel("Quantity").fill("2");
    await form.getByRole("button", { name: "Create contract" }).click();
    await expect(form.getByRole("status")).toContainText("sha256:");

    // The authored contract is a real contract: the parameter it was expanded
    // with is what the run is judged against, so two mugs must be what it wants.
    await workspace.arm();
    await agent.call("search_catalog", { query: "mug" });
    await agent.call("update_cart", {
      product_id: "mug-ceramic-001",
      quantity: 2,
      request_id: "e2e-authored-cart",
    });
    await agent.call("apply_discount", { code: "SAVE20" });
    await agent.call("verify_outcome");
    await workspace.expectTerminalPhase();
    await workspace.expectPhase("passed");
  });
});

test.describe("the form's limits are the server's", () => {
  test("declares the bound on the control, and the browser holds it", async ({ workspace }) => {
    await workspace.open();
    const form = workspace.panel("Create a contract");

    await form.getByLabel("Template").selectOption(TEMPLATE_ONE_MUG_SAVE20);
    const quantity = form.getByLabel("Quantity");
    await expect(quantity).toHaveAttribute("max", "5");
    await expect(quantity).toHaveAttribute("min", "1");

    // Past §13.1's cap. Native constraint validation refuses the submission, so
    // the request is never made — the courtesy layer doing its job.
    await quantity.fill("9");
    await form.getByRole("button", { name: "Create contract" }).click();
    expect(await quantity.evaluate((node: HTMLInputElement) => node.validity.rangeOverflow)).toBe(
      true,
    );
    await expect(form.getByRole("status")).not.toContainText("sha256:");
    await workspace.expectPhase("contract_ready");
  });

  test("is a courtesy — the rule is the server's", async ({ workspace, harness }) => {
    await workspace.open();

    // The same value, submitted the way anything that bypasses the control
    // would: an agent, a script, a client with its own idea of the schema.
    // §15.2 re-checks the allowlist because "a browser deciding which fields
    // are legal would be the client authorizing its own input".
    const response = await harness.raw.post("/api/v1/contracts", {
      data: { template_id: TEMPLATE_ONE_MUG_SAVE20, quantity: 9 },
    });
    expect(response.status()).toBeGreaterThanOrEqual(400);
    const envelope = (await response.json()) as {
      error?: { code?: string; details?: { path?: string }[] };
    };
    expect(envelope.error?.code).toBe("CONTRACT_VALIDATION_FAILED");
    // §15.8's details name the control, which is what lets the form put the
    // message beside the field rather than in a banner the reader has to map
    // back themselves.
    expect(envelope.error?.details?.map((detail) => detail.path)).toContain("quantity");
  });

  test("refuses a scalar the selected template does not allowlist", async ({ harness }) => {
    // `confirmed_checkout_only` allowlists nothing: quantity says nothing about
    // whether an order needed an approval. Refused rather than ignored, because
    // a caller told their contract was created would otherwise believe it
    // constrained something the template never mentions.
    const response = await harness.raw.post("/api/v1/contracts", {
      data: { template_id: TEMPLATE_CONFIRMED_CHECKOUT, quantity: 2 },
    });
    expect(response.status()).toBeGreaterThanOrEqual(400);
    const envelope = (await response.json()) as { error?: { code?: string } };
    expect(envelope.error?.code).toBe("CONTRACT_VALIDATION_FAILED");
  });

  test("refuses a body carrying anything but the four flat scalars", async ({ harness }) => {
    // FR-021: "the declarative form shall never accept nested assertions,
    // policies, paths, or arbitrary JSON". A model that ignored unknown keys
    // would accept an `assertions` array and create a contract the submitter
    // believes contains it.
    const response = await harness.raw.post("/api/v1/contracts", {
      data: {
        template_id: TEMPLATE_ONE_MUG_SAVE20,
        assertions: [{ path: "target.cart.total", operator: "equals", value: "0.00" }],
      },
    });
    expect(response.status()).toBeGreaterThanOrEqual(400);
    const envelope = (await response.json()) as { error?: { code?: string } };
    expect(envelope.error?.code).toBe("CONTRACT_VALIDATION_FAILED");
  });

  test("disables the controls a template does not accept, and says why", async ({ workspace }) => {
    await workspace.open();
    const form = workspace.panel("Create a contract");

    // `confirmed_checkout_only` allowlists no scalars: quantity and discount say
    // nothing about whether an order needed an approval.
    await form.getByLabel("Template").selectOption(TEMPLATE_CONFIRMED_CHECKOUT);

    await expect(form.getByLabel("Quantity")).toBeDisabled();
    await expect(form.getByLabel("Discount code")).toBeDisabled();
    // Disabled rather than hidden, with the reason visible: removing the control
    // would change the declarative tool's shape as the selection changed, so an
    // agent that read the form once could hold a schema the page no longer
    // offers.
    await expect(form).toContainText("This template does not use a quantity");
    await expect(form).toContainText("This template does not use a discount");

    // A template that does use them turns them back on.
    await form.getByLabel("Template").selectOption(TEMPLATE_ONE_MUG_SAVE20);
    await expect(form.getByLabel("Quantity")).toBeEnabled();
    await expect(form.getByLabel("Discount code")).toBeEnabled();
  });

  test("offers only allowlisted discount codes", async ({ workspace }) => {
    await workspace.open();
    const form = workspace.panel("Create a contract");
    await form.getByLabel("Template").selectOption(TEMPLATE_ONE_MUG_SAVE20);

    // FR-021 keeps the declarative path to allowlisted scalars. A free-text
    // field here would be the one place an agent could put arbitrary content
    // into a contract.
    const options = await form.getByLabel("Discount code").locator("option").allTextContents();
    expect(options).toEqual(["Template default", "SAVE20"]);
  });
});

test.describe("the declarative tool is declared but never claimed", () => {
  test("counts the form's tool as declared without reporting it registered", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();
    await agent.expectRegistered("get_workspace_status");

    // FR-003's reconciliation has three inputs, and this is the third: a tool
    // this page *declares* through markup and never *claims* to have
    // registered. The panel must not report it missing — nothing registered it,
    // so nothing failed — and it must not be silently dropped either.
    await workspace.showAdministration();
    const panel = workspace.panel("Tool registration");
    await expect(panel).toContainText("Harness tools:");
    await expect(panel).not.toContainText("claimed but not reported");
    expect(await agent.toolNames()).not.toContain("create_outcome_contract");
  });
});
