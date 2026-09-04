/**
 * AC-01: the application loads and reports WebMCP support status.
 *
 * Nothing below the browser is new here — the Python suite covers every route
 * and the vitest suite covers every component. What is new is that this is the
 * *composed* application: one origin serving the harness bundle, its API, the
 * storefront bundle, and the store's API through a proxy, with the workspace
 * cookie and the security headers a browser actually enforces. Those only exist
 * in the deployment shape, and until this lane nothing exercised it.
 */

import { HARNESS_URL } from "../../playwright.config";
import { expect, test } from "../support/harness";

test.describe("the composed one-origin deployment", () => {
  test("serves the harness at / and reports its own health", async ({ workspace, harness }) => {
    await workspace.open();

    await expect(workspace.page).toHaveTitle("ActionWitness — a WebMCP outcome witness");

    const health = await harness.raw.get("/healthz");
    expect(health.ok()).toBeTruthy();
    const body = (await health.json()) as Record<string, unknown>;
    // §29.1: an operator staring at a blank page needs to tell "no bundle in
    // the image" from "the bundle failed to load". This is that distinction.
    expect(body["assets_mounted"]).toBe(true);
  });

  test("serves the storefront at /demo from the same origin", async ({ page }) => {
    await page.goto("/demo");
    await expect(page).toHaveTitle("Buggy Store");
    await expect(page.getByRole("heading", { name: "Buggy Store", level: 1 })).toBeVisible();
  });

  test("applies the security headers to the document and to the API", async ({ harness }) => {
    for (const path of ["/", "/api/v1/workspace"]) {
      const response = await harness.raw.get(path);
      const headers = response.headers();
      expect(headers["x-content-type-options"], path).toBe("nosniff");
      expect(headers["x-frame-options"], path).toBe("DENY");
      expect(headers["referrer-policy"], path).toBe("same-origin");
      // The WebMCP permissions policy: tools are exposed to this document and
      // to nothing it embeds.
      expect(headers["permissions-policy"], path).toBe("tools=(self)");
      expect(headers["content-security-policy"], path).toContain("default-src");
    }
  });

  test("issues an HttpOnly, SameSite=Strict workspace cookie scoped to this origin", async ({
    context,
    workspace,
  }) => {
    await workspace.open();

    const cookies = await context.cookies(HARNESS_URL);
    const workspaceCookie = cookies.find((cookie) => cookie.name.includes("workspace"));
    expect(workspaceCookie, `cookies were: ${cookies.map((c) => c.name).join(", ")}`).toBeDefined();
    // §20.1. `secure` is the one attribute FR-005 lets local HTTP omit, so it
    // is deliberately not asserted here — the other two are unconditional.
    expect(workspaceCookie?.httpOnly).toBe(true);
    expect(workspaceCookie?.sameSite).toBe("Strict");

    // A page that could read the workspace id could hand it to a tool result.
    const visible = await workspace.page.evaluate(() => document.cookie);
    expect(visible).not.toContain(workspaceCookie?.value ?? "no-cookie-value");
  });
});

test.describe("capability reporting", () => {
  test("reports WebMCP as unavailable, and says the workspace still works", async ({
    workspace,
  }) => {
    // No `agent` fixture: this context has no `document.modelContext`, which is
    // every browser that has not opted into the origin trial. AC-09 requires
    // the whole human workflow to remain usable here.
    await workspace.open();

    await expect(workspace.capabilities).toContainText("not available");
    await expect(workspace.capabilities).toContainText("Every step below can still be done by hand");

    // Registration and configuration live on the administration view now,
    // reached the way a person reaches it — through the left rail.
    await workspace.showAdministration();
    const registration = workspace.panel("Tool registration");
    await expect(registration).toContainText("no tools are registered");

    // The controls a person needs are present and not gated on registration.
    await expect(workspace.panel("Configuration").getByRole("button", { name: /reset/i })).toBeEnabled();

    await workspace.showWorkflow();
    await expect(workspace.panel("Target")).toBeVisible();
    await expect(workspace.panel("Contract")).toBeVisible();
  });

  test("reports WebMCP as available and counts what the browser registry holds", async ({
    workspace,
    agent,
  }) => {
    await workspace.open();

    await expect(workspace.capabilities).toContainText("available");

    // FR-003: the count is reconciled against `getTools()`, not inferred from
    // component mount state. Asserting the panel's number against the registry
    // is the only way to tell those two apart.
    await agent.expectRegistered("get_workspace_status");

    // Both read inside one poll, deliberately. Registration settles across
    // several effects, so reading the registry and then the panel would compare
    // two different moments and fail on the page being *right* a beat later.
    await workspace.showAdministration();
    const panel = workspace.panel("Tool registration");
    await expect
      .poll(
        async () => {
          const registered = (await agent.toolNames()).length;
          const reported = (await panel.textContent()) ?? "";
          return registered > 0 && reported.includes(`${String(registered)} tool`);
        },
        { message: "waiting for the panel to report what the browser registry holds" },
      )
      .toBe(true);
  });

  test("names the demo target and its status", async ({ workspace }) => {
    await workspace.open();
    await expect(workspace.capabilities).toContainText("buggy_store");
  });

  test("shows disabled Shopify guidance without offering a pairing action", async ({
    workspace,
  }) => {
    // The hermetic E2E deployment intentionally supplies no Shopify target.
    // A shipped optional integration must be visible as unavailable without
    // leaving a control that can only fail against an unmounted route.
    await workspace.open();

    const pairing = workspace.panel("Shopify pairing");
    await expect(pairing).toContainText("target disabled");
    await expect(pairing.getByRole("button", { name: /create pairing/i })).toHaveCount(0);
  });
});
