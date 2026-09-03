# ActionWitness Shopify theme bridge

A small, dependency-free script an operator installs on **one authorized Shopify
development store**. It lets ActionWitness observe that store's shopper-session
cart independently of whatever a tool reported — which is the whole point: a
tool's self-report is evidence, never proof.

Spec references: §11.3 and Appendix D.3 (the tool), §12.12 / FR-110–FR-119 (the
target), §15.7 (the endpoints), §16.5 (the pairing state machine), AC-18.

## What it does — and what it refuses to do

It reads the cart. That is all.

| It does | It never does |
|---|---|
| Reads the one-time credential from the URL **fragment** and removes it from the visible URL immediately | Adds, changes, or clears a cart line |
| Redeems that credential **once** for a session credential held in memory | Navigates to checkout or creates an order |
| Reads `window.Shopify.routes.root + 'cart.js'` in the shopper's own session | Logs a customer in, or touches payment |
| Posts bounded before/after observations to the configured harness origin | Store anything: no `localStorage`, no `sessionStorage`, no cookie, no cart token |
| Registers `verify_shopify_outcome` while the pairing is valid, and unregisters on every terminal state | Use a Shopify Admin API credential — it needs none |

Shopify's own WebMCP tools perform the cart actions. This bridge only witnesses
them. `tests/architecture/test_shopify_bridge_artifact.py` fails the build if a
mutation, a navigation, or a persistence call ever appears in the script,
because "we did not mean to" is not a property a store can rely on.

## Install on a development store

**Use a development store you own or are authorized on. Never a live store.**

1. **Get the harness origin.** The exact HTTPS origin your ActionWitness
   deployment is served from — `https://actionwitness.example`, no trailing
   slash, no path.

2. **Upload the script.** In the Shopify admin: *Online Store → Themes → … →
   Edit code → Assets → Add a new asset → Upload*, and upload
   `actionwitness-bridge.js` from this directory. It is checked in unminified
   and unbundled so you can read every line before you do.

3. **Add the snippet.** *Snippets → Add a new snippet*, name it
   `actionwitness-bridge`, and paste the contents of
   `snippets/actionwitness-bridge.liquid`. Replace
   `https://REPLACE-WITH-YOUR-HARNESS.example` with the origin from step 1.

   If your development store is browsed on a custom domain rather than its
   `*.myshopify.com` domain, also replace `data-store-origin` with the origin
   you actually browse it on. The bridge refuses to read a cart that resolves
   off that origin, and the server independently refuses an observation from any
   origin but the one it was configured with (FR-110).

4. **Render it first.** In `layout/theme.liquid`, immediately after `<head>`:

   ```liquid
   <head>
     {% render 'actionwitness-bridge' %}
     ...
   ```

   **Position is load-bearing.** FR-111 requires the credential to be gone from
   the URL before unrelated third-party theme scripts run. A blocking classic
   script at the top of `<head>` is the only placement that guarantees it;
   `defer`, `async`, and `type="module"` all move it *after* the chat widget and
   the analytics tag.

5. **Pair.** In the ActionWitness workspace, open the Shopify panel, select the
   Shopify contract, and press *Create pairing*. Open the launch URL it gives
   you in a **fresh storefront tab with an empty cart** (FR-116). A small status
   card appears at the bottom right of the storefront saying which pairing it is
   on, when it expires, who acts next, and what to do if something goes wrong.

## Remove it

1. Delete the `{% render 'actionwitness-bridge' %}` line from
   `layout/theme.liquid`.
2. Delete the `actionwitness-bridge` snippet.
3. Delete the `actionwitness-bridge.js` asset.

There is nothing else to clean up. The bridge writes no cookie, no browser
storage, and no theme setting, so removing the three references above removes
the bridge completely. Any pairing still open expires on its own within 15
minutes and records no verdict — expiry never converts an incomplete trial into
a pass.

## The wire contract

The bridge speaks only to the configured harness origin, over HTTPS, with a
bearer credential. It never receives the harness's workspace cookie: the pairing
*is* the identity (§15.7).

| Step | Request | Credential |
|---|---|---|
| Redeem | `POST {harness}/api/v1/shopify/pairings/{id}/redeem` | the one-time credential from the fragment |
| Starting cart | `POST …/pairings/{id}/observations/before` | the session credential from redeem |
| Verify | `POST …/pairings/{id}/verify` | the session credential |

The launch URL's fragment carries the pairing and the one-time credential:

```
https://your-dev-store.myshopify.com/#actionwitness=<pairing_id>.<one_time_credential>
```

The explicit pair form `#actionwitness_pairing=<id>&actionwitness_credential=<c>`
is accepted as well, so a cosmetic difference in how the server composes the
link cannot fail the pairing closed.

The fragment is parsed as `application/x-www-form-urlencoded`, so the server
must emit a **URL-safe** credential — base64url, or percent-encoded. A raw `+`
in a standard-base64 credential decodes to a space and the redemption fails
closed, which is a confusing way to learn about an encoding choice.

Request bodies carry `store_origin`, `bridge_version`, `capture_url_path` (path
only — no query, no fragment, per FR-117), `captured_at`, and the raw `/cart.js`
document under `cart`. `verify` adds `checkout_navigation_observed`, which this
bridge can only ever report as `false` because it never navigates.

## Safety notes worth reading before you install

- **The credential lives in one closure.** Not on `window`, not in storage, not
  in the DOM, not in a log. A reload therefore ends the pairing — the bridge
  says exactly that, with what to do next, rather than sitting there looking
  idle.
- **Redirects are refused, not followed.** A cart read that redirects, or whose
  final URL is off the configured origin, is refused before its body is read. A
  redirected cart read is a read of something that is not this cart.
- **Non-JSON is refused.** On a storefront, an HTML response to `/cart.js` means
  an error page or a consent interstitial. Parsing one would record an empty
  cart as an observation.
- **256 KiB, checked twice.** Once against the declared `Content-Length` and
  once against the bytes actually read, because a declared length is a claim.
- **The tool takes no arguments.** Appendix D.3's schema is empty and
  `additionalProperties: false`. The pairing is session state; putting it in the
  schema would hand the credential to anything that reads the tool surface.

## Testing it

The bridge's logic — fragment stripping, origin refusal, the size cap, tool
registration and unregistration — is covered by
`apps/actionwitness_service/frontend/src/shopifyBridge.test.ts`, which runs in
the harness frontend's `npm run test`. That is the repository's only JavaScript
test runner; `actionwitness-bridge.d.ts` is what lets a `strict` TypeScript test
import this unbuilt file without an `any`.

`tests/architecture/test_shopify_bridge_artifact.py` holds the properties that
must be true of the *artifact* rather than of a code path: no storage, no
checkout, no navigation, and no place in the release image.
