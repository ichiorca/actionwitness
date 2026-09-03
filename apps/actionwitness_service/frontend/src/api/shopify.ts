/**
 * The Shopify development-store pairing, from the harness page (§15.7, §16.5,
 * FR-111–FR-117).
 *
 * Narrowing and requests only, like every other client in this directory. The
 * pairing's state machine, its expiry, the normalization of `/cart.js`, and the
 * verdict all belong to the server; this module creates a pairing and reads what
 * the server says about it.
 *
 * **This page never sees a credential, and that is structural.** §15.7 puts the
 * one-time credential in the launch URL's *fragment*, and the fragment is the
 * one part of a URL that never reaches a server. The status endpoint is
 * specified to return no raw credential at all. So there is no field below that
 * could carry one — a parser that added `credential` would be inventing a shape
 * the server does not send, and the reason to say so here is that it would look
 * like an ordinary convenience if somebody later wanted the panel to display
 * "the token" for debugging.
 *
 * The launch URL itself is the one exception, and it exists for exactly one
 * purpose: to be handed to a storefront tab. It is displayed for copying and
 * carries the credential in its fragment — which is why the panel that renders
 * it treats it as a secret rather than as a link to decorate.
 */

import {
  isRecord,
  optionalString,
  request,
  requireArray,
  requireRecord,
  requireString,
} from "./client";

/**
 * One normalized observation, as the status endpoint describes it (FR-113).
 *
 * Metadata, not the cart: §11.4 keeps detailed evidence server-side, and the
 * report endpoint is where the full normalized document lives. What a person
 * needs on this panel is enough to see that the evidence is *about the right
 * thing* — the phase, when it was captured, its hash, and the totals the
 * contract is asserted against.
 *
 * Everything but `phase` is nullable because a `before` observation is
 * summarized while the run is still open, and the server may have nothing to
 * say yet about a currency or a total. A missing number renders as "not
 * reported", never as zero: a subtotal of `0.00` and a subtotal nobody has
 * computed are different facts, and only one of them is evidence.
 */
export interface PairingObservation {
  readonly phase: string;
  readonly capturedAt: string | null;
  readonly contentHash: string | null;
  readonly capturePath: string | null;
  readonly provider: string;
  readonly provenance: string;
  readonly itemCount: number | null;
  readonly currency: string | null;
  readonly subtotal: string | null;
  readonly total: string | null;
}

/** §16.5's pairing, as the harness UI reads it. */
export interface ShopifyPairing {
  readonly pairingId: string;
  /** One of §16.5's states. Kept as the server's own token, never re-derived. */
  readonly status: string;
  readonly expiresAt: string | null;
  readonly storeOrigin: string | null;
  readonly contractId: string | null;
  readonly runId: string | null;
  readonly overallResult: string | null;
  /** Where the immutable report lives, when one exists. */
  readonly report: string | null;
  /**
   * The server's own guidance, when it sends any (FR-120).
   *
   * `null` is the ordinary case for these routes, and the panel falls back to
   * the §16.5 table rather than to silence. That fallback is a *rendering* of
   * the server's `status` — a pure lookup on a value the server chose — not a
   * second opinion about what happens next.
   */
  readonly activeActor: string | null;
  readonly nextAction: string | null;
  readonly observations: readonly PairingObservation[];
}

/** A created pairing, plus the one-shot launch URL (§15.7, `Cache-Control: no-store`). */
export interface CreatedPairing extends ShopifyPairing {
  /**
   * The launch URL, whose **fragment carries the one-time credential**.
   *
   * Shown once, for copying into a storefront tab. It is never persisted by
   * this page and never written anywhere a later reader could find it.
   */
  readonly launchUrl: string;
}

function parseNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Money as the server sent it — a string, always.
 *
 * FR-113 normalizes minor units into fixed-scale decimal strings, and turning
 * one back into a JavaScript number here would reintroduce exactly the
 * float error the fixed-scale representation exists to avoid. A number that
 * arrives anyway is refused rather than coerced.
 */
function parseMoney(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function parseObservation(value: unknown, field: string): PairingObservation {
  const record = requireRecord(value, field);
  return {
    phase: requireString(record["phase"], `${field}.phase`),
    capturedAt: optionalString(record["captured_at"]),
    contentHash: optionalString(record["content_hash"]),
    capturePath: optionalString(record["capture_url_path"]),
    provider: requireString(record["provider"], `${field}.provider`),
    provenance: requireString(record["provenance"], `${field}.provenance`),
    itemCount: parseNumber(record["item_count"]),
    currency: optionalString(record["currency"]),
    subtotal: parseMoney(record["subtotal"]),
    total: parseMoney(record["total"]),
  };
}

function parsePairingRecord(value: unknown, field: string): ShopifyPairing {
  const record = requireRecord(value, field);
  const observations = record["observations"];
  return {
    pairingId: requireString(record["pairing_id"], `${field}.pairing_id`),
    status: requireString(record["status"], `${field}.status`),
    expiresAt: optionalString(record["expires_at"]),
    storeOrigin: optionalString(record["store_origin"]),
    contractId: optionalString(record["contract_id"]),
    runId: optionalString(record["run_id"]),
    overallResult: optionalString(record["overall_result"]),
    report: optionalString(record["report"]),
    activeActor: optionalString(record["active_actor"]),
    nextAction: optionalString(record["next_action"]),
    // Absent and empty mean the same thing here — no evidence has landed yet —
    // so an absent list is not worth failing a parse over.
    observations: Array.isArray(observations)
      ? requireArray(observations, `${field}.observations`).map((entry, index) =>
          parseObservation(entry, `${field}.observations[${String(index)}]`),
        )
      : [],
  };
}

/**
 * Unwrap `{ "pairing": { … } }` or a bare record.
 *
 * The service half of §15.7 is being built alongside this one, and both
 * envelopes are in use elsewhere in this API — `/audits` returns the record
 * bare, `/audits/current` wraps it. Accepting either is not laxity about the
 * contract: every field inside is still required or explicitly optional, and a
 * body that is neither shape still fails with the field name that was missing.
 */
function unwrap(value: unknown): unknown {
  return isRecord(value) && isRecord(value["pairing"]) ? value["pairing"] : value;
}

export async function createShopifyPairing(
  contractId: string,
  signal?: AbortSignal,
): Promise<CreatedPairing> {
  return await request("/shopify/pairings", {
    method: "POST",
    body: { contract_id: contractId },
    parse: (value) => {
      const body = unwrap(value);
      return {
        ...parsePairingRecord(body, "pairing"),
        launchUrl: requireString(requireRecord(body, "pairing")["launch_url"], "launch_url"),
      };
    },
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function readShopifyPairing(
  pairingId: string,
  signal?: AbortSignal,
): Promise<ShopifyPairing> {
  return await request(`/shopify/pairings/${encodeURIComponent(pairingId)}`, {
    parse: (value) => parsePairingRecord(unwrap(value), "pairing"),
    ...(signal === undefined ? {} : { signal }),
  });
}
