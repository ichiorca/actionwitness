/**
 * Pairing a Shopify development store, from the harness side (§12.12, §14,
 * §15.7, §16.5, FR-111–FR-117, AC-18).
 *
 * Two tabs are involved in this journey and only one of them is this page. The
 * other is a storefront on somebody else's origin, running the checked-in theme
 * bridge. §14 requires both to agree about **the same pairing, the same expiry,
 * the same current actor, and the same next action** — so this panel and the
 * bridge's status card render the same four facts from the same table, and
 * every state carries a bounded recovery instruction rather than a control that
 * has quietly gone grey.
 *
 * ## The launch URL is a credential, and is treated as one
 *
 * §15.7 puts the one-time pairing credential in the launch URL's **fragment**.
 * That is the safest place for it — a fragment never reaches a server and never
 * lands in an access log — but it means the launch URL *is* the secret. So it is
 * never rendered: the panel shows the origin with the fragment redacted, and the
 * URL itself lives in a ref, is handed to `window.open`, and is dropped when the
 * pairing leaves `created`. A read-only input with the URL in its `value` would
 * look like a convenience and would be a credential sitting in the DOM for any
 * screenshot, extension, or error reporter to collect.
 *
 * ## What this panel does not decide
 *
 * Nothing. The pairing's state, its expiry, whether the initial cart was empty
 * enough to arm, and the verdict are all the server's (FR-120). The guidance
 * table below is a *lookup on the server's own `status` token* — the rendering
 * of a state the server chose, in the same words the other tab uses. When the
 * server sends its own `active_actor` and `next_action`, those win.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/client";
import {
  type ShopifyPairing,
  createShopifyPairing,
  readShopifyPairing,
} from "../api/shopify";

/** §16.5's states that never move again. */
const TERMINAL_PAIRING_STATES: readonly string[] = [
  "passed",
  "passed_with_warnings",
  "failed",
  "expired",
  "cancelled",
  "error",
];

export function isTerminalPairing(status: string): boolean {
  return TERMINAL_PAIRING_STATES.includes(status);
}

export interface PairingGuidance {
  readonly actor: string;
  readonly next: string;
  readonly recovery: string;
}

/**
 * §16.5's states, each with who acts, what happens next, and the way out.
 *
 * **This table is duplicated in `shopify_bridge/actionwitness-bridge.js` on
 * purpose.** The two tabs live at two origins and cannot share a module, and §14
 * requires them to say the same thing. `shopifyBridge.test.ts` asserts the two
 * copies are byte-identical, so drift fails a test instead of showing a person
 * two different answers about whose turn it is.
 *
 * Every row has a `recovery`, including the ones that ended well. §14's
 * requirement is that a closed tab, a reload, an expiry and a cancellation each
 * produce a bounded instruction — never a silent disabled state — and the only
 * way to guarantee that is for there to be no state without one.
 */
export const PAIRING_GUIDANCE: Readonly<Record<string, PairingGuidance>> = {
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

const UNKNOWN_STATE: PairingGuidance = {
  actor: "operator",
  next: "Waiting for a pairing.",
  recovery: "Create a pairing in the harness and open its launch URL on the storefront.",
};

/** The last four characters — what both tabs show, so a person can match them. */
export function pairingSuffix(pairingId: string): string {
  return pairingId.length > 4 ? pairingId.slice(-4) : pairingId;
}

/**
 * A launch URL with its fragment replaced by an ellipsis.
 *
 * Shown so a person can confirm the link points at their own store before they
 * open it. The fragment is the credential, so it never appears — and this
 * function returning the redacted form rather than the caller remembering to
 * redact is the difference between a rule and a habit.
 */
export function redactLaunchUrl(launchUrl: string): string {
  const hash = launchUrl.indexOf("#");
  return hash === -1 ? launchUrl : `${launchUrl.slice(0, hash)}#…`;
}

export interface ShopifyPairingPanelProps {
  readonly moduleStatus: string;
  readonly moduleReason: string;
  /** The workspace's selected contract, or `null`. A pairing is created for one. */
  readonly contractId: string | null;
  readonly pairing: ShopifyPairing | null;
  /** The redacted launch URL, present only while this page still holds one. */
  readonly redactedLaunchUrl: string | null;
  readonly busy: boolean;
  readonly error: string | null;
  readonly onCreate: () => void;
  readonly onOpenStorefront: () => void;
}

function Fact({
  label,
  children,
}: {
  readonly label: string;
  readonly children: React.ReactNode;
}): React.ReactElement {
  return (
    <p>
      <span className="panel__label">{label}:</span> {children}
    </p>
  );
}

/** One normalized observation's metadata (FR-113). Evidence stays server-side. */
function ObservationRows({ pairing }: { readonly pairing: ShopifyPairing }): React.ReactElement {
  if (pairing.observations.length === 0) {
    return (
      <p className="panel__note">
        No cart evidence yet. The starting cart is captured when the storefront tab connects.
      </p>
    );
  }
  return (
    <table className="pairing__evidence">
      <caption>Normalized cart evidence, as the harness recorded it</caption>
      <thead>
        <tr>
          <th scope="col">Phase</th>
          <th scope="col">Source</th>
          <th scope="col">Items</th>
          <th scope="col">Currency</th>
          <th scope="col">Subtotal</th>
          <th scope="col">Total</th>
          <th scope="col">Captured</th>
        </tr>
      </thead>
      <tbody>
        {pairing.observations.map((observation, index) => (
          <tr key={`${observation.phase}-${String(index)}`}>
            <th scope="row">{observation.phase}</th>
            <td>{observation.provider} / {observation.provenance}</td>
            {/* "not reported" rather than a zero: an unreported total and a
                total of 0.00 are different facts, and only one is evidence. */}
            <td>{observation.itemCount === null ? "not reported" : String(observation.itemCount)}</td>
            <td>{observation.currency ?? "not reported"}</td>
            <td>{observation.subtotal ?? "not reported"}</td>
            <td>{observation.total ?? "not reported"}</td>
            <td>{observation.capturedAt ?? "not reported"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function ShopifyPairingPanel({
  moduleStatus,
  moduleReason,
  contractId,
  pairing,
  redactedLaunchUrl,
  busy,
  error,
  onCreate,
  onOpenStorefront,
}: ShopifyPairingPanelProps): React.ReactElement {
  if (moduleStatus !== "enabled") {
    // §21.1: a module that is off says so, with its reason. A form that would
    // refuse on submit is the shape this deliberately is not.
    return (
      <section className="panel" aria-label="Shopify pairing" id="panel-shopify" tabIndex={-1}>
        <h3>Verify a Shopify development-store cart</h3>
        <p className="panel__note">
          This deployment has the Shopify target <strong>{moduleStatus}</strong>
          {moduleReason === "" ? "." : ` — ${moduleReason}`}
        </p>
        <p className="panel__note">
          It stays off until an operator configures one authorized development store, one test
          variant, and the expected currency. Those are server-held on purpose: a workspace that
          could name its own store could point the harness at a stranger&rsquo;s.
        </p>
      </section>
    );
  }

  const status = pairing?.status ?? "";
  const guidance = (status === "" ? undefined : PAIRING_GUIDANCE[status]) ?? UNKNOWN_STATE;
  // The server's guidance wins when it sends any (FR-120); the table is the
  // rendering of its `status` token when it does not.
  const actor = pairing?.activeActor ?? guidance.actor;
  const next = pairing?.nextAction ?? guidance.next;
  const terminal = pairing !== null && isTerminalPairing(status);

  return (
    <section className="panel" aria-label="Shopify pairing" id="panel-shopify" tabIndex={-1}>
      <h3>Verify a Shopify development-store cart</h3>
      <p className="panel__note">
        The store&rsquo;s own tools move the cart. The checked-in theme bridge reads{" "}
        <code>cart.js</code> in the same shopper session and reports it here, so the verdict comes
        from the cart rather than from what a tool said about it. No checkout, no order, no payment.
      </p>

      {error === null ? null : <p className="panel__error" role="alert">{error}</p>}

      {pairing === null ? (
        <>
          <button
            type="button"
            id="action-create-shopify-pairing"
            disabled={busy || contractId === null}
            onClick={onCreate}
          >
            Create pairing
          </button>
          {/* §8.4: the disabled state is never the only signal. */}
          <p className="panel__note">
            {contractId === null
              ? "Select a Shopify contract first — a pairing is bound to the contract it will verify."
              : "Creates a 15-minute pairing bound to this workspace, contract, and store origin."}
          </p>
        </>
      ) : (
        <div className="pairing__live" data-status={status}>
          {/* The four facts §14 requires both tabs to agree on. The storefront
              card shows the same four, in the same words. */}
          <Fact label="Pairing">
            <code>…{pairingSuffix(pairing.pairingId)}</code>{" "}
            <span data-status={status}>{status}</span>
          </Fact>
          <Fact label="Expires">{pairing.expiresAt ?? "not reported"}</Fact>
          <Fact label="Acting now">{actor}</Fact>
          <Fact label="Next">{next}</Fact>
          {/* Present in every state, terminal ones included — §14 forbids a
              dead-end that shows only a disabled control. */}
          <Fact label="If that does not happen">{guidance.recovery}</Fact>

          {pairing.storeOrigin === null ? null : (
            <Fact label="Store">
              <code>{pairing.storeOrigin}</code>
            </Fact>
          )}
          {pairing.runId === null ? null : (
            <Fact label="Run">
              <code>{pairing.runId}</code>
              {pairing.overallResult === null ? null : (
                <>
                  {" — "}
                  <strong data-status={pairing.overallResult}>{pairing.overallResult}</strong>
                </>
              )}
            </Fact>
          )}
          {pairing.report === null ? null : (
            <p>
              <a href={pairing.report}>Open the immutable report</a>
            </p>
          )}

          {redactedLaunchUrl === null ? null : (
            <div className="pairing__launch">
              <p className="panel__note">
                Open this on the storefront, in a fresh tab with an empty cart. The link carries a
                one-time credential in its fragment, so it is never displayed in full and is not
                kept after you leave this page.
              </p>
              <p>
                <code>{redactedLaunchUrl}</code>
              </p>
              <button type="button" disabled={busy} onClick={onOpenStorefront}>
                Open the storefront tab
              </button>
            </div>
          )}

          <ObservationRows pairing={pairing} />

          {!terminal ? null : (
            <button type="button" disabled={busy || contractId === null} onClick={onCreate}>
              Create a new pairing
            </button>
          )}
        </div>
      )}
    </section>
  );
}

/** How often a live pairing is re-read. Slow enough to leave FR-009's budget alone. */
const POLL_MS = 3_000;

export interface ShopifyPairingSectionProps {
  readonly moduleStatus: string;
  readonly moduleReason: string;
  readonly contractId: string | null;
}

/**
 * The panel plus the state it owns: create, hold the launch URL in memory, and
 * poll while the pairing is live.
 *
 * Self-contained deliberately. Nothing else on the page reads a pairing, its
 * cadence is its own, and keeping it here is what lets `App` mount the whole
 * journey with one element instead of six props and three effects.
 */
export function ShopifyPairingSection({
  moduleStatus,
  moduleReason,
  contractId,
}: ShopifyPairingSectionProps): React.ReactElement {
  const [pairing, setPairing] = useState<ShopifyPairing | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [redacted, setRedacted] = useState<string | null>(null);
  /**
   * The launch URL, with its credential.
   *
   * A ref rather than state because it must never take part in rendering: state
   * is what a component displays, and the one thing this value must never do is
   * reach the DOM. It is read by `window.open` and by nothing else, and it is
   * dropped the moment the pairing stops being `created`.
   */
  const launchUrlRef = useRef<string | null>(null);

  const create = useCallback(() => {
    if (contractId === null) {
      return;
    }
    setBusy(true);
    setError(null);
    // The selected template establishes operator intent and the Shopify target.
    // The request deliberately leaves the contract id unstated so FastAPI binds
    // the configured variant and currency into a fresh immutable contract.
    void createShopifyPairing().then(
      (created) => {
        // Split, deliberately: the URL goes to the ref and the *rest* goes to
        // state. Passing `created` straight to `setPairing` would work and would
        // put a credential in render state, one careless `JSON.stringify` away
        // from a screen or a log.
        const { launchUrl, ...record } = created;
        launchUrlRef.current = launchUrl;
        setRedacted(redactLaunchUrl(launchUrl));
        setPairing(record);
        setBusy(false);
      },
      (caught: unknown) => {
        setError(caught instanceof ApiError ? caught.message : "The pairing could not be created.");
        setBusy(false);
      },
    );
  }, [contractId]);

  const openStorefront = useCallback(() => {
    const target = launchUrlRef.current;
    if (target === null) {
      return;
    }
    // `noopener` so the storefront tab cannot reach back through
    // `window.opener` into the harness page that holds the workspace cookie.
    window.open(target, "_blank", "noopener,noreferrer");
  }, []);

  const pairingId = pairing?.pairingId ?? null;
  const status = pairing?.status ?? "";
  const live = pairingId !== null && !isTerminalPairing(status);

  // Chained `setTimeout`, never `setInterval`: a slow response must not let
  // requests stack up. The `active` guard drops a response that lands after the
  // effect was torn down, so a stale pairing cannot overwrite a fresh one.
  useEffect(() => {
    if (!live || pairingId === null) {
      return;
    }
    let active = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const controller = new AbortController();

    const tick = (): void => {
      void readShopifyPairing(pairingId, controller.signal).then(
        (next) => {
          if (!active) {
            return;
          }
          setPairing(next);
          if (isTerminalPairing(next.status)) {
            // The credential is spent once the bridge has redeemed it, and
            // useless afterwards. Dropping it here means the page stops holding
            // a secret it can no longer do anything with.
            launchUrlRef.current = null;
            setRedacted(null);
            return;
          }
          if (next.status !== "created") {
            launchUrlRef.current = null;
            setRedacted(null);
          }
          timer = setTimeout(tick, POLL_MS);
        },
        () => {
          // A failed read is not a state change: the pairing is whatever the
          // server last said it was, and the panel keeps showing that rather
          // than inventing an error state for the pairing itself.
          if (active) {
            timer = setTimeout(tick, POLL_MS);
          }
        },
      );
    };

    timer = setTimeout(tick, POLL_MS);
    return () => {
      active = false;
      controller.abort();
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  }, [live, pairingId]);

  // A page that goes away must not leave the credential behind it in a closure
  // some other reference still holds.
  useEffect(() => {
    return () => {
      launchUrlRef.current = null;
    };
  }, []);

  return (
    <ShopifyPairingPanel
      moduleStatus={moduleStatus}
      moduleReason={moduleReason}
      contractId={contractId}
      pairing={pairing}
      redactedLaunchUrl={redacted}
      busy={busy}
      error={error}
      onCreate={create}
      onOpenStorefront={openStorefront}
    />
  );
}
