function asRecord(value: unknown, field: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(field + " was not an object.");
  }
  return value as Record<string, unknown>;
}

function schemaAlternatives(inputSchema: unknown): readonly Record<string, unknown>[] {
  const root = asRecord(inputSchema, "update_cart.inputSchema");
  const alternatives = [root];
  for (const keyword of ["oneOf", "anyOf"] as const) {
    const candidate = root[keyword];
    if (Array.isArray(candidate)) {
      for (const [index, entry] of candidate.entries()) {
        alternatives.push(
          asRecord(entry, "update_cart.inputSchema." + keyword + "[" + String(index) + "]"),
        );
      }
    }
  }
  return alternatives;
}

function itemIdentifier(rawVariantId: string, field: string): string | number {
  const gid = rawVariantId.startsWith("gid://")
    ? rawVariantId
    : "gid://shopify/ProductVariant/" + rawVariantId;
  if (field === "merchandise_id" || field === "merchandiseId" || field === "id") {
    return gid;
  }
  if (field === "variant_id" || field === "variantId") {
    return /^\d+$/.test(rawVariantId) ? Number(rawVariantId) : rawVariantId;
  }
  throw new Error("Unrecognized Shopify variant identifier field " + field + ".");
}

export function updateArguments(
  inputSchema: unknown,
  rawVariantId: string,
  quantity = 1,
): Record<string, unknown> {
  if (!Number.isSafeInteger(quantity) || quantity < 1) {
    throw new Error("Shopify update_cart quantity must be a positive safe integer.");
  }
  for (const alternative of schemaAlternatives(inputSchema)) {
    const properties = asRecord(alternative["properties"] ?? {}, "update_cart.properties");
    const cartSchema = properties["cart"];
    if (cartSchema !== undefined) {
      const cart = asRecord(cartSchema, "update_cart.cart");
      const cartProperties = asRecord(cart["properties"] ?? {}, "update_cart.cart.properties");
      const linesSchema = cartProperties["line_items"];
      if (linesSchema !== undefined) {
        const lines = asRecord(linesSchema, "update_cart.cart.line_items");
        const line = asRecord(lines["items"], "update_cart.cart.line_items.items");
        const lineProperties = asRecord(
          line["properties"] ?? {},
          "update_cart.cart.line_items.items.properties",
        );
        const itemSchema = lineProperties["item"];
        if (itemSchema !== undefined && "quantity" in lineProperties) {
          const item = asRecord(itemSchema, "update_cart.cart.line_items.items.item");
          const itemProperties = asRecord(
            item["properties"] ?? {},
            "update_cart.cart.line_items.items.item.properties",
          );
          if ("id" in itemProperties) {
            return {
              cart: {
                line_items: [
                  {
                    item: { id: itemIdentifier(rawVariantId, "id") },
                    quantity,
                  },
                ],
              },
            };
          }
        }
      }
    }
    for (const collectionName of ["add_items", "addItems", "lines", "items"] as const) {
      const collectionSchema = properties[collectionName];
      if (collectionSchema === undefined) {
        continue;
      }
      const collection = asRecord(collectionSchema, "update_cart." + collectionName);
      const item = asRecord(collection["items"], "update_cart." + collectionName + ".items");
      const itemProperties = asRecord(
        item["properties"] ?? {},
        "update_cart." + collectionName + ".items.properties",
      );
      const identifier = [
        "merchandise_id",
        "merchandiseId",
        "variant_id",
        "variantId",
        "id",
      ].find((field) => field in itemProperties);
      if (identifier === undefined || !("quantity" in itemProperties)) {
        continue;
      }
      return {
        [collectionName]: [
          {
            [identifier]: itemIdentifier(rawVariantId, identifier),
            quantity,
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
    "The live update_cart schema is not one of the reviewed cart-only shapes (properties: " +
      names.join(", ") +
      "). No mutation was sent.",
  );
}