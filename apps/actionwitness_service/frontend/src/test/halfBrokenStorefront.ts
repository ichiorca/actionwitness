/**
 * A storefront that lies about its cart (015-T4).
 *
 * §12.17's audit exists because of surfaces like this one: the read tools
 * answer perfectly, `update_cart` returns a cheerful success, and the shopper's
 * cart never changes. A merchant looking at their own agent tools sees
 * everything working.
 *
 * **This is a fixture we own, and that is the point.** The plan is explicit:
 * "no external network in any required lane", and the guardrails forbid live
 * scans of third-party brands "ever, in any test or demo". The failure shape is
 * reproduced here rather than demonstrated against somebody else's store.
 *
 * It is deliberately *half* broken. A surface where everything failed would be
 * caught by any smoke test and would prove nothing about why independent
 * observation is needed; the interesting case is the one where five tools work
 * and the sixth is the reason a customer's order never arrives.
 */

/** The cart this fixture reports through its read tool. */
export interface FixtureCart {
  readonly items: { readonly variant_id: number; readonly quantity: number; readonly price: number }[];
  readonly item_count: number;
  readonly total_price: number;
  readonly items_subtotal_price: number;
  readonly currency: string;
}

export const EMPTY_CART: FixtureCart = {
  items: [],
  item_count: 0,
  total_price: 0,
  items_subtotal_price: 0,
  currency: "USD",
};

export interface FixtureOptions {
  /**
   * When true, `update_cart` reports success and mutates nothing — the
   * Allbirds-shaped failure, reproduced in a fixture we own.
   *
   * When false the same tool actually updates the cart, so a test can show the
   * audit passing on an honest surface. Without that half, a green audit would
   * be unfalsifiable.
   */
  readonly cartToolLies: boolean;
}

/**
 * The one capability this fixture needs from a `modelContext`.
 *
 * Structural rather than `unknown`-typed: a parameter typed `unknown` is not
 * assignable *from* the real `registerTool`, and widening it further would mean
 * the fixture no longer type-checks against the interface it is pretending to
 * be published on.
 */
export type Registrar = Pick<WebMCP.ModelContext, "registerTool">;

export interface FixtureStorefront {
  /** What `GET /cart.js` would return right now, for the observation channel. */
  readonly readCart: () => FixtureCart;
  /** Register the surface on a `modelContext` double. */
  readonly register: (modelContext: Registrar) => Promise<void>;
}

/**
 * Build the fixture.
 *
 * The cart lives in a closure rather than in the tool handlers, because the
 * whole demonstration depends on the *observation* channel and the *tool*
 * channel reading from the same place. If the tool wrote to state the read
 * could not see, the fixture would prove the harness works by rigging it.
 */
export function halfBrokenStorefront(options: FixtureOptions): FixtureStorefront {
  let cart: FixtureCart = EMPTY_CART;

  const readCart = (): FixtureCart => cart;

  const applyUpdate = (variantId: number, quantity: number): void => {
    const price = 2599;
    cart = {
      items: quantity === 0 ? [] : [{ variant_id: variantId, quantity, price }],
      item_count: quantity,
      total_price: price * quantity,
      items_subtotal_price: price * quantity,
      currency: "USD",
    };
  };

  const register = async (modelContext: Registrar): Promise<void> => {
    // The read tools. These genuinely work, which is what makes the surface
    // convincing to whoever published it.
    await modelContext.registerTool({
      name: "search_catalog",
      description: "Search the catalog.",
      inputSchema: { type: "object", properties: { query: { type: "string" } } },
      execute: () =>
        Promise.resolve({
          content: [{ type: "text", text: JSON.stringify([{ variant_id: 111, price: 2599 }]) }],
        }),
    });

    await modelContext.registerTool({
      name: "get_cart",
      description: "Read the shopper's cart.",
      inputSchema: { type: "object", properties: {} },
      execute: () => Promise.resolve({ content: [{ type: "text", text: JSON.stringify(cart) }] }),
    });

    // The one that lies. It answers exactly as the working version does — same
    // shape, same cheerful text — and that identical answer is why a tool-level
    // evaluator cannot tell the two apart and an independent read can.
    await modelContext.registerTool({
      name: "update_cart",
      description: "Add an item to the cart.",
      inputSchema: {
        type: "object",
        properties: { variant_id: { type: "number" }, quantity: { type: "number" } },
        required: ["variant_id", "quantity"],
      },
      execute: (input: unknown) => {
        if (!options.cartToolLies) {
          const args = (input ?? {}) as { variant_id?: number; quantity?: number };
          applyUpdate(args.variant_id ?? 111, args.quantity ?? 1);
        }
        return Promise.resolve({
          content: [{ type: "text", text: '{"status":"success","message":"Added to cart"}' }],
        });
      },
    });

    // Present and never invoked. FR-162 forbids exercising it against an
    // external target; the fixture publishes it so the audit has something real
    // to report as present-but-unexercised, which is the case a merchant most
    // needs to see.
    await modelContext.registerTool({
      name: "proceed_to_checkout",
      description: "Start checkout.",
      inputSchema: { type: "object", properties: {} },
      execute: () =>
        Promise.reject(new Error("the fixture refuses: nothing may exercise checkout")),
    });
  };

  return { readCart, register };
}
