/**
 * Fresh footage of the real Shopify development-store proof.
 *
 * The server chooses the only store, variant, and currency. This driver reads
 * the selected contract to learn the already-authorized variant, inspects the
 * live storefront's WebMCP schema before composing one cart-only mutation, and
 * refuses to guess when the published schema has changed.
 */

import { expect, test, type Page } from "@playwright/test";

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
): Promise<ToolResult> {
  const result = (await page.evaluate(
    async ([toolName, toolArguments]) =>
      await window.__awAgent?.invokePinned(
        toolName as string,
        toolArguments as Record<string, unknown>,
      ),
    [name, arguments_] as const,
  )) as ToolResult;
  if (result === undefined || result.isError === true) {
    throw new Error(`${name} returned an error result.`);
  }
  return result;
}

function schemaAlternatives(inputSchema: unknown): readonly Record<string, unknown>[] {
  const root = asRecord(inputSchema, "update_cart.inputSchema");
  const alternatives = [root];
  for (const keyword of ["oneOf", "anyOf"] as const) {
    const candidate = root[keyword];
    if (Array.isArray(candidate)) {
      for (const [index, entry] of candidate.entries()) {
        alternatives.push(asRecord(entry, `update_cart.inputSchema.${keyword}[${String(index)}]`));
      }
    }
  }
  return alternatives;
}

function itemIdentifier(rawVariantId: string, field: string): string | number {
  const gid = rawVariantId.startsWith("gid://")
    ? rawVariantId
    : `gid://shopify/ProductVariant/${rawVariantId}`;
  if (field === "merchandise_id" || field === "merchandiseId" || field === "id") {
    return gid;
  }
  if (field === "variant_id" || field === "variantId") {
    return /^\d+$/.test(rawVariantId) ? Number(rawVariantId) : rawVariantId;
  }
  throw new Error(`Unrecognized Shopify variant identifier field ${field}.`);
}

function updateArguments(inputSchema: unknown, rawVariantId: string): Record<string, unknown> {
  for (const alternative of schemaAlternatives(inputSchema)) {
    const properties = asRecord(alternative["properties"] ?? {}, "update_cart.properties");
    for (const collectionName of ["add_items", "addItems", "lines", "items"] as const) {
      const collectionSchema = properties[collectionName];
      if (collectionSchema === undefined) {
        continue;
      }
      const collection = asRecord(collectionSchema, `update_cart.${collectionName}`);
      const item = asRecord(collection["items"], `update_cart.${collectionName}.items`);
      const itemProperties = asRecord(
        item["properties"] ?? {},
        `update_cart.${collectionName}.items.properties`,
      );
      const identifier = ["merchandise_id", "merchandiseId", "variant_id", "variantId", "id"].find(
        (field) => field in itemProperties,
      );
      if (identifier === undefined || !("quantity" in itemProperties)) {
        continue;
      }
      return {
        [collectionName]: [
          {
            [identifier]: itemIdentifier(rawVariantId, identifier),
            quantity: 1,
          },
        ],
      };
    }
  }

  const names = schemaAlternatives(inputSchema).flatMap((alternative) => {
    const properties = alternative["properties"];
    return typeof properties === "object" && properties !== null && !Array.isArray(properties)
      ? Object.keys(properties as Record<string, unknown>)
      : [];
  });
  throw new Error(
    `The live update_cart schema is not one of the reviewed cart-only shapes (properties: ${names.join(", ")}). No mutation was sent.`,
  );
}

function configuredVariant(contract: Record<string, unknown>): string {
  const document = asRecord(contract["document"], "contract.document");
  const assertions = asArray(document["assertions"], "contract.document.assertions");
  for (const assertionValue of assertions) {
    const assertion = asRecord(assertionValue, "contract assertion");
    if (assertion["id"] !== "the-configured-test-variant") {
      continue;
    }
    const value = assertion["value"];
    if ((typeof value === "string" || typeof value === "number") && String(value) !== "") {
      return String(value);
    }
  }
  throw new Error("The selected Shopify contract did not expose its server-configured variant.");
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

  const workspace = await jsonResponse(page, "/api/v1/workspace");
  const contractId = workspace["selected_contract_id"];
  if (typeof contractId !== "string" || contractId === "") {
    throw new Error("The Shopify contract selection did not produce a contract id.");
  }
  const variantId = configuredVariant(
    await jsonResponse(page, `/api/v1/contracts/${encodeURIComponent(contractId)}`),
  );

  const pairing = page.getByRole("region", { name: "Shopify pairing", exact: true });
  await pairing.scrollIntoViewIfNeeded();
  await expect(pairing.getByRole("button", { name: "Create pairing", exact: true })).toBeEnabled();
  await hold(page, 2_000);
  await pairing.getByRole("button", { name: "Create pairing", exact: true }).click();
  await expect(pairing.locator(".pairing__live")).toHaveAttribute("data-status", "created");
  await expect(pairing).toContainText("#…");
  await hold(page, 3_200);

  const storefrontPromise = context.waitForEvent("page");
  await pairing.getByRole("button", { name: "Open the storefront tab" }).click();
  const storefront = await storefrontPromise;
  await storefront.waitForLoadState("domcontentloaded");
  await expect(storefront.getByRole("status", { name: "ActionWitness pairing" })).toBeVisible({
    timeout: 45_000,
  });

  const updateTool = await waitForTool(storefront, UPDATE_CART);
  await waitForTool(storefront, VERIFY_SHOPIFY_OUTCOME);
  await expect(storefront.getByRole("status", { name: "ActionWitness pairing" })).toContainText(
    "State: armed",
  );
  await hold(storefront, 3_200);

  const mutation = updateArguments(updateTool.inputSchema, variantId);
  await invokeTool(storefront, UPDATE_CART, mutation);
  await hold(storefront, 3_200);
  await invokeTool(storefront, VERIFY_SHOPIFY_OUTCOME, {});
  await expect(storefront.getByRole("status", { name: "ActionWitness pairing" })).toContainText(
    "State: passed",
    { timeout: 45_000 },
  );
  await hold(storefront, 3_800);

  await page.bringToFront();
  await expect(pairing.locator(".pairing__live")).toHaveAttribute("data-status", "passed", {
    timeout: 45_000,
  });
  await expect(pairing).toContainText("platform_session_api");
  await expect(pairing.getByRole("row")).toHaveCount(3);
  await expect(pairing).toContainText("Open the immutable report");
  await pairing.scrollIntoViewIfNeeded();
  await hold(page, 5_200);
});
