/**
 * ActionWitness Shopify theme bridge (spec §11.3, §12.12, §15.7, §16.5,
 * FR-110–FR-119, Appendix D.3).
 *
 * A dependency-free classic script an operator installs on **one authorized
 * development store**. It is deliberately not built, bundled, or transpiled:
 * the whole artifact is the file a merchant can read before they paste it into
 * their theme, and a build step would put a bundle between them and that.
 *
 * ## What it does, in order
 *
 * 1. Reads the one-time pairing credential out of the URL **fragment** and
 *    removes it from the visible URL synchronously, at script-execution time.
 * 2. Redeems it once for a session credential that lives in a closure and
 *    nowhere else.
 * 3. Captures the shopper session's cart from the **locale-aware**
 *    `window.Shopify.routes.root + 'cart.js'` and submits it as the `before`
 *    observation.
 * 4. Registers `verify_shopify_outcome` while — and only while — the pairing is
 *    valid, and unregisters it on every terminal state and on teardown.
 *
 * ## Why the fragment, and why it is stripped first
 *
 * FR-111: the credential is delivered in a URL fragment, "consumed by a bridge
 * snippet loaded before unrelated third-party theme scripts, removed from the
 * visible URL immediately". A fragment is never sent to the server and never
 * lands in an access log — that is why it is the fragment and not a query
 * string. But it *is* readable by every script on the page, so the window in
 * which a third-party theme script (a chat widget, an analytics tag, a review
 * app) could read `location.hash` is the whole exposure. Closing that window is
 * the reason this file is a blocking classic script at the top of `<head>`
 * rather than a module: `type="module"` is deferred, so it would run *after*
 * every classic third-party tag on the page — which is precisely backwards.
 *
 * Nothing here is stored. No `localStorage`, no `sessionStorage`, no cookie, no
 * cart token, no raw credential in the DOM. A reload therefore ends the
 * pairing, and the bridge says so rather than appearing to be idle
 * (§14: a bounded recovery instruction, never a silent disabled state).
 *
 * ## What it will not do
 *
 * There is no code path here that adds to the cart, changes a line, clears a
 * cart, navigates to checkout, creates an order, logs a customer in, or touches
 * payment. Shopify's own WebMCP tools perform the cart actions; this bridge only
 * *observes*. `tests/architecture/test_shopify_bridge_artifact.py` fails the
 * build if a mutation or navigation ever appears in this file, because "we did
 * not mean to" is not a property an authorized development store can rely on.
 *
 * ## Testing seam
 *
 * Everything reachable is exposed on `ActionWitnessBridge` and every capability
 * this file needs — `window`, `fetch`, the clock, the model context — is passed
 * in rather than reached for. That is what lets the interesting parts (fragment
 * stripping, origin refusal, the 256 KiB cap) be unit-tested in jsdom with no
 * storefront, and it is also why the auto-start below is opt-in: the script only
 * starts itself when a real `<script data-actionwitness …>` element loaded it.
 */
(function (globalScope) {
  "use strict";

  /** Reported to the harness as FR-117's `bridge_version`. */
  var BRIDGE_VERSION = "1.0.0";

  /** FR-112's hard ceiling on one `/cart.js` payload. */
  var MAX_CART_BYTES = 256 * 1024;

  /** Appendix D.3's tool. The pairing is session state and never an argument. */
  var TOOL_NAME = "verify_shopify_outcome";

  var TOOL_DESCRIPTION =
    "Capture the final same-session Shopify cart, verify the armed cart-only outcome " +
    "contract, and return a compact deterministic verdict. This tool does not navigate " +
    "to checkout or create an order.";

  /**
   * §16.5's states, each with the one thing that happens next and the one way
   * out if it does not.
   *
   * This table is duplicated, on purpose, in the harness page's pairing panel
   * (`components/ShopifyPairingPanel.tsx`). §14 requires the storefront tab and
   * the harness tab to agree about the current actor and the next action, and
   * two tabs at two origins cannot share a module. `shopifyBridge.test.ts`
   * asserts the two copies are identical, so drift fails a test rather than
   * showing a person two different answers.
   *
   * Every row carries a `recovery`. A pairing that has expired, been cancelled,
   * or lost its tab must say what to do about it — a disabled control with no
   * sentence beside it is the failure mode §14 names.
   */
  var PAIRING_GUIDANCE = {
    created: {
      actor: "operator",
      next: "Open the launch URL in a storefront tab to connect this pairing.",
      recovery:
        "If the storefront tab is already open, reload it: the one-time credential is spent, " +
        "so create a new pairing and open the new launch URL.",
    },
    paired: {
      actor: "system",
      next: "Capturing the starting cart from this shopper session.",
      recovery:
        "If this does not move on within a few seconds, the cart could not be read. " +
        "Create a new pairing and open its launch URL in a fresh storefront tab.",
    },
    armed: {
      actor: "agent",
      next:
        "Use the store's own tools to place exactly one configured test variant in the cart, " +
        "then invoke verify_shopify_outcome.",
      recovery:
        "To start over, empty the cart on the store and create a new pairing. " +
        "Cleanup is never part of the evaluated journey.",
    },
    verifying: {
      actor: "system",
      next: "Evaluating the final cart against the armed contract.",
      recovery:
        "If no verdict appears, the pairing expires on its own and records no pass. " +
        "Create a new pairing to try again.",
    },
    passed: {
      actor: "operator",
      next: "Read the verdict and the run's findings in the harness.",
      recovery: "Create a new pairing to run the journey again.",
    },
    passed_with_warnings: {
      actor: "operator",
      next: "Read the warnings on this run before treating it as a clean pass.",
      recovery: "Create a new pairing to run the journey again.",
    },
    failed: {
      actor: "operator",
      next: "Read the findings: the cart the store reported and the cart observed disagree.",
      recovery: "Create a new pairing to run the journey again.",
    },
    expired: {
      actor: "operator",
      next: "This pairing expired. Expiry never converts an incomplete trial into a pass.",
      recovery: "Create a new pairing and open its launch URL in a fresh storefront tab.",
    },
    cancelled: {
      actor: "operator",
      next: "This pairing was cancelled.",
      recovery: "Create a new pairing when you are ready to run the journey again.",
    },
    error: {
      actor: "operator",
      next: "The pairing stopped safely and recorded no verdict.",
      recovery: "Create a new pairing; nothing from the stopped attempt is carried forward.",
    },
  };

  var TERMINAL_STATES = [
    "passed",
    "passed_with_warnings",
    "failed",
    "expired",
    "cancelled",
    "error",
  ];

  function isTerminal(state) {
    return TERMINAL_STATES.indexOf(state) !== -1;
  }

  /** The last four characters of a pairing id — what both tabs display (§14). */
  function pairingSuffix(pairingId) {
    return typeof pairingId === "string" && pairingId.length > 4
      ? pairingId.slice(-4)
      : String(pairingId || "");
  }

  // --- the URL fragment ------------------------------------------------------

  /**
   * Take the pairing out of the fragment and put the URL back without it.
   *
   * Only ActionWitness's own key is removed: a theme's anchor (`#main-content`)
   * or another app's fragment parameter belongs to the store, and a bridge that
   * cleared the whole fragment would break navigation it has no business
   * touching.
   *
   * The stripping happens whether or not the value parses. A malformed
   * `#actionwitness=` is still a credential-shaped string in the address bar,
   * in the browser's history entry, and in whatever the next script reads.
   *
   * Accepted forms — both are one key whose value is `<pairing_id>.<credential>`:
   *
   *     #actionwitness=<pairing_id>.<credential>
   *     #aw=<pairing_id>.<credential>
   *
   * plus the explicit pair form `#actionwitness_pairing=<id>&actionwitness_credential=<c>`,
   * because the launch URL is composed by the server and a bridge that only
   * understood one spelling would fail closed on a cosmetic difference.
   */
  function readAndStripPairing(win) {
    var location = win.location;
    var raw = String(location.hash || "");
    if (raw === "" || raw === "#") {
      return null;
    }

    var params = new URLSearchParams(raw.charAt(0) === "#" ? raw.slice(1) : raw);
    var combined = params.get("actionwitness") || params.get("aw");
    var pairingId = params.get("actionwitness_pairing");
    var credential = params.get("actionwitness_credential");

    if (combined === null && pairingId === null && credential === null) {
      return null; // Not ours. Leave the store's own fragment alone.
    }

    params.delete("actionwitness");
    params.delete("aw");
    params.delete("actionwitness_pairing");
    params.delete("actionwitness_credential");

    // Rebuilt from the current URL rather than from a remembered one: another
    // script may already have pushed a state, and replacing the wrong entry
    // would restore the fragment we are removing.
    var rest = params.toString();
    var cleaned = location.pathname + location.search + (rest === "" ? "" : "#" + rest);
    try {
      win.history.replaceState(win.history.state, "", cleaned);
    } catch (error) {
      // A sandboxed or opaque-origin document refuses `replaceState`. Falling
      // back still removes the value from the visible URL; it costs a history
      // entry, which is a far smaller problem than leaving the credential up.
      location.hash = rest;
    }

    if (combined !== null) {
      var split = combined.indexOf(".");
      if (split <= 0 || split === combined.length - 1) {
        return null; // Stripped above; unusable here. Fail closed.
      }
      return { pairingId: combined.slice(0, split), credential: combined.slice(split + 1) };
    }
    if (pairingId && credential) {
      return { pairingId: pairingId, credential: credential };
    }
    return null;
  }

  // --- origins ---------------------------------------------------------------

  /**
   * An exact HTTPS origin, or a thrown refusal.
   *
   * "Exact" is doing real work: `https://shop.example/` and
   * `https://shop.example/en` both *look* configured and neither is an origin,
   * and accepting them would mean the comparisons below were comparing
   * something other than what the operator wrote down.
   */
  function requireHttpsOrigin(value, what) {
    var parsed;
    try {
      parsed = new URL(String(value));
    } catch (error) {
      throw new Error(what + " is not a URL: " + String(value));
    }
    if (parsed.protocol !== "https:") {
      throw new Error(what + " must be HTTPS: " + String(value));
    }
    if (parsed.origin !== String(value).replace(/\/+$/, "")) {
      throw new Error(what + " must be an exact origin with no path: " + String(value));
    }
    return parsed.origin;
  }

  /**
   * The locale-aware cart URL for this session (FR-112).
   *
   * `Shopify.routes.root` is `/` on a single-locale store and `/en-gb/` — or
   * `/fr/`, or `/en-ca/` — on a localised one. A hard-coded `/cart.js` returns
   * the wrong locale's cart or a redirect on those storefronts, which is the
   * quiet way this observation stops being about the shopper's session.
   *
   * The result is required to sit on the configured origin. It cannot be
   * anything else on a normal storefront, which is the point: if `routes.root`
   * has been set to an absolute URL by a theme or an app, that is exactly the
   * case that must fail closed rather than fetch a stranger's cart.
   */
  function resolveCartUrl(win, storeOrigin) {
    var shopify = win.Shopify;
    var root =
      shopify && shopify.routes && typeof shopify.routes.root === "string"
        ? shopify.routes.root
        : "/";
    if (root.charAt(root.length - 1) !== "/") {
      root = root + "/";
    }
    var url = new URL(root + "cart.js", win.location.href);
    if (url.origin !== storeOrigin) {
      throw new Error("the cart URL left the configured store origin: " + url.origin);
    }
    return url.toString();
  }

  // --- the cart observation --------------------------------------------------

  function byteLength(text) {
    if (typeof TextEncoder === "function") {
      return new TextEncoder().encode(text).length;
    }
    // A browser without TextEncoder cannot be one with WebMCP, but a cap that
    // silently stopped applying would be worse than a rough one.
    return text.length;
  }

  /**
   * One bounded, same-session `/cart.js` read (FR-112).
   *
   * Four refusals, each of which is a way the observation stops being
   * independent evidence rather than a mere transport problem:
   *
   * - a **redirect**, which is how a cart read becomes a read of something else
   *   (`redirect: "error"` refuses before a body is ever seen, and the final
   *   URL is re-checked in case a future browser follows one anyway);
   * - a **final URL off the configured origin**;
   * - a **non-JSON body**, which on a storefront means an HTML error or consent
   *   page, and parsing one would record a cart of `{}`;
   * - a payload **over 256 KiB**, checked against the declared length first and
   *   then against the bytes actually read, because a declared length is a
   *   claim.
   */
  async function readCartDocument(env, storeOrigin) {
    var url = resolveCartUrl(env.window, storeOrigin);
    var response = await env.fetch(url, {
      method: "GET",
      // The shopper's own session is the whole point: this is *their* cart,
      // read with their storefront cookies, and it never leaves the origin.
      credentials: "same-origin",
      redirect: "error",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });

    if (response.redirected === true) {
      throw new Error("the cart read was redirected; refusing the observation");
    }
    if (typeof response.url === "string" && response.url !== "") {
      var finalOrigin = new URL(response.url).origin;
      if (finalOrigin !== storeOrigin) {
        throw new Error("the cart read ended on " + finalOrigin + ", not the configured origin");
      }
    }
    if (!response.ok) {
      throw new Error("the cart could not be read (" + String(response.status) + ")");
    }

    var contentType = String(response.headers.get("content-type") || "")
      .split(";")[0]
      .trim()
      .toLowerCase();
    if (contentType !== "application/json" && contentType !== "text/javascript") {
      throw new Error("the cart response was not JSON (" + (contentType || "no content type") + ")");
    }

    var declared = Number(response.headers.get("content-length"));
    if (Number.isFinite(declared) && declared > MAX_CART_BYTES) {
      throw new Error("the cart response declares more than 256 KiB");
    }

    var text = await response.text();
    if (byteLength(text) > MAX_CART_BYTES) {
      throw new Error("the cart response is larger than 256 KiB");
    }

    var parsed;
    try {
      parsed = JSON.parse(text);
    } catch (_error) {
      throw new Error("the cart response body was not valid JSON");
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("the cart response was not a JSON object");
    }
    // FR-117: the capture URL is recorded without query or fragment. A cart URL
    // carries neither today; recording the path rather than the href is what
    // keeps that true if one ever does.
    return { cart: parsed, capturePath: new URL(url).pathname };
  }

  /**
   * Add the one page fact Python requires before treating a cart as evidence.
   *
   * The bridge is installed on the storefront document and is disposed on
   * pagehide; if checkout navigation occurs, this document cannot stay alive
   * to submit a successful verification. While this frame is still able to
   * read the same-session cart, the truthful page witness is therefore false.
   * It is nested under cart because the strict HTTP envelope accepts only
   * capture_path and the complete observation document.
   */
  function cartObservation(cart) {
    return Object.assign({}, cart, {
      page: { checkout_navigation_observed: false },
    });
  }

  // --- the harness -----------------------------------------------------------

  /**
   * One POST to the configured harness origin, authorized by a bearer
   * credential that exists only in this closure.
   *
   * `credentials: "omit"` on purpose: the pairing credential is the entire
   * authorization (§15.7 — the bridge never receives the workspace cookie), and
   * sending ambient credentials cross-origin would invite the server to trust
   * something the pairing model says it must not.
   */
  async function postToHarness(env, harnessOrigin, path, credential, body) {
    var response = await env.fetch(harnessOrigin + path, {
      method: "POST",
      credentials: "omit",
      redirect: "error",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        Authorization: "Bearer " + credential,
      },
      body: JSON.stringify(body),
    });
    var text = await response.text();
    var payload = null;
    if (text !== "") {
      try {
        payload = JSON.parse(text);
      } catch (error) {
        payload = null;
      }
    }
    if (!response.ok) {
      var envelope = payload && payload.error ? payload.error : null;
      var error = new Error(
        (envelope && envelope.message) || "The harness refused (" + String(response.status) + ").",
      );
      error.code = (envelope && envelope.code) || "";
      error.status = response.status;
      throw error;
    }
    return payload === null || typeof payload !== "object" ? {} : payload;
  }

  /**
   * A harness response's own record, whether or not it arrived in an envelope.
   *
   * `{ "pairing": { … } }` and a bare record are both in use across this API,
   * and the pairing routes are being written alongside this file. Accepting
   * either is not laxity about the contract: every field is still read by name
   * and a missing one still fails closed. It is refusing to let a cosmetic
   * envelope decision strand an operator's storefront tab.
   */
  function record(payload) {
    return payload && typeof payload.pairing === "object" && payload.pairing !== null
      ? payload.pairing
      : payload;
  }

  /** The first of `keys` present as a non-empty string, or `null`. */
  function firstString(payload, keys) {
    for (var index = 0; index < keys.length; index += 1) {
      var value = payload[keys[index]];
      if (typeof value === "string" && value !== "") {
        return value;
      }
    }
    return null;
  }

  // --- the visible panel -----------------------------------------------------

  /**
   * The storefront tab's half of §14's two-tab agreement.
   *
   * Text, always: the pairing suffix, the expiry, the current actor, the next
   * action, and — in every state where something has gone wrong or ended — the
   * recovery instruction. Nothing here is carried by colour alone (§8.4), and
   * nothing is set with `innerHTML`: the values come from a server response and
   * are rendered as text, never as markup.
   *
   * Styles are applied through CSSOM rather than a stylesheet because this
   * element lives in somebody else's theme and must not depend on, or add to,
   * their CSS.
   */
  function createPanel(doc) {
    var host = doc.createElement("aside");
    host.setAttribute("data-actionwitness-panel", "");
    host.setAttribute("role", "status");
    host.setAttribute("aria-live", "polite");
    host.setAttribute("aria-label", "ActionWitness pairing");
    var style = host.style;
    style.position = "fixed";
    style.right = "12px";
    style.bottom = "12px";
    style.zIndex = "2147483000";
    style.maxWidth = "320px";
    style.padding = "10px 12px";
    style.border = "1px solid #444";
    style.borderRadius = "8px";
    style.background = "#fff";
    style.color = "#111";
    style.font = "13px/1.45 system-ui, sans-serif";
    style.boxShadow = "0 2px 8px rgba(0,0,0,0.25)";

    var title = doc.createElement("strong");
    title.textContent = "ActionWitness";
    var state = doc.createElement("div");
    var identity = doc.createElement("div");
    var next = doc.createElement("div");
    var recovery = doc.createElement("div");
    recovery.style.marginTop = "6px";
    recovery.style.color = "#5a3a00";

    host.appendChild(title);
    host.appendChild(state);
    host.appendChild(identity);
    host.appendChild(next);
    host.appendChild(recovery);

    return {
      element: host,
      render: function (view) {
        state.textContent = "State: " + view.state + " · acting: " + view.actor;
        identity.textContent =
          "Pairing …" + view.pairingSuffix + (view.expiresAt ? " · expires " + view.expiresAt : "");
        next.textContent = view.next;
        recovery.textContent = view.recovery;
      },
    };
  }

  // --- the bridge ------------------------------------------------------------

  /**
   * @param {object} options
   *   `window`, `fetch`, `now`, `resolveModelContext`, `harnessOrigin`,
   *   `storeOrigin`, and an optional `onChange` for tests. Every capability is
   *   injected so the whole lifecycle is exercisable without a storefront.
   */
  function createBridge(options) {
    var env = {
      window: options.window,
      fetch: options.fetch,
      now: options.now || function () { return new Date(); },
    };
    var doc = env.window.document;
    var harnessOrigin = requireHttpsOrigin(options.harnessOrigin, "the harness origin");
    var storeOrigin = requireHttpsOrigin(options.storeOrigin, "the store origin");
    var resolveModelContext =
      options.resolveModelContext ||
      function () {
        if (doc && doc.modelContext) {
          return doc.modelContext;
        }
        var nav = env.window.navigator;
        return nav && nav.modelContext ? nav.modelContext : null;
      };

    /**
     * The redeemed session credential. A closure variable and nothing else —
     * not a property of the returned object, not on `window`, not in storage.
     * When this frame goes away the credential goes with it, which is what makes
     * "a reload ends the pairing" a fact about the design rather than a promise.
     */
    var sessionCredential = null;

    var state = "idle";
    var pairingId = "";
    var expiresAt = null;
    var runId = null;
    var detail = "";
    var expiryTimer = null;
    var registration = null; // The AbortController whose abort is the unregistration.
    var panel = null;
    var disposed = false;

    function view() {
      var guidance = PAIRING_GUIDANCE[state] || {
        actor: "operator",
        next: "Waiting for a pairing.",
        recovery: "Create a pairing in the harness and open its launch URL here.",
      };
      return {
        state: state,
        actor: guidance.actor,
        next: detail === "" ? guidance.next : guidance.next + " (" + detail + ")",
        recovery: guidance.recovery,
        pairingSuffix: pairingSuffix(pairingId),
        expiresAt: expiresAt,
        runId: runId,
      };
    }

    function publish() {
      if (panel !== null) {
        panel.render(view());
      }
      if (typeof options.onChange === "function") {
        options.onChange(view());
      }
    }

    function moveTo(next, why) {
      state = next;
      detail = why || "";
      if (isTerminal(state)) {
        // §16.5 and FR-115: the tool exists only while the pairing is valid.
        // Unregistering here rather than at each call site is what makes
        // "on every terminal state" true of the state machine instead of true
        // of the paths somebody remembered.
        unregisterTool();
        sessionCredential = null;
        clearExpiryTimer();
      }
      publish();
    }

    function clearExpiryTimer() {
      if (expiryTimer !== null) {
        env.window.clearTimeout(expiryTimer);
        expiryTimer = null;
      }
    }

    /**
     * Expire locally, without being told.
     *
     * The server is authoritative about expiry and will refuse a late
     * submission — but a bridge that only learned this by trying would keep
     * offering `verify_shopify_outcome` to an agent for as long as the tab was
     * open, and an agent reading the tool surface would have no way to know the
     * pairing behind it was dead.
     */
    function armExpiry() {
      clearExpiryTimer();
      if (expiresAt === null) {
        return;
      }
      var remaining = Date.parse(expiresAt) - env.now().getTime();
      if (!Number.isFinite(remaining)) {
        return;
      }
      expiryTimer = env.window.setTimeout(
        function () {
          if (!isTerminal(state)) {
            moveTo("expired", "");
          }
        },
        Math.max(0, remaining),
      );
    }

    function unregisterTool() {
      if (registration !== null) {
        registration.abort();
        registration = null;
      }
    }

    function registerTool() {
      var modelContext = resolveModelContext();
      if (!modelContext || typeof modelContext.registerTool !== "function") {
        // A browser with no WebMCP is not a broken bridge: the operator can
        // still drive the store by hand, and the panel says who acts next.
        return Promise.resolve(false);
      }
      unregisterTool();
      var controller = new AbortController();
      registration = controller;
      return Promise.resolve(
        modelContext.registerTool(
          {
            name: TOOL_NAME,
            description: TOOL_DESCRIPTION,
            // Appendix D.3 exactly: no properties. The pairing is browser and
            // session state, and putting it in the schema would hand the
            // credential to whatever reads the tool surface.
            inputSchema: { type: "object", properties: {}, additionalProperties: false },
            annotations: { readOnlyHint: false },
            execute: function () {
              return verify();
            },
          },
          { signal: controller.signal },
        ),
      ).then(
        function () {
          return true;
        },
        function () {
          registration = null;
          return false;
        },
      );
    }

    /** Redeem once, capture the starting cart, and arm — §16.5's happy path. */
    async function start(pairing) {
      if (disposed) {
        return view();
      }
      pairingId = pairing.pairingId;
      moveTo("created", "");

      try {
        var redeemed = await postToHarness(
          env,
          harnessOrigin,
          "/api/v1/shopify/pairings/" + encodeURIComponent(pairingId) + "/redeem",
          pairing.credential,
          { bridge_version: BRIDGE_VERSION },
        );
        // The one-time credential is spent the moment it is redeemed. Dropping
        // the caller's copy here means the only surviving credential is the
        // session one, in the closure above.
        pairing.credential = "";

        var redeemedRecord = record(redeemed);
        // Looked for at the top level *and* inside the envelope. The credential
        // has no business in a pairing view — a view that could carry one is a
        // refactor away from the status endpoint returning one — so top level is
        // where it should be, and checking both costs nothing if it moves.
        var credentialKeys = ["session_credential", "bridge_session_credential"];
        sessionCredential =
          firstString(redeemed, credentialKeys) || firstString(redeemedRecord, credentialKeys);
        if (sessionCredential === null) {
          throw new Error("the harness returned no session credential");
        }
        expiresAt = firstString(redeemedRecord, ["expires_at"]);
        moveTo("paired", "");
        armExpiry();

        var observed = await readCartDocument(env, storeOrigin);
        var armedResponse = await postToHarness(
          env,
          harnessOrigin,
          "/api/v1/shopify/pairings/" + encodeURIComponent(pairingId) + "/observations/before",
          sessionCredential,
          {
            capture_path: observed.capturePath,
            cart: cartObservation(observed.cart),
          },
        );
        var armedRecord = record(armedResponse);
        runId = firstString(armedRecord, ["run_id"]);
        moveTo(firstString(armedRecord, ["status"]) || "armed", "");
        if (state === "armed") {
          await registerTool();
          publish();
        }
      } catch (error) {
        moveTo("error", messageOf(error));
      }
      return view();
    }

    /**
     * Appendix D.3's handler: capture the final cart, submit it, return a
     * compact verdict.
     *
     * The result is deliberately thin — a status and a run id. §11.4 caps a tool
     * result at 1,500 characters and forbids returning evidence through it; the
     * cart, the findings, and the report stay server-side where the harness page
     * reads them.
     */
    async function verify() {
      if (state !== "armed" || sessionCredential === null) {
        return toolResult("This pairing is not armed, so there is nothing to verify.", true);
      }
      try {
        var observed = await readCartDocument(env, storeOrigin);
        var verdict = await postToHarness(
          env,
          harnessOrigin,
          "/api/v1/shopify/pairings/" + encodeURIComponent(pairingId) + "/verify",
          sessionCredential,
          {
            capture_path: observed.capturePath,
            // Recorded rather than asserted: this bridge never navigates, so
            // the honest value is what this frame observed, and the server
            // decides what it means.
            cart: cartObservation(observed.cart),
          },
        );
        var verdictRecord = record(verdict);
        runId = firstString(verdictRecord, ["run_id"]) || runId;
        var settled = firstString(verdictRecord, ["verdict"]);
        if (settled !== "passed" && settled !== "failed" && settled !== "error") {
          throw new Error("The harness returned an invalid verification verdict.");
        }
        moveTo(settled, "");
        return toolResult(
          "Verified. Result: " + settled + (runId === null ? "" : ". Run " + runId) + ".",
          settled === "failed" || settled === "error",
        );
      } catch (error) {
        // A verification that could not complete is not a failed contract, and
        // saying "failed" here would record a verdict nobody established. The
        // pairing stops safely instead (§16.5's `error`).
        moveTo("error", messageOf(error));
        return toolResult("Verification could not complete: " + messageOf(error), true);
      }
    }

    function attachPanel() {
      if (panel !== null || !doc || !doc.body) {
        return;
      }
      panel = createPanel(doc);
      doc.body.appendChild(panel.element);
      publish();
    }

    function dispose() {
      disposed = true;
      // §16.5's teardown clause. A tab that is going away must not leave a tool
      // registered against a pairing nothing is driving.
      unregisterTool();
      clearExpiryTimer();
      sessionCredential = null;
      if (panel !== null && panel.element.parentNode) {
        panel.element.parentNode.removeChild(panel.element);
      }
      panel = null;
    }

    return {
      start: start,
      verify: verify,
      attachPanel: attachPanel,
      dispose: dispose,
      view: view,
      /** Present for tests and for the teardown listener; never a credential. */
      isToolRegistered: function () {
        return registration !== null;
      },
    };
  }

  function messageOf(error) {
    return error && error.message ? String(error.message) : String(error);
  }

  function toolResult(text, isError) {
    var result = { content: [{ type: "text", text: text.slice(0, 1500) }] };
    if (isError) {
      result.isError = true;
    }
    return result;
  }

  // --- auto-start ------------------------------------------------------------

  /**
   * Start only when a real theme `<script data-actionwitness …>` loaded this
   * file.
   *
   * `document.currentScript` is the element being executed for a classic script
   * and `null` for a module import — so a test that imports this file to
   * exercise `readAndStripPairing` never spins up a live bridge, and the theme
   * never has to call anything.
   *
   * The origins come from the script element rather than from the URL. That is
   * the difference between "the operator installed this pointing at their
   * harness" and "whoever composed the link chose where the observations go".
   */
  function autoStart() {
    var doc = globalScope.document;
    var script = doc ? doc.currentScript : null;
    if (!script || !script.hasAttribute("data-actionwitness")) {
      return null;
    }
    var bridge;
    try {
      bridge = createBridge({
        window: globalScope,
        fetch: globalScope.fetch.bind(globalScope),
        harnessOrigin: script.getAttribute("data-harness-origin"),
        storeOrigin: script.getAttribute("data-store-origin"),
      });
    } catch (error) {
      // A misconfigured install must say so where the operator will see it,
      // rather than looking like a store with no bridge on it.
      if (globalScope.console && globalScope.console.error) {
        globalScope.console.error("[ActionWitness] " + messageOf(error));
      }
      return null;
    }

    // Synchronous, before any await and before any other theme script runs.
    var pairing = readAndStripPairing(globalScope);

    var attach = function () {
      bridge.attachPanel();
    };
    if (doc.readyState === "loading") {
      doc.addEventListener("DOMContentLoaded", attach, { once: true });
    } else {
      attach();
    }
    globalScope.addEventListener("pagehide", function () {
      bridge.dispose();
    });

    if (pairing !== null) {
      void bridge.start(pairing);
    }
    return bridge;
  }

  globalScope.ActionWitnessBridge = {
    BRIDGE_VERSION: BRIDGE_VERSION,
    MAX_CART_BYTES: MAX_CART_BYTES,
    TOOL_NAME: TOOL_NAME,
    PAIRING_GUIDANCE: PAIRING_GUIDANCE,
    createBridge: createBridge,
    readAndStripPairing: readAndStripPairing,
    resolveCartUrl: resolveCartUrl,
    readCartDocument: readCartDocument,
    requireHttpsOrigin: requireHttpsOrigin,
    pairingSuffix: pairingSuffix,
    isTerminal: isTerminal,
  };

  globalScope.ActionWitnessBridge.instance = autoStart();
})(typeof window !== "undefined" ? window : globalThis);
