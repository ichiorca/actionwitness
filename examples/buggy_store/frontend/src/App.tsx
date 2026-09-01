/**
 * The ordinary human storefront (spec v1.9 §15.5, §20.4; BUILD_ORDER §7/M2).
 *
 * This is the shop a person uses. It has no agent, no WebMCP, and no harness:
 * §26.7 and AC-19 require the Buggy Store to run with every assurance package
 * absent, and AC-09's progressive-enhancement claim rests on there being a real
 * human path that never needed the browser-tool surface in the first place.
 *
 * Two things it must do that an ordinary shop would not:
 *
 * **Show canonical state, not a local mirror.** AC-03 compares "the same
 * canonical state in human UI and observation", so every number rendered here
 * is read back from the server after each mutation rather than predicted
 * locally. When the `discount_reported_but_not_applied` profile is active, this
 * page shows the *unchanged* total while the tool that just ran reported
 * success — which is the contradiction the harness detects, visible to a person.
 *
 * **Label an injected fault.** §20.4: "the UI clearly labels unsafe injected
 * modes." A demo defect that looked like a real one would teach the wrong
 * lesson to whoever is watching.
 */

import { useCallback, useEffect, useState } from "react";

import {
  type Confirmation,
  type Product,
  type Scenario,
  type StoreState,
  StoreApiError,
  StoreClient,
  requestId,
} from "./api";

export interface AppProps {
  readonly client: StoreClient;
}

interface Notice {
  readonly tone: "info" | "problem";
  readonly text: string;
}

export function App({ client }: AppProps): JSX.Element {
  const [catalog, setCatalog] = useState<readonly Product[]>([]);
  const [state, setState] = useState<StoreState | null>(null);
  const [scenario, setScenario] = useState<Scenario | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);

  /** Re-read authoritative state. Never derived from the last response body. */
  const refresh = useCallback(async () => {
    setState(await client.readCart());
    setScenario(await client.scenario());
  }, [client]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const products = await client.catalog();
        const current = await client.readCart();
        const mode = await client.scenario();
        // StrictMode mounts effects twice and a slow response can land after
        // unmount; applying it then would set state on a gone component and,
        // worse, show a cart from a session the shopper has left.
        if (cancelled) return;
        setCatalog(products);
        setState(current);
        setScenario(mode);
      } catch (error) {
        if (!cancelled) setNotice(toNotice(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  const run = useCallback(
    async (action: () => Promise<void>, success: string) => {
      setBusy(true);
      setNotice(null);
      try {
        await action();
        await refresh();
        setNotice({ tone: "info", text: success });
      } catch (error) {
        setNotice(toNotice(error));
        // Re-read even after a refusal: the store may have refused *because*
        // state moved, and showing the stale cart would hide why.
        try {
          await refresh();
        } catch {
          /* the notice above already says the store is unreachable */
        }
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  return (
    <main>
      <h1>Buggy Store</h1>
      <p>
        A deterministic demo storefront. It runs on its own, with no assurance
        harness and no browser-agent support.
      </p>

      {scenario ? <ScenarioBanner scenario={scenario} /> : null}
      {notice ? (
        <p role={notice.tone === "problem" ? "alert" : "status"}>{notice.text}</p>
      ) : null}

      <section aria-labelledby="catalog-heading">
        <h2 id="catalog-heading">Catalog</h2>
        <ul>
          {catalog.map((product) => (
            <li key={product.product_id}>
              <span>
                {product.name} — {product.price}
              </span>{" "}
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  void run(
                    () =>
                      client.updateCart(
                        product.product_id,
                        quantityOf(state, product.line_key) + 1,
                        requestId("add"),
                      ),
                    `Added one ${product.name}.`,
                  )
                }
              >
                Add {product.name}
              </button>
            </li>
          ))}
        </ul>
      </section>

      <CartView
        state={state}
        busy={busy}
        onRemove={(lineKey) => {
          const line = state?.cart.items[lineKey];
          if (!line) return;
          void run(
            () => client.updateCart(line.product_id, 0, requestId("remove")),
            "Removed the line.",
          );
        }}
        onDiscount={() =>
          void run(() => client.applyDiscount("SAVE20"), "Applied SAVE20.")
        }
      />

      <CheckoutView
        state={state}
        confirmation={confirmation}
        busy={busy}
        onRequest={() =>
          void run(async () => {
            setConfirmation(await client.openConfirmation());
          }, "Review the order and choose.")
        }
        onDecide={(approved) => {
          const pending = confirmation;
          if (!pending) return;
          void run(async () => {
            await client.decide(pending.confirmation_id, approved);
            if (approved) {
              await client.checkout(pending.confirmation_id, requestId("checkout"));
            }
            setConfirmation(null);
          }, approved ? "Order placed." : "Nothing was ordered.");
        }}
        onCancel={() => {
          const pending = confirmation;
          if (!pending) return;
          void run(async () => {
            await client.cancelConfirmation(pending.confirmation_id);
            setConfirmation(null);
          }, "Cancelled. Nothing was ordered.");
        }}
      />
    </main>
  );
}

function ScenarioBanner({ scenario }: { readonly scenario: Scenario }): JSX.Element {
  // §20.4: "the UI clearly labels unsafe injected modes." The label carries the
  // word "injected" so a viewer cannot mistake a seeded defect for a real one,
  // and it is a live region so it is announced rather than only shown.
  if (!scenario.fault_active) {
    return (
      <p role="status" data-testid="scenario-banner">
        Running the correct implementation ({scenario.scenario_mode}).
        {scenario.fault_profile !== "none"
          ? ` Comparison fault ${scenario.fault_profile} is recorded but disabled.`
          : null}
      </p>
    );
  }
  return (
    <p role="alert" data-testid="scenario-banner">
      <strong>Injected unsafe demo behaviour is active.</strong> Profile{" "}
      <code>{scenario.fault_profile}</code> in <code>{scenario.scenario_mode}</code>:{" "}
      {scenario.description} This is a deliberate defect in this demo store, not a
      real fault.
    </p>
  );
}

function CartView({
  state,
  busy,
  onRemove,
  onDiscount,
}: {
  readonly state: StoreState | null;
  readonly busy: boolean;
  readonly onRemove: (lineKey: string) => void;
  readonly onDiscount: () => void;
}): JSX.Element {
  const lines = state ? Object.entries(state.cart.items) : [];
  return (
    <section aria-labelledby="cart-heading">
      <h2 id="cart-heading">Cart</h2>
      {state === null ? (
        <p>Loading the cart…</p>
      ) : (
        <>
          {lines.length === 0 ? (
            <p data-testid="empty-cart">Your cart is empty.</p>
          ) : (
            <ul>
              {lines.map(([lineKey, line]) => (
                <li key={lineKey} data-testid={`line-${lineKey}`}>
                  {lineKey} × {line.quantity} @ {line.unit_price}{" "}
                  <button type="button" disabled={busy} onClick={() => onRemove(lineKey)}>
                    Remove {lineKey}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <dl>
            <dt>Subtotal</dt>
            <dd data-testid="subtotal">{state.cart.subtotal}</dd>
            <dt>Discount</dt>
            <dd data-testid="discount">
              {state.cart.discount
                ? `${state.cart.discount.code} −${state.cart.discount.amount}`
                : "None"}
            </dd>
            <dt>Total</dt>
            <dd data-testid="total">{state.cart.total}</dd>
            {/* Shown because AC-03 compares this view with the adapter's
                observation, and a version is how a reader tells two readings
                of the same cart apart. */}
            <dt>State version</dt>
            <dd data-testid="state-version">{state.state_version}</dd>
          </dl>
          <button type="button" disabled={busy} onClick={onDiscount}>
            Apply SAVE20
          </button>
        </>
      )}
    </section>
  );
}

function CheckoutView({
  state,
  confirmation,
  busy,
  onRequest,
  onDecide,
  onCancel,
}: {
  readonly state: StoreState | null;
  readonly confirmation: Confirmation | null;
  readonly busy: boolean;
  readonly onRequest: () => void;
  readonly onDecide: (approved: boolean) => void;
  readonly onCancel: () => void;
}): JSX.Element {
  return (
    <section aria-labelledby="checkout-heading">
      <h2 id="checkout-heading">Checkout</h2>
      {state?.order.created ? (
        <p data-testid="order">Order {state.order.order_id} placed.</p>
      ) : confirmation === null ? (
        <button type="button" disabled={busy} onClick={onRequest}>
          Proceed to checkout
        </button>
      ) : (
        // §14 step 4: no option is preselected, and the agent-facing equivalent
        // of this dialog cannot choose either control. Here the person reads the
        // exact consequence first and then picks.
        <div role="group" aria-labelledby="confirm-heading" data-testid="confirmation">
          <h3 id="confirm-heading">Confirm this order</h3>
          <p>
            You are about to order {confirmation.consequence.item_count} line(s) totalling{" "}
            <strong data-testid="confirm-total">{confirmation.consequence.cart_total}</strong>.
            This approval is single-use and expires at {confirmation.expires_at}.
          </p>
          <button type="button" disabled={busy} onClick={() => onDecide(true)}>
            Approve once
          </button>
          <button type="button" disabled={busy} onClick={() => onDecide(false)}>
            Deny
          </button>
          <button type="button" disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        </div>
      )}
    </section>
  );
}

function quantityOf(state: StoreState | null, lineKey: string): number {
  return state?.cart.items[lineKey]?.quantity ?? 0;
}

function toNotice(error: unknown): Notice {
  if (error instanceof StoreApiError) {
    return { tone: "problem", text: `${error.message} (${error.code})` };
  }
  // Never render an unknown error's text: it may carry internals, which §15.8
  // keeps away from a browser.
  return { tone: "problem", text: "The store could not be reached." };
}
