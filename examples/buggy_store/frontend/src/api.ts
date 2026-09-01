/**
 * The storefront's only route to the server: the store's own versioned API.
 *
 * Spec v1.9 §15.5 (the endpoint table, and who may call it), §15.8 (the one
 * error envelope); constitution stack-typescript rules: "treat HTTP JSON as
 * `unknown` and validate or narrow it through runtime checks".
 *
 * Everything the server returns arrives here as `unknown` and leaves as a
 * declared type. That is not ceremony: a `JSON.parse` result typed as the shape
 * you hoped for is a lie the compiler will happily maintain, and this UI renders
 * money. A response that does not match is a thrown `StoreApiError`, not a
 * silently missing total.
 *
 * There is no WebMCP here and no harness call. §15.5 reserves this surface for
 * the store's own human UI and `integrations.buggy_store`; the storefront must
 * work in a browser with no agent support at all.
 */

const API = "/demo/api/v1/store";

/** Project-allocated; the store's isolation scope, never an authorization one. */
export const WORKSPACE_HEADER = "X-Workspace-Id";

export interface Product {
  readonly product_id: string;
  readonly line_key: string;
  readonly name: string;
  readonly price: string;
  readonly stock: number;
}

export interface CartLine {
  readonly product_id: string;
  readonly quantity: number;
  readonly unit_price: string;
}

export interface CartDiscount {
  readonly code: string;
  readonly amount: string;
}

export interface Cart {
  readonly items: Readonly<Record<string, CartLine>>;
  readonly discount: CartDiscount | null;
  readonly subtotal: string;
  readonly total: string;
}

export interface Order {
  readonly created: boolean;
  readonly order_id: string | null;
}

export interface StoreState {
  readonly state_version: number;
  readonly cart: Cart;
  readonly order: Order;
}

export interface Scenario {
  readonly scenario_mode: string;
  readonly fault_profile: string;
  readonly fault_active: boolean;
  readonly description: string;
  /** Present only for an injected profile (FR-011). */
  readonly label?: string;
}

export interface Confirmation {
  readonly confirmation_id: string;
  readonly status: string;
  readonly consequence: {
    readonly action: string;
    readonly state_version: number;
    readonly cart_total: string;
    readonly item_count: number;
  };
  readonly expires_at: string;
}

/**
 * A failure the store described (§15.8), carrying its stable code.
 *
 * The code is what the UI branches on, never the message: messages are for
 * people and may be reworded, codes are the contract.
 */
export class StoreApiError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly retryable: boolean,
  ) {
    super(message);
    this.name = "StoreApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function requireString(value: unknown, field: string): string {
  if (typeof value !== "string") {
    throw new StoreApiError("MALFORMED_RESPONSE", `${field} is not a string`, false);
  }
  return value;
}

function requireNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new StoreApiError("MALFORMED_RESPONSE", `${field} is not a number`, false);
  }
  return value;
}

function requireBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") {
    throw new StoreApiError("MALFORMED_RESPONSE", `${field} is not a boolean`, false);
  }
  return value;
}

function requireRecord(value: unknown, field: string): Record<string, unknown> {
  if (!isRecord(value)) {
    throw new StoreApiError("MALFORMED_RESPONSE", `${field} is not an object`, false);
  }
  return value;
}

function narrowCart(value: unknown): Cart {
  const cart = requireRecord(value, "cart");
  const rawItems = requireRecord(cart["items"], "cart.items");
  const items: Record<string, CartLine> = {};
  for (const [key, raw] of Object.entries(rawItems)) {
    const line = requireRecord(raw, `cart.items.${key}`);
    items[key] = {
      product_id: requireString(line["product_id"], "product_id"),
      quantity: requireNumber(line["quantity"], "quantity"),
      unit_price: requireString(line["unit_price"], "unit_price"),
    };
  }
  const rawDiscount = cart["discount"];
  return {
    items,
    discount:
      rawDiscount === null || rawDiscount === undefined
        ? null
        : {
            code: requireString(requireRecord(rawDiscount, "discount")["code"], "discount.code"),
            amount: requireString(
              requireRecord(rawDiscount, "discount")["amount"],
              "discount.amount",
            ),
          },
    subtotal: requireString(cart["subtotal"], "cart.subtotal"),
    total: requireString(cart["total"], "cart.total"),
  };
}

function narrowOrder(value: unknown): Order {
  const order = requireRecord(value, "order");
  const identifier = order["order_id"];
  return {
    created: requireBoolean(order["created"], "order.created"),
    order_id: identifier === null || identifier === undefined ? null : requireString(identifier, "order.order_id"),
  };
}

/** The store's own client. One instance per shopper session. */
export class StoreClient {
  constructor(
    private readonly workspaceId: string,
    private readonly fetchImpl: typeof fetch = globalThis.fetch.bind(globalThis),
  ) {}

  private async request(path: string, init?: RequestInit): Promise<unknown> {
    const response = await this.fetchImpl(`${API}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        [WORKSPACE_HEADER]: this.workspaceId,
        ...(init?.headers ?? {}),
      },
    });

    // An empty or non-JSON body is a malformed response, not an empty object.
    // Guessing here would render a cart with no total rather than saying so.
    const text = await response.text();
    let body: unknown = null;
    if (text.length > 0) {
      try {
        body = JSON.parse(text) as unknown;
      } catch {
        throw new StoreApiError("MALFORMED_RESPONSE", "the store returned invalid JSON", false);
      }
    }

    if (!response.ok) {
      const envelope = isRecord(body) ? body["error"] : undefined;
      if (isRecord(envelope)) {
        throw new StoreApiError(
          typeof envelope["code"] === "string" ? envelope["code"] : "STORE_ERROR",
          typeof envelope["message"] === "string" ? envelope["message"] : "The store refused.",
          envelope["retryable"] === true,
        );
      }
      throw new StoreApiError("STORE_ERROR", `The store returned ${response.status}.`, false);
    }
    return body;
  }

  async catalog(): Promise<readonly Product[]> {
    const body = requireRecord(await this.request("/catalog"), "catalog");
    const products = body["products"];
    if (!Array.isArray(products)) {
      throw new StoreApiError("MALFORMED_RESPONSE", "products is not an array", false);
    }
    return products.map((raw, index) => {
      const product = requireRecord(raw, `products[${index}]`);
      return {
        product_id: requireString(product["product_id"], "product_id"),
        line_key: requireString(product["line_key"], "line_key"),
        name: requireString(product["name"], "name"),
        price: requireString(product["price"], "price"),
        stock: requireNumber(product["stock"], "stock"),
      };
    });
  }

  async readCart(): Promise<StoreState> {
    const body = requireRecord(await this.request("/cart"), "cart response");
    return {
      state_version: requireNumber(body["state_version"], "state_version"),
      cart: narrowCart(body["cart"]),
      order: narrowOrder(body["order"]),
    };
  }

  async scenario(): Promise<Scenario> {
    const body = requireRecord(await this.request("/scenario"), "scenario");
    const label = body["label"];
    return {
      scenario_mode: requireString(body["scenario_mode"], "scenario_mode"),
      fault_profile: requireString(body["fault_profile"], "fault_profile"),
      fault_active: requireBoolean(body["fault_active"], "fault_active"),
      description: requireString(body["description"], "description"),
      // `exactOptionalPropertyTypes` is on, so an absent label must be omitted
      // rather than set to undefined - the two are different types here.
      ...(typeof label === "string" ? { label } : {}),
    };
  }

  async updateCart(productId: string, quantity: number, requestId: string): Promise<void> {
    await this.request("/cart/mutations", {
      method: "POST",
      body: JSON.stringify({ product_id: productId, quantity, request_id: requestId }),
    });
  }

  async applyDiscount(code: string): Promise<void> {
    await this.request("/discount", { method: "POST", body: JSON.stringify({ code }) });
  }

  async openConfirmation(): Promise<Confirmation> {
    const body = requireRecord(await this.request("/checkout/confirmations", { method: "POST", body: "{}" }), "confirmation");
    const consequence = requireRecord(body["consequence"], "consequence");
    return {
      confirmation_id: requireString(body["confirmation_id"], "confirmation_id"),
      status: requireString(body["status"], "status"),
      consequence: {
        action: requireString(consequence["action"], "consequence.action"),
        state_version: requireNumber(consequence["state_version"], "consequence.state_version"),
        cart_total: requireString(consequence["cart_total"], "consequence.cart_total"),
        item_count: requireNumber(consequence["item_count"], "consequence.item_count"),
      },
      expires_at: requireString(body["expires_at"], "expires_at"),
    };
  }

  async decide(confirmationId: string, approved: boolean): Promise<void> {
    await this.request(`/checkout/confirmations/${encodeURIComponent(confirmationId)}/decision`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    });
  }

  async cancelConfirmation(confirmationId: string): Promise<void> {
    await this.request(`/checkout/confirmations/${encodeURIComponent(confirmationId)}`, {
      method: "DELETE",
    });
  }

  async checkout(confirmationId: string, requestId: string): Promise<void> {
    await this.request("/checkout", {
      method: "POST",
      body: JSON.stringify({ confirmation_id: confirmationId, request_id: requestId }),
    });
  }
}

/**
 * A caller-generated idempotency key (Appendix D.2: 8..80 characters).
 *
 * Generated per *intent* rather than per click: a retry of the same intended
 * change reuses its key, so a double-submit returns the first persisted result
 * instead of mutating twice.
 */
export function requestId(prefix: string): string {
  return `${prefix}-${Math.random().toString(36).slice(2, 12)}`.padEnd(12, "0");
}
