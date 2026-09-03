/**
 * The checked-in Shopify theme bridge (FR-111, FR-112, FR-115, §11.3,
 * Appendix D.3, §16.5, AC-18).
 *
 * The bridge lives at `shopify_bridge/` because it is installed on a storefront,
 * not served by the harness — but it is plain JavaScript with an injectable
 * environment, and this is the repository's only JavaScript test runner. So its
 * logic is exercised here, from the same `npm run test` every other frontend
 * behaviour is held to.
 *
 * Four properties carry most of the weight, and each is a way the feature stops
 * being safe rather than a way it stops working:
 *
 * - **The credential leaves the URL before anything else can read it.** That is
 *   FR-111's entire point, and a bridge that stripped the fragment "eventually"
 *   would look identical in every other test.
 * - **The cart read refuses a redirect, a foreign final origin, a non-JSON body,
 *   and anything over 256 KiB.** Each of those is how an "independent
 *   observation" quietly becomes an observation of something else.
 * - **`verify_shopify_outcome` exists only while the pairing does.** A tool left
 *   registered against a dead pairing is an agent being offered an action that
 *   cannot happen.
 * - **The two tabs say the same thing.** §14 requires it, and two copies of a
 *   table at two origins is exactly the arrangement that drifts.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import type {
  BridgeFetch,
  BridgeModelContext,
  BridgeResponse,
  BridgeWindow,
} from "../../../../shopify_bridge/actionwitness-bridge.js";
import "../../../../shopify_bridge/actionwitness-bridge.js";
import { PAIRING_GUIDANCE } from "./components/ShopifyPairingPanel";

const STORE = "https://authorized-dev-store.example";
const HARNESS = "https://harness.example";
const ONE_TIME = "one-time-credential-abcdefgh";
const SESSION = "session-credential-ijklmnop";

/** The bridge, or a failure that names what is missing rather than a `null` deref. */
function bridgeApi(): NonNullable<typeof globalThis.ActionWitnessBridge> {
  const api = globalThis.ActionWitnessBridge;
  if (api === undefined) {
    throw new Error("the bridge did not install its namespace");
  }
  return api;
}

/**
 * A window the bridge can run in.
 *
 * Hand-built rather than jsdom's own, because the storefront the bridge runs on
 * is an HTTPS origin and jsdom's document is not. The one test that needs a
 * *real* location — the fragment strip — uses the real window instead, since
 * that is the one place a double could pass while the browser behaviour was
 * wrong.
 */
function storefrontWindow(overrides: Partial<BridgeWindow> = {}): BridgeWindow {
  return {
    document,
    location: {
      href: `${STORE}/en-gb/products/test-variant`,
      origin: STORE,
      pathname: "/en-gb/products/test-variant",
      search: "",
      hash: "",
    },
    history: { state: null, replaceState: vi.fn() },
    navigator: {},
    Shopify: { routes: { root: "/en-gb/" } },
    setTimeout: (handler: () => void, ms: number) => window.setTimeout(handler, ms),
    clearTimeout: (id: number) => {
      window.clearTimeout(id);
    },
    ...overrides,
  };
}

/** A field off an untrusted value, narrowed rather than asserted. */
function stringField(value: unknown, key: string): string {
  if (typeof value !== "object" || value === null) {
    return "";
  }
  const held: unknown = Reflect.get(value, key);
  return typeof held === "string" ? held : "";
}

function response(overrides: Partial<BridgeResponse> & { body?: string }): BridgeResponse {
  const body = overrides.body ?? "{}";
  return {
    ok: overrides.ok ?? true,
    status: overrides.status ?? 200,
    url: overrides.url ?? `${STORE}/en-gb/cart.js`,
    redirected: overrides.redirected ?? false,
    headers: overrides.headers ?? {
      get: (name: string) => (name.toLowerCase() === "content-type" ? "application/json" : null),
    },
    text: async () => body,
  };
}

const EMPTY_CART = JSON.stringify({ items: [], item_count: 0, currency: "USD", total_price: 0 });
const ONE_LINE_CART = JSON.stringify({
  items: [{ variant_id: 1234567890, quantity: 1, final_line_price: 2500 }],
  item_count: 1,
  currency: "USD",
  total_price: 2500,
});

/**
 * A whole storefront: the cart endpoint plus the three harness routes.
 *
 * Returns the calls so a test can assert what the bridge *sent* — which is
 * where "the credential never travels in a query string" is actually visible.
 */
function storefrontFetch(options: { readonly carts?: readonly string[] } = {}): {
  fetch: BridgeFetch;
  calls: { url: string; init: Record<string, unknown> }[];
} {
  const carts = [...(options.carts ?? [EMPTY_CART, ONE_LINE_CART])];
  const calls: { url: string; init: Record<string, unknown> }[] = [];
  const fetch: BridgeFetch = async (url, init) => {
    calls.push({ url, init });
    if (url.includes("cart.js")) {
      return response({ body: carts.shift() ?? EMPTY_CART });
    }
    if (url.endsWith("/redeem")) {
      return response({
        url,
        body: JSON.stringify({
          session_credential: SESSION,
          expires_at: new Date(Date.now() + 900_000).toISOString(),
          status: "paired",
        }),
      });
    }
    if (url.endsWith("/observations/before")) {
      return response({ url, body: JSON.stringify({ status: "armed", run_id: "run_1" }) });
    }
    if (url.endsWith("/verify")) {
      return response({ url, body: JSON.stringify({ status: "passed", run_id: "run_1" }) });
    }
    throw new Error(`unexpected request to ${url}`);
  };
  return { fetch, calls };
}

/** A model context that records registrations and whether they were aborted. */
function recordingModelContext(): {
  context: BridgeModelContext;
  registered: { name: string; aborted: () => boolean }[];
} {
  const registered: { name: string; aborted: () => boolean }[] = [];
  const context: BridgeModelContext = {
    registerTool: async (tool: unknown, options: { signal: AbortSignal }) => {
      registered.push({
        name: stringField(tool, "name"),
        aborted: () => options.signal.aborted,
      });
      return undefined;
    },
  };
  return { context, registered };
}

/**
 * Watch every route into browser storage.
 *
 * Asserting `localStorage.length === 0` afterwards would be the obvious test and
 * is the weaker one: it cannot tell "nothing was written" from "something was
 * written and removed", and this environment's `localStorage` is Node's rather
 * than jsdom's. Spying on the prototype both storages share catches a write
 * through either of them, and catches it at the moment it happens.
 */
function watchStorage(): () => number {
  const setItem = vi.spyOn(Storage.prototype, "setItem");
  return () => setItem.mock.calls.length;
}

afterEach(() => {
  window.history.replaceState(null, "", "/");
  for (const panel of Array.from(document.querySelectorAll("[data-actionwitness-panel]"))) {
    panel.remove();
  }
});

describe("the pairing fragment (FR-111)", () => {
  it("removes the credential from the visible URL as it reads it", () => {
    // Arrange — a real browser location, because this is the one behaviour a
    // double could satisfy while the browser did something else.
    window.history.replaceState(null, "", `/pages/test#actionwitness=pair_wxyz.${ONE_TIME}`);
    expect(window.location.hash).toContain(ONE_TIME);

    const writes = watchStorage();

    // Act
    const pairing = bridgeApi().readAndStripPairing(window);

    // Assert — the value is in hand and gone from everywhere a later reader looks.
    expect(pairing).toEqual({ pairingId: "pair_wxyz", credential: ONE_TIME });
    expect(window.location.hash).toBe("");
    expect(window.location.href).not.toContain(ONE_TIME);
    expect(document.documentElement.outerHTML).not.toContain(ONE_TIME);
    expect(document.cookie).not.toContain(ONE_TIME);
    expect(writes()).toBe(0);
  });

  it("strips a malformed pairing fragment rather than leaving it up", () => {
    // Arrange — a value with no separator: unusable, and still credential-shaped.
    window.history.replaceState(null, "", `/pages/test#actionwitness=${ONE_TIME}`);

    // Act
    const pairing = bridgeApi().readAndStripPairing(window);

    // Assert — fails closed *and* cleans up. A bridge that only stripped on the
    // happy path would leave the string in the address bar of every failed
    // attempt, which is when a person is most likely to paste the URL somewhere.
    expect(pairing).toBeNull();
    expect(window.location.href).not.toContain(ONE_TIME);
  });

  it("accepts the explicit pair spelling of the launch fragment", () => {
    // Arrange
    window.history.replaceState(
      null,
      "",
      `/#actionwitness_pairing=pair_1&actionwitness_credential=${ONE_TIME}`,
    );

    // Act / Assert
    expect(bridgeApi().readAndStripPairing(window)).toEqual({
      pairingId: "pair_1",
      credential: ONE_TIME,
    });
    expect(window.location.href).not.toContain(ONE_TIME);
  });

  it("leaves a fragment that is not ours alone", () => {
    // Arrange — a theme's own anchor. Clearing it would break the store's
    // navigation for a bridge that has no business touching it.
    window.history.replaceState(null, "", "/pages/test#main-content");

    // Act / Assert
    expect(bridgeApi().readAndStripPairing(window)).toBeNull();
    expect(window.location.hash).toBe("#main-content");
  });
});

describe("the cart observation (FR-112)", () => {
  it("builds the cart URL from the locale-aware storefront root", () => {
    // Arrange / Act
    const url = bridgeApi().resolveCartUrl(storefrontWindow(), STORE);

    // Assert — a hard-coded `/cart.js` returns the wrong locale's cart, or a
    // redirect, on a localised storefront.
    expect(url).toBe(`${STORE}/en-gb/cart.js`);
  });

  it("refuses a routes root that would take the read off the store origin", () => {
    // Arrange — a theme or app that set an absolute root.
    const win = storefrontWindow({ Shopify: { routes: { root: "https://elsewhere.example/" } } });

    // Act / Assert
    expect(() => bridgeApi().resolveCartUrl(win, STORE)).toThrow(/store origin/);
  });

  async function read(
    res: BridgeResponse,
  ): Promise<{ cart: Record<string, unknown>; capturePath: string }> {
    const fetch: BridgeFetch = async () => res;
    return await bridgeApi().readCartDocument(
      { window: storefrontWindow(), fetch, now: () => new Date() },
      STORE,
    );
  }

  it("refuses a redirected cart read", async () => {
    // Act / Assert — a redirected read is a read of something that is not this cart.
    await expect(read(response({ redirected: true, body: EMPTY_CART }))).rejects.toThrow(
      /redirect/i,
    );
  });

  it("refuses a final URL outside the configured store origin", async () => {
    // Act / Assert
    await expect(
      read(response({ url: "https://elsewhere.example/cart.js", body: EMPTY_CART })),
    ).rejects.toThrow(/configured origin/);
  });

  it("refuses a non-JSON body rather than parsing an error page as a cart", async () => {
    // Arrange — on a storefront this is a consent interstitial or an error page.
    const html = response({
      headers: { get: () => "text/html; charset=utf-8" },
      body: "<!doctype html><title>Just a moment…</title>",
    });

    // Act / Assert
    await expect(read(html)).rejects.toThrow(/not JSON/);
  });

  it("refuses a payload over 256 KiB, whatever its declared length said", async () => {
    // Arrange — an honest content type, a silent (absent) length, and a body
    // one byte past the cap. The declared length is a claim; the bytes are not.
    const oversized = JSON.stringify({ note: "x".repeat(bridgeApi().MAX_CART_BYTES) });
    const res = response({
      headers: {
        get: (name: string) => (name.toLowerCase() === "content-type" ? "application/json" : null),
      },
      body: oversized,
    });

    // Act / Assert
    await expect(read(res)).rejects.toThrow(/256 KiB/);
  });

  it("refuses a declared length over the cap before reading a body", async () => {
    // Arrange
    let bodyRead = false;
    const res: BridgeResponse = {
      ...response({ body: EMPTY_CART }),
      headers: {
        get: (name: string) =>
          name.toLowerCase() === "content-type"
            ? "application/json"
            : String(bridgeApi().MAX_CART_BYTES + 1),
      },
      text: async () => {
        bodyRead = true;
        return EMPTY_CART;
      },
    };

    // Act / Assert
    await expect(read(res)).rejects.toThrow(/256 KiB/);
    expect(bodyRead).toBe(false);
  });

  it("records the capture path without query or fragment (FR-117)", async () => {
    // Act
    const observed = await read(response({ body: EMPTY_CART }));

    // Assert
    expect(observed.capturePath).toBe("/en-gb/cart.js");
  });
});

describe("origin configuration (FR-110)", () => {
  it.each([
    ["http://shop.example", /HTTPS/],
    ["https://shop.example/path", /exact origin/],
    ["not-a-url", /not a URL/],
  ])("refuses %s", (value, expected) => {
    // Act / Assert — the store origin is server-held and exact; anything that
    // merely looks configured would make every later comparison meaningless.
    expect(() => bridgeApi().requireHttpsOrigin(value, "the store origin")).toThrow(expected);
  });
});

describe("the pairing lifecycle (§16.5, FR-115)", () => {
  function makeBridge(
    options: {
      readonly fetch?: BridgeFetch;
      readonly modelContext?: BridgeModelContext | null;
      readonly onChange?: (view: { state: string }) => void;
    } = {},
  ) {
    return bridgeApi().createBridge({
      window: storefrontWindow(),
      fetch: options.fetch ?? storefrontFetch().fetch,
      harnessOrigin: HARNESS,
      storeOrigin: STORE,
      resolveModelContext: () => options.modelContext ?? null,
      ...(options.onChange === undefined ? {} : { onChange: options.onChange }),
    });
  }

  it("redeems once, captures the starting cart, and arms", async () => {
    // Arrange
    const { fetch, calls } = storefrontFetch();
    const bridge = makeBridge({ fetch });

    // Act
    const view = await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });

    // Assert — §16.5's `armed`, and the agent is who acts next.
    expect(view.state).toBe("armed");
    expect(view.actor).toBe("agent");
    expect(view.pairingSuffix).toBe("abcd");
    expect(calls.map((call) => call.url)).toEqual([
      `${HARNESS}/api/v1/shopify/pairings/pair_abcd/redeem`,
      `${STORE}/en-gb/cart.js`,
      `${HARNESS}/api/v1/shopify/pairings/pair_abcd/observations/before`,
    ]);
    bridge.dispose();
  });

  it("carries credentials in the Authorization header and never in a URL", async () => {
    // Arrange
    const { fetch, calls } = storefrontFetch();
    const bridge = makeBridge({ fetch });

    // Act
    await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });

    // Assert — §15.7: never in a query string, and the redeemed session
    // credential is what every later request uses.
    for (const call of calls) {
      expect(call.url).not.toContain(ONE_TIME);
      expect(call.url).not.toContain(SESSION);
      const body: unknown = call.init["body"];
      expect(typeof body === "string" ? body : "").not.toContain(ONE_TIME);
    }
    const harnessCalls = calls.filter((call) => call.url.startsWith(HARNESS));
    const headers = harnessCalls.map((call) => stringField(call.init["headers"], "Authorization"));
    expect(headers).toEqual([
      `Bearer ${ONE_TIME}`,
      `Bearer ${SESSION}`,
    ]);
    // Ambient credentials are not the authorization boundary here; the pairing is.
    expect(harnessCalls.every((call) => call.init["credentials"] === "omit")).toBe(true);
    bridge.dispose();
  });

  it("keeps both credentials out of storage and out of the page", async () => {
    // Arrange
    const writes = watchStorage();
    const bridge = makeBridge();

    // Act
    await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });
    bridge.attachPanel();

    // Assert — the session credential lives in a closure. Nothing a later
    // reader can reach holds it, and nothing was written on the way there.
    expect(writes()).toBe(0);
    expect(document.cookie).not.toContain(SESSION);
    expect(document.cookie).not.toContain(ONE_TIME);
    expect(document.body.innerHTML).not.toContain(SESSION);
    expect(document.body.innerHTML).not.toContain(ONE_TIME);
    expect(JSON.stringify(bridge.view())).not.toContain(SESSION);
    bridge.dispose();
  });

  it("registers verify_shopify_outcome only once the pairing is armed", async () => {
    // Arrange
    const { context, registered } = recordingModelContext();
    const bridge = makeBridge({ modelContext: context });

    // Assert — nothing before the journey starts.
    expect(bridge.isToolRegistered()).toBe(false);

    // Act
    await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });

    // Assert — Appendix D.3's schema exactly: no arguments, so the pairing
    // cannot be passed in and cannot leak out through the tool surface.
    expect(registered.map((entry) => entry.name)).toEqual(["verify_shopify_outcome"]);
    expect(bridge.isToolRegistered()).toBe(true);
    bridge.dispose();
  });

  it("unregisters the tool when verification reaches a terminal state", async () => {
    // Arrange
    const { context, registered } = recordingModelContext();
    const bridge = makeBridge({ modelContext: context });
    await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });

    // Act
    const result = await bridge.verify();

    // Assert — §16.5: terminal means the tool goes away, and the abort *is* the
    // unregistration, so it cannot be forgotten separately from the register.
    expect(bridge.view().state).toBe("passed");
    expect(result.isError).toBeUndefined();
    expect(registered[0]?.aborted()).toBe(true);
    expect(bridge.isToolRegistered()).toBe(false);
  });

  it("unregisters the tool on teardown", async () => {
    // Arrange
    const { context, registered } = recordingModelContext();
    const bridge = makeBridge({ modelContext: context });
    await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });

    // Act — what the `pagehide` listener does when the tab goes away.
    bridge.dispose();

    // Assert
    expect(registered[0]?.aborted()).toBe(true);
    expect(bridge.isToolRegistered()).toBe(false);
  });

  it("stops safely rather than reporting a verdict when the cart cannot be read", async () => {
    // Arrange — the store is reachable for pairing, and the final cart read
    // fails. This is the exact moment a bridge could decide the run "failed".
    const backing = storefrontFetch();
    let cartReads = 0;
    const fetch: BridgeFetch = async (url, init) => {
      if (url.includes("cart.js")) {
        cartReads += 1;
        if (cartReads > 1) {
          return response({ ok: false, status: 503, body: "" });
        }
      }
      return await backing.fetch(url, init);
    };
    const { context, registered } = recordingModelContext();
    const bridge = makeBridge({ fetch, modelContext: context });
    await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });

    // Act
    const result = await bridge.verify();

    // Assert — an observation that did not happen is never a failed contract.
    expect(result.isError).toBe(true);
    expect(bridge.view().state).toBe("error");
    expect(bridge.view().recovery).not.toBe("");
    expect(registered[0]?.aborted()).toBe(true);
  });

  it("reads the pairing state whether or not the harness wraps its response", async () => {
    // Arrange — the pairing routes are being written alongside this file, and
    // both a bare record and a `{ pairing: … }` envelope are in use across this
    // API. A cosmetic envelope choice must not strand an operator's storefront
    // tab with no state and no instruction.
    const fetch: BridgeFetch = async (url) => {
      if (url.includes("cart.js")) {
        return response({ body: EMPTY_CART });
      }
      if (url.endsWith("/redeem")) {
        return response({
          url,
          body: JSON.stringify({
            bridge_session_credential: SESSION,
            pairing: { status: "paired", expires_at: "2026-09-03T12:15:00Z" },
          }),
        });
      }
      return response({
        url,
        body: JSON.stringify({ pairing: { status: "armed", run_id: "run_9" } }),
      });
    };
    const bridge = makeBridge({ fetch });

    // Act
    const view = await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });

    // Assert
    expect(view.state).toBe("armed");
    expect(view.runId).toBe("run_9");
    expect(view.expiresAt).toBe("2026-09-03T12:15:00Z");
    bridge.dispose();
  });

  it("refuses to verify a pairing that is not armed", async () => {
    // Arrange — never started.
    const bridge = makeBridge();

    // Act
    const result = await bridge.verify();

    // Assert
    expect(result.isError).toBe(true);
    expect(result.content[0]?.text).toContain("not armed");
    bridge.dispose();
  });

  it("shows the pairing, expiry, actor and next action on the storefront (§14)", async () => {
    // Arrange
    const bridge = makeBridge();
    await bridge.start({ pairingId: "pair_abcd", credential: ONE_TIME });

    // Act
    bridge.attachPanel();

    // Assert — the storefront tab's half of the two-tab agreement, in words.
    const panel = document.querySelector("[data-actionwitness-panel]");
    const text = panel?.textContent ?? "";
    expect(text).toContain("…abcd");
    expect(text).toContain("armed");
    expect(text).toContain("agent");
    expect(text).toContain("verify_shopify_outcome");
    expect(text).toContain("expires");
    bridge.dispose();
    expect(document.querySelector("[data-actionwitness-panel]")).toBeNull();
  });

  it("reports a bounded recovery instruction in every terminal state", () => {
    // Assert — §14 forbids a silent disabled state, so there must be no state
    // without a way out. Asserted over the table rather than per state, because
    // the failure this guards against is a *new* state added without one.
    for (const [state, guidance] of Object.entries(bridgeApi().PAIRING_GUIDANCE)) {
      expect(guidance.recovery, `${state} has no recovery instruction`).not.toBe("");
      expect(guidance.next, `${state} does not say what happens next`).not.toBe("");
    }
  });
});

describe("the two tabs (§14)", () => {
  it("renders the same guidance table as the harness panel", () => {
    // Assert — the storefront tab and the harness tab live at two origins and
    // cannot share a module, so §14's "both tabs agree" is only true while
    // these two copies are identical. Drift fails here rather than showing a
    // person two different answers about whose turn it is.
    expect(bridgeApi().PAIRING_GUIDANCE).toEqual(PAIRING_GUIDANCE);
  });

  it("derives the same displayed pairing suffix on both sides", () => {
    // Assert — a person matches the two tabs by these four characters.
    expect(bridgeApi().pairingSuffix("pair_abcd")).toBe("abcd");
    expect(bridgeApi().pairingSuffix("ab")).toBe("ab");
  });
});
