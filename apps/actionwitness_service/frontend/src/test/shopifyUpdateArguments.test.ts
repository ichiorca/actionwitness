import { describe, expect, it } from "vitest";

import { updateArguments } from "./shopifyUpdateArguments";

describe("the Shopify live update_cart schema", () => {
  it("builds the reviewed cart.line_items envelope published by Shopify", () => {
    const inputSchema: unknown = {
      type: "object",
      required: ["cart"],
      properties: {
        cart: {
          type: "object",
          required: ["line_items"],
          properties: {
            line_items: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  item: { type: "object", properties: { id: { type: "string" } } },
                  quantity: { type: "integer" },
                },
              },
            },
          },
        },
      },
    };

    expect(updateArguments(inputSchema, "1234567890", 2)).toEqual({
      cart: {
        line_items: [
          {
            item: { id: "gid://shopify/ProductVariant/1234567890" },
            quantity: 2,
          },
        ],
      },
    });
  });
  it("refuses a cart envelope that cannot identify a variant through item.id", () => {
    const inputSchema: unknown = {
      type: "object",
      properties: {
        cart: {
          type: "object",
          properties: {
            line_items: {
              type: "array",
              items: {
                type: "object",
                properties: {
                  id: { type: "string" },
                  quantity: { type: "integer" },
                },
              },
            },
          },
        },
      },
    };

    expect(() => updateArguments(inputSchema, "1234567890")).toThrow(
      /not one of the reviewed cart-only shapes/,
    );
  });

  it("preserves the previously reviewed top-level add_items shape", () => {
    const inputSchema: unknown = {
      type: "object",
      properties: {
        add_items: {
          type: "array",
          items: {
            type: "object",
            properties: {
              variant_id: { type: "integer" },
              quantity: { type: "integer" },
            },
          },
        },
      },
    };

    expect(updateArguments(inputSchema, "1234567890")).toEqual({
      add_items: [{ variant_id: 1234567890, quantity: 1 }],
    });
  });
});