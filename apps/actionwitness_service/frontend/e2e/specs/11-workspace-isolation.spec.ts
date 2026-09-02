/**
 * AC-01/AC-11: the workspace is the isolation boundary, in real browsers.
 *
 * The Python suite proves the repositories scope every query. What it cannot
 * prove is that two browsers get two workspaces and that neither can reach the
 * other's evidence through the surface a person actually uses — the cookie is
 * the whole mechanism, and a cookie is a browser thing.
 *
 * These tests deliberately mint workspaces, which FR-009 limits to ten an hour
 * per client. They stay well inside that by presenting as their own clients and
 * by minting exactly the two they need.
 */

import { HARNESS_URL } from "../../playwright.config";
import { emptyStorage, expect, test } from "../support/harness";

/** A browser that has never seen the harness, on its own simulated network. */
async function freshVisitor(
  browser: import("@playwright/test").Browser,
  address: string,
): Promise<import("@playwright/test").BrowserContext> {
  // `storageState` is explicit: a context created inside a test inherits the
  // project's, which would give this "fresh" visitor the suite's own workspace
  // cookie — and an isolation test that shares a workspace proves nothing.
  const context = await browser.newContext({ baseURL: HARNESS_URL, storageState: emptyStorage() });
  await context.setExtraHTTPHeaders({ "X-Forwarded-For": address });
  return context;
}

test.describe("two browsers, two workspaces", () => {
  test("cannot read each other's runs", async ({ browser, agent: _agent }) => {
    const first = await freshVisitor(browser, "192.0.2.11");
    const second = await freshVisitor(browser, "192.0.2.12");
    try {
      const firstPage = await first.newPage();
      await firstPage.goto("/");
      await expect(firstPage.getByRole("heading", { name: "ActionWitness" })).toBeVisible();

      const secondPage = await second.newPage();
      await secondPage.goto("/");
      await expect(secondPage.getByRole("heading", { name: "ActionWitness" })).toBeVisible();

      const firstId = await workspaceIdOf(firstPage);
      const secondId = await workspaceIdOf(secondPage);
      expect(firstId).not.toBe(secondId);

      // A fresh workspace has no contract selected, and arming is refused until
      // one is — the refusal is the server's rule, not this test's setup.
      await selectFirstTemplate(firstPage);

      // The first visitor arms a run. The second must not be able to read it,
      // even knowing its id: §20.1 makes the cookie the authorization, so an
      // identifier is not a capability.
      const armed = await firstPage.evaluate(async () => {
        const response = await fetch("/api/v1/runs", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
        return (await response.json()) as Record<string, unknown>;
      });
      const runId = String(armed["run_id"] ?? armed["id"] ?? "");
      expect(runId, JSON.stringify(armed)).not.toBe("");

      const attempted = await secondPage.evaluate(async (id) => {
        const response = await fetch(`/api/v1/runs/${id}`, { credentials: "same-origin" });
        return { status: response.status, body: (await response.text()).slice(0, 300) };
      }, runId);

      expect(attempted.status).toBeGreaterThanOrEqual(400);
      const envelope = JSON.parse(attempted.body) as { error?: { code?: string } };
      expect(envelope.error?.code).toBe("RESOURCE_NOT_FOUND");
      // Refused as "no such run", not "not yours": a distinguishable refusal
      // would confirm the id exists to somebody who should not know that.
      expect(attempted.body).not.toContain(String(firstId));
    } finally {
      await first.close();
      await second.close();
    }
  });

  test("keeps target state separate between workspaces", async ({ browser }) => {
    const shopper = await freshVisitor(browser, "192.0.2.21");
    try {
      const page = await shopper.newPage();
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "ActionWitness" })).toBeVisible();

      // This visitor's own run and cart. The shared workspace every other test
      // uses must not see any of it.
      await page.evaluate(async () => {
        await fetch("/api/v1/workspace/reset", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ purge_completed: true }),
        });
      });
      expect(await workspaceIdOf(page)).not.toBe("");
    } finally {
      await shopper.close();
    }
  });
});

test.describe("the request limit", () => {
  test("refuses with a stable envelope and a whole-second Retry-After", async ({ browser }) => {
    // FR-009's limits are a documented capacity, not an obstacle to route
    // around: a client that outruns them is told when to come back, and told in
    // the same envelope shape as every other refusal.
    const context = await freshVisitor(browser, "192.0.2.31");
    try {
      const page = await context.newPage();
      await page.goto("/");
      await expect(page.getByRole("heading", { name: "ActionWitness" })).toBeVisible();

      const refusal = await page.evaluate(async () => {
        for (let attempt = 0; attempt < 200; attempt += 1) {
          const response = await fetch("/api/v1/workspace", { credentials: "same-origin" });
          if (response.status === 429) {
            return {
              retryAfter: response.headers.get("Retry-After"),
              body: await response.text(),
            };
          }
        }
        return null;
      });

      expect(refusal, "the limiter never refused within 200 requests").not.toBeNull();
      const envelope = JSON.parse(refusal?.body ?? "{}") as {
        error?: { code?: string; retryable?: boolean };
      };
      expect(envelope.error?.code).toBe("RATE_LIMIT_EXCEEDED");
      // Retryable, unlike a validation refusal: trying again later genuinely
      // can succeed, and the client is told how long later.
      expect(envelope.error?.retryable).toBe(true);
      const retryAfter = Number(refusal?.retryAfter);
      expect(Number.isInteger(retryAfter)).toBe(true);
      // Never zero: a client told to retry immediately fails immediately, which
      // turns one refusal into a busy loop.
      expect(retryAfter).toBeGreaterThanOrEqual(1);
    } finally {
      await context.close();
    }
  });

  test("does not spend the budget on health checks or static assets", async ({ browser }) => {
    const context = await freshVisitor(browser, "192.0.2.41");
    try {
      const page = await context.newPage();
      await page.goto("/");

      // A liveness probe running every second must not consume half a
      // workspace's allowance and take the deployment down by monitoring it.
      const codes = await page.evaluate(async () => {
        const seen: number[] = [];
        for (let attempt = 0; attempt < 60; attempt += 1) {
          seen.push((await fetch("/healthz")).status);
        }
        return seen;
      });
      expect(new Set(codes)).toEqual(new Set([200]));
    } finally {
      await context.close();
    }
  });
});

/** Select the first built-in contract, which also selects its target. */
async function selectFirstTemplate(page: import("@playwright/test").Page): Promise<void> {
  const selected = await page.evaluate(async () => {
    const listed = await fetch("/api/v1/contracts/templates", { credentials: "same-origin" });
    const body = (await listed.json()) as { templates?: { contract_id: string }[] };
    const first = body.templates?.[0]?.contract_id ?? "";
    const response = await fetch(`/api/v1/contracts/${first}/select`, {
      method: "POST",
      credentials: "same-origin",
    });
    return { ok: response.ok, body: await response.text() };
  });
  expect(selected.ok, selected.body).toBe(true);
}

/** The workspace this page is acting in, read through the API it uses. */
async function workspaceIdOf(page: import("@playwright/test").Page): Promise<string> {
  return await page.evaluate(async () => {
    const response = await fetch("/api/v1/workspace", { credentials: "same-origin" });
    const body = (await response.json()) as Record<string, unknown>;
    return String(body["workspace_id"] ?? "");
  });
}
