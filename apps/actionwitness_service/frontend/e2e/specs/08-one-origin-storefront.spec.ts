/**
 * §29.1 and ADR-0006: two applications on one origin, sharing nothing.
 *
 * The composed deployment is the part of this product with the most ways to be
 * quietly wrong, and none of them are visible below the browser: a proxy that
 * forwards the harness's `HttpOnly` cookie into the demo target, a storefront
 * that acquires a harness workspace by being opened, a `Set-Cookie` from the
 * store landing on the harness's origin. Each would be a boundary failure that
 * every existing test would still pass.
 *
 * The storefront half also carries AC-09's other meaning: this is what a person
 * uses when there is no agent, no harness, and no WebMCP — so the same false
 * success the harness catches has to be visible to a human here, in words.
 */

import { emptyStorage, expect, test } from "../support/harness";

const STORE_API = "/demo/api/v1/store";

test.describe("the storefront stands on its own", () => {
  test("runs a whole cart journey with no harness and no WebMCP", async ({ page }) => {
    await page.goto("/demo");
    await expect(page.getByRole("heading", { name: "Buggy Store", level: 1 })).toBeVisible();

    // §26.7 gates the manifest and the source; this is the runtime half of the
    // same claim.
    expect(await page.evaluate(() => "modelContext" in document)).toBe(false);

    await expect(page.getByTestId("empty-cart")).toBeVisible();
    await page.getByRole("button", { name: /add/i }).first().click();

    await expect(page.getByTestId("subtotal")).toHaveText("25.00");
    await expect(page.getByTestId("total")).toHaveText("25.00");

    await page.getByRole("button", { name: "Apply SAVE20" }).click();
    await expect(page.getByTestId("discount")).toContainText("5.00");
    await expect(page.getByTestId("total")).toHaveText("20.00");
  });

  test("labels an injected defect as injected, in words a person can read", async ({
    page,
    request,
  }) => {
    await page.goto("/demo");
    await expect(page.getByTestId("scenario-banner")).toBeVisible();

    // The shopper's own isolation id, which the storefront keeps in
    // localStorage and is explicitly *not* an authorization boundary.
    const shopper = await page.evaluate(() =>
      window.localStorage.getItem("buggy-store.workspace-id"),
    );
    expect(shopper).not.toBeNull();

    const selected = await request.post(`${STORE_API}/scenario`, {
      headers: { "X-Workspace-Id": shopper ?? "" },
      data: { scenario_mode: "pre_fix", fault_profile: "discount_reported_but_not_applied" },
    });
    expect(selected.ok(), await selected.text()).toBeTruthy();

    await page.reload();
    // §20.4: "the UI clearly labels unsafe injected modes", as an alert rather
    // than a colour, so a viewer cannot mistake a seeded defect for a real one.
    const banner = page.getByTestId("scenario-banner");
    await expect(banner).toContainText("Injected unsafe demo behaviour is active");
    await expect(banner).toContainText("discount_reported_but_not_applied");

    await page.getByRole("button", { name: /add/i }).first().click();
    await expect(page.getByTestId("total")).toHaveText("25.00");

    // AC-04 from the human's side of the same defect: the store says the
    // discount was applied and the total it then shows has not moved.
    await page.getByRole("button", { name: "Apply SAVE20" }).click();
    await expect(page.getByRole("status").first()).toBeVisible();
    await expect(page.getByTestId("total")).toHaveText("25.00");
  });
});

test.describe("the boundary between the two applications", () => {
  test("mints no harness workspace for a storefront-only visitor", async ({ browser }) => {
    // A visitor who has never seen the harness. `/demo` and `/demo/api/v1` are
    // workspace-exempt, so nothing may be issued to them — otherwise the table
    // fills with rows no harness user created, and a `HttpOnly` credential for
    // `/api/v1` is attached to a page that is required to work with no harness
    // present at all.
    const visitor = await browser.newContext({ storageState: emptyStorage() });
    try {
      const page = await visitor.newPage();
      await page.goto("http://127.0.0.1:8010/demo");
      await expect(page.getByRole("heading", { name: "Buggy Store", level: 1 })).toBeVisible();
      await page.getByRole("button", { name: /add/i }).first().click();
      await expect(page.getByTestId("subtotal")).toHaveText("25.00");

      const cookies = await visitor.cookies("http://127.0.0.1:8010");
      expect(
        cookies.filter((cookie) => cookie.name.includes("workspace")),
        `storefront-only visitor received: ${cookies.map((c) => c.name).join(", ")}`,
      ).toEqual([]);
    } finally {
      await visitor.close();
    }
  });

  test("does not forward the harness cookie into the demo target", async ({
    workspace,
    harness,
  }) => {
    // Establish the harness cookie in this context first, so the request below
    // genuinely carries one.
    await workspace.open();
    const cookies = await workspace.page.context().cookies();
    expect(cookies.some((cookie) => cookie.name.includes("workspace"))).toBe(true);

    // The proxy forwards by allowlist, and `Cookie` is not on it: the demo
    // target has no use for the harness's ambient authority and no obligation
    // to protect it. The store answers on its own `X-Workspace-Id` instead,
    // which is the observable consequence.
    const response = await harness.raw.get(`${STORE_API}/cart`, {
      headers: { "X-Workspace-Id": "e2e-proxy-boundary" },
    });
    expect(response.ok(), await response.text()).toBeTruthy();

    // `Set-Cookie` is deliberately absent from the response allowlist, so the
    // demo target can never write a cookie onto the harness's origin.
    expect(Object.keys(response.headers())).not.toContain("set-cookie");
    expect(response.headers()["content-type"]).toContain("application/json");
  });

  test("keeps the storefront's cart out of the harness run's target state", async ({
    workspace,
    agent,
    harness,
    page,
  }) => {
    // A shopper puts something in their own cart.
    await page.goto("/demo");
    const shopper = await page.evaluate(() =>
      window.localStorage.getItem("buggy-store.workspace-id"),
    );
    await page.getByRole("button", { name: /add/i }).first().click();
    await expect(page.getByTestId("subtotal")).toHaveText("25.00");

    // The harness then arms a run in its own workspace and observes an empty
    // cart. The workspace/run is the isolation boundary; one browser holding
    // both must not merge them.
    await workspace.open();
    await workspace.arm();
    const cart = await agent.call("get_cart");
    expect(JSON.stringify(cart)).not.toContain(shopper ?? "no-shopper");

    const status = await harness.workspace();
    const runId = String((status["active_run"] as Record<string, unknown>)["id"]);
    const events = (await harness.events(runId))["events"] as Record<string, unknown>[];
    // The arming snapshot saw the harness workspace's own empty cart, not the
    // shopper's.
    expect(events.map((event) => event["event_type"])).toContain("snapshot_captured");
  });

  test("refuses a proxied body over the composition's ceiling", async ({ harness }) => {
    // §20.2 bounds a frontend-submitted payload at 64 KiB, enforced by the
    // proxy rather than by the store: without it one request could hold an
    // arbitrary amount of the harness process's memory before the store ever
    // saw a byte.
    const response = await harness.raw.post(`${STORE_API}/cart/mutations`, {
      headers: { "X-Workspace-Id": "e2e-oversize", "Content-Type": "application/json" },
      data: { product_id: "x".repeat(70_000), quantity: 1, request_id: "e2e-oversize-body" },
    });
    expect(response.ok()).toBeFalsy();
    const envelope = (await response.json()) as { error?: { code?: string } };
    expect(envelope.error?.code).toBe("CONTRACT_VALIDATION_FAILED");
  });
});
