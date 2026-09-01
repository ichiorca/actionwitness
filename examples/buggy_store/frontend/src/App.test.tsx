/**
 * Storefront gates (spec v1.9 §15.5, §20.4, §14; AC-03, AC-09; 003-T7).
 *
 * The whole suite runs in a jsdom with no `document.modelContext`, which is the
 * point rather than a limitation: AC-09's progressive-enhancement claim rests on
 * there being a real human path that never needed the browser-tool surface, and
 * the cheapest way to prove that is to never provide one.
 *
 * The load-bearing test is `test the storefront shows unchanged canonical state
 * when the injected fault is active`. That is AC-03 and AC-04 from the human
 * side: the shopper clicks "Apply SAVE20", the store answers success, and the
 * page still reads 25.00 — because it renders what the server says the cart *is*
 * rather than what the last response *claimed*. A UI that predicted the total
 * locally would show 20.00 and hide the defect the whole product exists to find.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { App } from "./App";
import { StoreApiError, StoreClient, requestId } from "./api";

interface CartShape {
  items: Record<string, { product_id: string; quantity: number; unit_price: string }>;
  discount: { code: string; amount: string } | null;
  subtotal: string;
  total: string;
}

/**
 * A stand-in for the store, faithful to the parts of §15.5 this UI touches.
 *
 * Hand-written rather than a mocking library so the response *shapes* are
 * visible in the test file: this UI's job is narrowing untrusted JSON, and a
 * fixture that auto-generated its own shapes would never disagree with the code.
 */
class FakeStore {
  cart: CartShape = { items: {}, discount: null, subtotal: "0.00", total: "0.00" };
  order: { created: boolean; order_id: string | null } = { created: false, order_id: null };
  version = 1;
  scenario = {
    scenario_mode: "post_fix",
    fault_profile: "none",
    fault_active: false,
    description: "No fault is injected. The store behaves correctly.",
  } as Record<string, unknown>;
  /** When true, `apply_discount` reports success and changes nothing (§13.3). */
  discountFault = false;
  confirmationStatus = "pending";
  /** When set, `update_cart` refuses with this coded envelope (§15.8). */
  refuseMutation: { code: string; message: string } | null = null;
  readonly calls: string[] = [];

  fetch = async (input: string, init?: RequestInit): Promise<Response> => {
    const path = input.replace("/demo/api/v1/store", "");
    const method = init?.method ?? "GET";
    this.calls.push(`${method} ${path}`);

    if (path === "/catalog") {
      return this.ok({
        products: [
          {
            product_id: "mug-ceramic-001",
            line_key: "mug",
            name: "Ceramic Mug",
            price: "25.00",
            stock: 20,
          },
        ],
      });
    }
    if (path === "/cart") {
      return this.ok({ state_version: this.version, cart: this.cart, order: this.order });
    }
    if (path === "/scenario") return this.ok(this.scenario);

    if (path === "/cart/mutations" && method === "POST") {
      if (this.refuseMutation !== null) {
        return this.refuse(this.refuseMutation.code, this.refuseMutation.message, 409);
      }
      const body = this.jsonBody<{ quantity: number }>(init);
      if (body.quantity === 0) {
        this.cart = { items: {}, discount: null, subtotal: "0.00", total: "0.00" };
      } else {
        const subtotal = (25 * body.quantity).toFixed(2);
        this.cart = {
          items: {
            mug: { product_id: "mug-ceramic-001", quantity: body.quantity, unit_price: "25.00" },
          },
          discount: null,
          subtotal,
          total: subtotal,
        };
      }
      this.version += 1;
      return this.ok({ status: "success", state_version: this.version, cart: this.cart });
    }

    if (path === "/discount" && method === "POST") {
      const subtotal = Number(this.cart.subtotal);
      const amount = (subtotal * 0.2).toFixed(2);
      const applied = {
        ...this.cart,
        discount: { code: "SAVE20", amount },
        total: (subtotal - Number(amount)).toFixed(2),
      };
      if (this.discountFault) {
        // Reports the discounted cart; persists nothing, and the version stays.
        return this.ok({ status: "success", state_version: this.version, cart: applied });
      }
      this.cart = applied;
      this.version += 1;
      return this.ok({ status: "success", state_version: this.version, cart: this.cart });
    }

    if (path === "/checkout/confirmations" && method === "POST") {
      return this.ok(
        {
          confirmation_id: "confirmation-0001",
          status: "pending",
          consequence: {
            action: "proceed_to_checkout",
            state_version: this.version,
            cart_total: this.cart.total,
            item_count: Object.keys(this.cart.items).length,
          },
          expires_at: "2026-01-01T00:01:00Z",
        },
        201,
      );
    }
    if (path.endsWith("/decision") && method === "POST") {
      const body = this.jsonBody<{ approved: boolean }>(init);
      this.confirmationStatus = body.approved ? "approved" : "denied";
      return this.ok({ confirmation_id: "confirmation-0001", status: this.confirmationStatus });
    }
    if (path.startsWith("/checkout/confirmations/") && method === "DELETE") {
      this.confirmationStatus = "cancelled";
      return this.ok({ confirmation_id: "confirmation-0001", status: "cancelled" });
    }
    if (path === "/checkout" && method === "POST") {
      if (this.confirmationStatus !== "approved") {
        return this.refuse("CONFIRMATION_REQUIRED", "No approval.", 409);
      }
      this.order = { created: true, order_id: "order-0001" };
      this.version += 1;
      return this.ok({ status: "success", state_version: this.version, order_id: "order-0001" });
    }
    return this.refuse("STORE_ERROR", `unrouted ${method} ${path}`, 404);
  };

  /**
   * The request body, narrowed to the string the storefront actually sends.
   *
   * `RequestInit["body"]` is `BodyInit | null`, which includes `Blob`,
   * `FormData`, and `URLSearchParams` — all of which `String()` would turn into
   * `"[object Object]"` and `JSON.parse` would then reject with a message about
   * character 1. Throwing here says what went wrong instead, and asserts
   * something true about `api.ts`: it sends JSON strings and nothing else.
   */
  private jsonBody<T>(init: RequestInit | undefined): T {
    const raw = init?.body;
    if (typeof raw !== "string") {
      throw new TypeError(`the storefront sends JSON strings; got ${typeof raw}`);
    }
    return JSON.parse(raw) as T;
  }

  private ok(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  }

  private refuse(code: string, message: string, status: number): Response {
    return new Response(
      JSON.stringify({ error: { code, message, retryable: false, details: {} } }),
      { status, headers: { "Content-Type": "application/json" } },
    );
  }
}

function renderStore(store: FakeStore) {
  const client = new StoreClient("ws-1", store.fetch as unknown as typeof fetch);
  return { user: userEvent.setup(), ...render(<App client={client} />) };
}

let store: FakeStore;

beforeEach(() => {
  store = new FakeStore();
});

describe("the storefront works without any agent support", () => {
  it("renders with no document.modelContext present", async () => {
    // AC-09: the human path must not need the browser-tool surface.
    expect("modelContext" in document).toBe(false);

    renderStore(store);
    expect(await screen.findByRole("heading", { name: "Buggy Store" })).toBeDefined();
    expect(await screen.findByRole("button", { name: "Add Ceramic Mug" })).toBeDefined();
  });

  it("never touches the harness API", async () => {
    renderStore(store);
    await screen.findByRole("button", { name: "Add Ceramic Mug" });
    // §15.5: only the store's own UI and the integration call this surface, and
    // this UI calls nothing else at all.
    expect(store.calls.every((call) => !call.includes("/api/v1/"))).toBe(true);
  });
});

describe("the cart shows canonical state", () => {
  it("starts empty", async () => {
    renderStore(store);
    expect(await screen.findByTestId("empty-cart")).toBeDefined();
    expect((await screen.findByTestId("total")).textContent).toBe("0.00");
  });

  it("re-reads the server after a mutation rather than predicting locally", async () => {
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: /Add Ceramic Mug/ }));

    await waitFor(() => {
      expect(screen.getByTestId("subtotal").textContent).toBe("25.00");
    });
    expect(screen.getByTestId("state-version").textContent).toBe("2");
    // A read follows the write; that is what makes this view comparable with
    // the adapter's observation under AC-03.
    expect(store.calls.filter((call) => call === "GET /cart").length).toBeGreaterThan(1);
  });

  it("applies a discount and shows the reduced total", async () => {
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: /Add Ceramic Mug/ }));
    await waitFor(() => expect(screen.getByTestId("subtotal").textContent).toBe("25.00"));

    await user.click(screen.getByRole("button", { name: "Apply SAVE20" }));
    await waitFor(() => expect(screen.getByTestId("total").textContent).toBe("20.00"));
    expect(screen.getByTestId("discount").textContent).toContain("SAVE20");
  });

  it("removes a line", async () => {
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: /Add Ceramic Mug/ }));
    await waitFor(() => expect(screen.getByTestId("line-mug")).toBeDefined());

    await user.click(screen.getByRole("button", { name: "Remove mug" }));
    await waitFor(() => expect(screen.getByTestId("empty-cart")).toBeDefined());
  });
});

describe("the injected fault is visible and labelled", () => {
  beforeEach(() => {
    store.discountFault = true;
    store.scenario = {
      scenario_mode: "pre_fix",
      fault_profile: "discount_reported_but_not_applied",
      fault_active: true,
      description:
        "apply_discount returns an apparent success response while canonical cart state retains no discount and an unchanged total.",
      label: "injected unsafe demo behaviour",
    };
  });

  it("labels the mode as an injected demo defect", async () => {
    // §20.4: "the UI clearly labels unsafe injected modes."
    renderStore(store);
    const banner = await screen.findByTestId("scenario-banner");
    expect(banner.textContent).toContain("Injected unsafe demo behaviour is active");
    expect(banner.textContent).toContain("discount_reported_but_not_applied");
    expect(banner.textContent).toContain("not a real fault");
    expect(banner.getAttribute("role")).toBe("alert");
  });

  it("shows unchanged canonical state after the tool reports success", async () => {
    // AC-03 and AC-04 from the human side. The store answered "success"; the
    // page shows 25.00, because it renders what the cart *is*.
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: /Add Ceramic Mug/ }));
    await waitFor(() => expect(screen.getByTestId("subtotal").textContent).toBe("25.00"));

    const versionBefore = screen.getByTestId("state-version").textContent;
    await user.click(screen.getByRole("button", { name: "Apply SAVE20" }));

    await waitFor(() => expect(screen.getByText("Applied SAVE20.")).toBeDefined());
    expect(screen.getByTestId("total").textContent).toBe("25.00");
    expect(screen.getByTestId("discount").textContent).toBe("None");
    expect(screen.getByTestId("state-version").textContent).toBe(versionBefore);
  });

  it("says a recorded but disabled fault is not running", async () => {
    // FR-011: post_fix keeps the comparison fault recorded and inactive.
    store.discountFault = false;
    store.scenario = {
      scenario_mode: "post_fix",
      fault_profile: "discount_reported_but_not_applied",
      fault_active: false,
      description: "…",
    };
    renderStore(store);
    const banner = await screen.findByTestId("scenario-banner");
    expect(banner.textContent).toContain("recorded but disabled");
    expect(banner.getAttribute("role")).toBe("status");
  });
});

describe("checkout asks a person first", () => {
  it("shows the exact consequence with no option preselected", async () => {
    // §14 step 4: "no option is preselected".
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: /Add Ceramic Mug/ }));
    await waitFor(() => expect(screen.getByTestId("subtotal").textContent).toBe("25.00"));

    await user.click(screen.getByRole("button", { name: "Proceed to checkout" }));
    const dialog = await screen.findByTestId("confirmation");

    expect(within(dialog).getByTestId("confirm-total").textContent).toBe("25.00");
    expect(within(dialog).getByRole("button", { name: "Approve once" })).toBeDefined();
    expect(within(dialog).getByRole("button", { name: "Deny" })).toBeDefined();
    for (const control of within(dialog).getAllByRole("button")) {
      expect(control.getAttribute("aria-pressed")).toBeNull();
      expect(control.hasAttribute("autofocus")).toBe(false);
    }
  });

  it("creates an order only after an approval", async () => {
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: /Add Ceramic Mug/ }));
    await user.click(await screen.findByRole("button", { name: "Proceed to checkout" }));
    await user.click(await screen.findByRole("button", { name: "Approve once" }));

    await waitFor(() => expect(screen.getByTestId("order").textContent).toContain("order-0001"));
  });

  it("creates no order when the person denies", async () => {
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: /Add Ceramic Mug/ }));
    await user.click(await screen.findByRole("button", { name: "Proceed to checkout" }));
    await user.click(await screen.findByRole("button", { name: "Deny" }));

    await waitFor(() => expect(screen.getByText("Nothing was ordered.")).toBeDefined());
    expect(screen.queryByTestId("order")).toBeNull();
    expect(store.calls.some((call) => call === "POST /checkout")).toBe(false);
  });

  it("creates no order when the person cancels", async () => {
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: /Add Ceramic Mug/ }));
    await user.click(await screen.findByRole("button", { name: "Proceed to checkout" }));
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.getByText(/Nothing was ordered/)).toBeDefined());
    expect(screen.queryByTestId("order")).toBeNull();
  });
});

describe("failures are shown to the person, not swallowed", () => {
  it("renders the store's stable error code", async () => {
    // §15.8: the code is the contract a reader can act on, so it reaches the
    // page rather than being flattened into "something went wrong".
    store.refuseMutation = {
      code: "IDEMPOTENCY_KEY_REUSED",
      message: "That request ID was already used with a different payload.",
    };
    const { user } = renderStore(store);
    await user.click(await screen.findByRole("button", { name: "Add Ceramic Mug" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("IDEMPOTENCY_KEY_REUSED");
    expect(alert.textContent).toContain("already used");
    // The cart is re-read after the refusal, so the page never shows a change
    // the store did not make.
    expect(screen.getByTestId("empty-cart")).toBeDefined();
  });

  it("does not render an unknown error's text", async () => {
    const exploding = new StoreClient("ws-1", (async () => {
      throw new Error("ECONNREFUSED /internal/path/secret.py");
    }) as unknown as typeof fetch);

    render(<App client={exploding} />);
    const alert = await screen.findByRole("alert");
    // §15.8 keeps internals away from a browser; the message is generic.
    expect(alert.textContent).toBe("The store could not be reached.");
    expect(alert.textContent).not.toContain("secret.py");
  });
});

describe("request identifiers", () => {
  it("fit the schema bound Appendix D.2 declares", () => {
    for (let index = 0; index < 50; index += 1) {
      const identifier = requestId("add");
      expect(identifier.length).toBeGreaterThanOrEqual(8);
      expect(identifier.length).toBeLessThanOrEqual(80);
    }
  });
});

describe("the API client narrows untrusted responses", () => {
  it("refuses a cart whose total is missing", async () => {
    const client = new StoreClient("ws-1", (async () =>
      new Response(JSON.stringify({ state_version: 1, cart: { items: {} }, order: {} }), {
        status: 200,
      })) as unknown as typeof fetch);

    await expect(client.readCart()).rejects.toBeInstanceOf(StoreApiError);
  });

  it("refuses a non-JSON body rather than guessing", async () => {
    const client = new StoreClient("ws-1", (async () =>
      new Response("<html>gateway</html>", { status: 200 })) as unknown as typeof fetch);

    await expect(client.catalog()).rejects.toMatchObject({ code: "MALFORMED_RESPONSE" });
  });

  it("surfaces the store's error code from the envelope", async () => {
    const client = new StoreClient("ws-1", (async () =>
      new Response(
        JSON.stringify({
          error: { code: "IDEMPOTENCY_KEY_REUSED", message: "reused", retryable: false },
        }),
        { status: 409 },
      )) as unknown as typeof fetch);

    await expect(client.applyDiscount("SAVE20")).rejects.toMatchObject({
      code: "IDEMPOTENCY_KEY_REUSED",
      retryable: false,
    });
  });

  it("omits an absent scenario label rather than setting it undefined", async () => {
    // `exactOptionalPropertyTypes` makes those different types, and a UI that
    // rendered `undefined` as a label would print the word.
    const client = new StoreClient("ws-1", (async () =>
      new Response(
        JSON.stringify({
          scenario_mode: "post_fix",
          fault_profile: "none",
          fault_active: false,
          description: "…",
        }),
        { status: 200 },
      )) as unknown as typeof fetch);

    const scenario = await client.scenario();
    expect("label" in scenario).toBe(false);
  });
});
