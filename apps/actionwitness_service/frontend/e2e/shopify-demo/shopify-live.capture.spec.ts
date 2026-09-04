/**
 * Fresh footage of the real Shopify development-store proof.
 *
 * The server chooses the only store, variant, and currency. This driver reads
 * the selected contract to learn the already-authorized variant, inspects the
 * live storefront's WebMCP schema before composing one cart-only mutation, and
 * refuses to guess when the published schema has changed.
 */

import { expect, test, type Page } from "@playwright/test";

import { updateArguments } from "../../src/test/shopifyUpdateArguments";

import { installWebMcpAgent } from "../support/webmcpAgent";

const SHOPIFY_TEMPLATE = "shopify_exact_cart";
const VERIFY_SHOPIFY_OUTCOME = "verify_shopify_outcome";
const UPDATE_CART = "update_cart";
const REQUIRED_SCHEMA_VERSION = 10;

interface ToolResult {
  readonly content?: readonly { readonly type?: unknown; readonly text?: unknown }[];
  readonly isError?: boolean;
}

interface ToolDescription {
  readonly name: string;
  readonly description: string;
  readonly inputSchema: unknown;
}

function asRecord(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${field} was not an object.`);
  }
  return value as Record<string, unknown>;
}

function asArray(value: unknown, field: string): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`${field} was not an array.`);
  }
  return value;
}

async function jsonResponse(page: Page, path: string): Promise<Record<string, unknown>> {
  const response = await page.request.get(path);
  if (!response.ok()) {
    throw new Error(`GET ${path} failed with ${String(response.status())}.`);
  }
  return asRecord(await response.json(), path);
}

async function hold(page: Page, milliseconds = 1_800): Promise<void> {
  await page.waitForTimeout(milliseconds);
}

async function toolDescriptions(page: Page): Promise<readonly ToolDescription[]> {
  return await page.evaluate(() => window.__awAgent?.describe() ?? []);
}

async function waitForTool(page: Page, name: string): Promise<ToolDescription> {
  await expect
    .poll(
      async () => (await toolDescriptions(page)).map((tool) => tool.name),
      { message: `waiting for the storefront to register ${name}`, timeout: 45_000 },
    )
    .toContain(name);
  const found = (await toolDescriptions(page)).find((tool) => tool.name === name);
  if (found === undefined) {
    throw new Error(`${name} disappeared while its schema was being read.`);
  }
  return found;
}

async function invokeTool(
  page: Page,
  name: string,
  arguments_: Record<string, unknown>,
  allowErrorResult = false,
): Promise<ToolResult> {
  const result = (await page.evaluate(
    async ([toolName, toolArguments]) =>
      await window.__awAgent?.invokePinned(
        toolName as string,
        toolArguments as Record<string, unknown>,
      ),
    [name, arguments_] as const,
  )) as ToolResult;
  if (result === undefined || (result.isError === true && !allowErrorResult)) {
    throw new Error(`${name} returned an error result.`);
  }
  return result;
}
function toolText(result: ToolResult): string {
  return (result.content ?? [])
    .filter(
      (block): block is { readonly type: "text"; readonly text: string } =>
        block.type === "text" && typeof block.text === "string",
    )
    .map((block) => block.text)
    .join("\n");
}
function configuredVariant(contract: Record<string, unknown>): string {
  const document = asRecord(contract["document"], "contract.document");
  const assertions = asArray(document["assertions"], "contract.document.assertions");
  let variantId: string | null = null;
  let hasConfiguredCurrency = false;
  for (const assertionValue of assertions) {
    const assertion = asRecord(assertionValue, "contract assertion");
    if (assertion["id"] === "the-configured-test-variant") {
      const value = assertion["value"];
      if ((typeof value === "string" || typeof value === "number") && String(value) !== "") {
        variantId = String(value);
      }
    }
    if (assertion["id"] === "the-expected-currency") {
      hasConfiguredCurrency =
        typeof assertion["value"] === "string" && /^[A-Z]{3}$/.test(assertion["value"]);
    }
  }
  if (variantId !== null && hasConfiguredCurrency) {
    return variantId;
  }
  throw new Error(
    "The pairing contract did not expose its server-configured variant and currency.",
  );
}

test("04 — real Shopify same-session cart proof", async ({ context, page }) => {
  await context.addInitScript(installWebMcpAgent);

  const health = await jsonResponse(page, "/healthz");
  const schemaVersion = health["schema_version"];
  expect(
    typeof schemaVersion === "number" && schemaVersion >= REQUIRED_SCHEMA_VERSION,
    `The deployed demo must run schema ${String(REQUIRED_SCHEMA_VERSION)} or newer; found ${String(schemaVersion)}.`,
  ).toBeTruthy();

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "ActionWitness", level: 1 })).toBeVisible();
  await expect(page.getByText(/shopify:\s*enabled/i)).toBeVisible();
  await hold(page, 2_200);

  const shopifyContract = page.getByRole("button", { name: SHOPIFY_TEMPLATE, exact: true });
  await expect(shopifyContract).toBeVisible();
  await shopifyContract.click();
  await expect(shopifyContract).toHaveAttribute("aria-pressed", "true");

  const pairing = page.getByRole("region", { name: "Shopify pairing", exact: true });
  await pairing.scrollIntoViewIfNeeded();
  await expect(pairing.getByRole("button", { name: "Create pairing", exact: true })).toBeEnabled();
  await hold(page, 2_000);
  const createdPairingResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      response.request().method() === "POST" && url.pathname === "/api/v1/shopify/pairings"
    );
  });
  await pairing.getByRole("button", { name: "Create pairing", exact: true }).click();
  const createdPairing = await createdPairingResponse;
  expect(createdPairing.ok(), "The server refused to create the Shopify pairing.").toBeTruthy();
  const createdDocument = asRecord(await createdPairing.json(), "created Shopify pairing");
  const contractId = createdDocument["contract_id"];
  if (typeof contractId !== "string" || contractId === "") {
    throw new Error("The pairing did not name its server-expanded contract.");
  }
  const variantId = configuredVariant(
    await jsonResponse(page, `/api/v1/contracts/${encodeURIComponent(contractId)}`),
  );
  await expect(pairing.locator(".pairing__live")).toHaveAttribute("data-status", "created");
  await expect(pairing).toContainText("#…");
  await hold(page, 3_200);

  const storefrontPromise = context.waitForEvent("page");
  await pairing.getByRole("button", { name: "Open the storefront tab" }).click();
  const storefront = await storefrontPromise;
  const harnessOrigin = new URL(page.url()).origin;
  const verificationNetwork: string[] = [];
  storefront.on("response", (response) => {
    const url = new URL(response.url());
    if (url.origin === harnessOrigin && url.pathname.startsWith("/api/v1/shopify/")) {
      verificationNetwork.push(
        `${response.request().method()} ${url.pathname} -> ${String(response.status())}`,
      );
    }
  });
  storefront.on("requestfailed", (request) => {
    const url = new URL(request.url());
    if (url.origin === harnessOrigin && url.pathname.startsWith("/api/v1/shopify/")) {
      verificationNetwork.push(
        `${request.method()} ${url.pathname} -> ${request.failure()?.errorText ?? "request failed"}`,
      );
    }
  });
  await storefront.waitForLoadState("domcontentloaded");
  await expect(storefront.getByRole("status", { name: "ActionWitness pairing" })).toBeVisible({
    timeout: 45_000,
  });

  const updateTool = await waitForTool(storefront, UPDATE_CART);
  try {
    await waitForTool(storefront, VERIFY_SHOPIFY_OUTCOME);
  } catch (error: unknown) {
    const bridgeState = await storefront
      .getByRole("status", { name: "ActionWitness pairing" })
      .innerText();
    throw new Error(
      `The bridge did not arm. Visible bridge state: ${bridgeState}. Network: ${verificationNetwork.join("; ")}`,
      { cause: error },
    );
  }
  await expect(storefront.getByRole("status", { name: "ActionWitness pairing" })).toContainText(
    "State: armed",
  );
  await hold(storefront, 3_200);

  const mutation = updateArguments(updateTool.inputSchema, variantId);
  await invokeTool(storefront, UPDATE_CART, mutation);
  await hold(storefront, 3_200);
  const verification = await invokeTool(storefront, VERIFY_SHOPIFY_OUTCOME, {}, true);
  expect(
    toolText(verification),
    `The bridge must complete a real failed verdict, not stop with an observation or transport error. Network: ${verificationNetwork.join("; ")}`,
  ).toMatch(/^Verified\. Result: failed\./);
  await expect(storefront.getByRole("status", { name: "ActionWitness pairing" })).toContainText(
    "State: failed",
    { timeout: 45_000 },
  );
  await hold(storefront, 3_800);

  await page.bringToFront();
  await expect(pairing.locator(".pairing__live")).toHaveAttribute("data-status", "failed", {
    timeout: 45_000,
  });
  await expect(pairing).toContainText("platform_session_api");
  await expect(pairing.getByRole("row")).toHaveCount(3);
  await expect(pairing).toContainText("Open the immutable report");
  await pairing.scrollIntoViewIfNeeded();
  await hold(page, 5_200);
});
