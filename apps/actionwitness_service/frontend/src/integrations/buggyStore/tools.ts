/**
 * The Buggy Store browser bridge (§11.2, BUILD_ORDER invariant 3).
 *
 * These handlers call **the generic harness target route** and nothing else.
 * §11.2 is explicit: "they never call Buggy Store service objects or its API
 * directly from React". That is what keeps the recorded evidence complete — a
 * call that reached the store directly would change the world without a start
 * event, a terminal event, or an independent observation, which is precisely
 * the gap this product exists to close.
 *
 * The schemas below are the store's own, restated for the browser. They are a
 * convenience for an agent, not an authority: the harness revalidates every
 * argument against the adapter's published schema in Python before dispatching
 * (§11.4), so a mismatch here costs a round trip rather than correctness.
 *
 * ## `proceed_to_checkout` is registered natively
 *
 * Every other tool here is one request the browser may abandon harmlessly.
 * Checkout waits on a human, and ADR-0002's pinned hook forwards no
 * per-invocation signal — so an agent that walked away could not cancel its own
 * confirmation, and a person would be left deciding on an action nobody is
 * waiting for (§14.9). The native path exists for exactly this tool.
 */

import { ApiError, request } from "../../api/client";
import { type ConfirmationCoordinator, confirmations } from "../../state/confirmations";
import {
  type RegistrationState,
  useHarnessTool,
  useNativeTool,
} from "../../webmcp/adapter";

const ACTIVE_PHASES = ["armed", "running"];

/** §11.2's five tools. */
export const SEARCH_CATALOG = "search_catalog";
export const GET_CART = "get_cart";
export const UPDATE_CART = "update_cart";
export const APPLY_DISCOUNT = "apply_discount";
export const PROCEED_TO_CHECKOUT = "proceed_to_checkout";

interface InvokeOutcome {
  readonly status: string;
  readonly confirmationId: string | null;
  readonly body: Record<string, unknown>;
}

/** One target action through the recorded route. */
async function invoke(
  runId: string,
  toolName: string,
  args: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<InvokeOutcome> {
  const body = await request(`/runs/${runId}/target-tools/${toolName}:invoke`, {
    method: "POST",
    body: { arguments: args },
    ...(signal === undefined ? {} : { signal }),
    parse: (value) => {
      if (typeof value !== "object" || value === null) {
        throw new Error("The harness returned no invocation result.");
      }
      return value as Record<string, unknown>;
    },
  });
  const confirmation = body["confirmation"];
  return {
    status: typeof body["status"] === "string" ? body["status"] : "completed",
    confirmationId:
      typeof confirmation === "object" &&
      confirmation !== null &&
      typeof (confirmation as Record<string, unknown>)["confirmation_id"] === "string"
        ? ((confirmation as Record<string, unknown>)["confirmation_id"] as string)
        : null,
    body,
  };
}

export interface StoreToolset {
  readonly states: Readonly<Record<string, RegistrationState>>;
}

export function useBuggyStoreTools(
  runId: string | null,
  phase: string,
  refresh: () => Promise<void>,
  coordinator: ConfirmationCoordinator = confirmations,
): StoreToolset {
  // §11.2: "contract armed or run running". The mutating three add "not
  // verifying or awaiting confirmation" — which these phases already exclude.
  const active = runId !== null && ACTIVE_PHASES.includes(phase);

  const searchCatalog = useHarnessTool({
    name: SEARCH_CATALOG,
    description: "Search the seeded catalog and return stable product ids.",
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", minLength: 1, maxLength: 100, description: "Words to search for." },
        max_results: { type: "integer", minimum: 1, maximum: 5, description: "How many matches." },
      },
      required: ["query"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    enabled: active,
    execute: async (args: Record<string, unknown>) =>
      (await invoke(runId ?? "", SEARCH_CATALOG, args)).body,
  });

  const getCart = useHarnessTool({
    name: GET_CART,
    description: "Return the current cart contents and totals.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    enabled: active,
    execute: async () => (await invoke(runId ?? "", GET_CART, {})).body,
  });

  const updateCart = useHarnessTool({
    name: UPDATE_CART,
    description: "Set or remove one cart line using a required request id.",
    inputSchema: {
      type: "object",
      properties: {
        product_id: { type: "string", minLength: 1, description: "Catalog product id." },
        quantity: { type: "integer", minimum: 0, description: "Zero removes the line." },
        request_id: {
          type: "string",
          minLength: 8,
          maxLength: 80,
          description: "Idempotency key for this change.",
        },
      },
      required: ["product_id", "quantity", "request_id"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    enabled: active,
    execute: async (args: Record<string, unknown>) => {
      const outcome = await invoke(runId ?? "", UPDATE_CART, args);
      await refresh();
      return outcome.body;
    },
  });

  const applyDiscount = useHarnessTool({
    name: APPLY_DISCOUNT,
    description: "Apply a discount code to the current cart.",
    inputSchema: {
      type: "object",
      properties: { code: { type: "string", minLength: 1, maxLength: 32, description: "Code." } },
      required: ["code"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    enabled: active,
    execute: async (args: Record<string, unknown>) => {
      const outcome = await invoke(runId ?? "", APPLY_DISCOUNT, args);
      await refresh();
      return outcome.body;
    },
  });

  const proceedToCheckout = useNativeTool({
    name: PROCEED_TO_CHECKOUT,
    description: "Request human confirmation and create an order only after approval.",
    inputSchema: {
      type: "object",
      properties: {
        request_id: {
          type: "string",
          minLength: 8,
          maxLength: 80,
          description: "Idempotency key for this checkout attempt.",
        },
      },
      required: ["request_id"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    enabled: active,
    execute: async (args, { signal }) => {
      const current = runId ?? "";
      const first = await invoke(current, PROCEED_TO_CHECKOUT, args, signal);
      await refresh();

      if (first.status !== "awaiting_confirmation" || first.confirmationId === null) {
        // Either the contract did not protect this tool, or the run already
        // held an approval. Nothing to wait for.
        return first.body;
      }

      // §14.3: the promise stays pending while a human decides. The wait is
      // in this page, over a committed row — no server transaction is held.
      const outcome = await coordinator.wait(first.confirmationId, signal);

      if (outcome.kind === "cancelled") {
        // §14.9: the invocation was abandoned, so the request is cancelled
        // rather than left for a person to answer on nobody's behalf. Sent
        // without the (now aborted) signal, or the cancellation itself would
        // be cancelled and the dialog would outlive the caller.
        await cancel(current, first.confirmationId);
        await refresh();
        throw new Error("The checkout request was cancelled before a decision was made.");
      }

      if (outcome.kind === "refused") {
        // A safe block, not a failure — but still `isError` to the agent, so
        // it does not read a refusal as an order (§14.8).
        throw new Error(outcome.detail);
      }

      // Approved: run it once, now, with the consent recorded server-side.
      const second = await invoke(current, PROCEED_TO_CHECKOUT, args, signal);
      await refresh();
      return second.body;
    },
  });

  return {
    states: {
      [SEARCH_CATALOG]: searchCatalog,
      [GET_CART]: getCart,
      [UPDATE_CART]: updateCart,
      [APPLY_DISCOUNT]: applyDiscount,
      [PROCEED_TO_CHECKOUT]: proceedToCheckout,
    },
  };
}

/** Cancel a pending confirmation this page can no longer answer for. */
export async function cancel(runId: string, confirmationId: string): Promise<void> {
  try {
    await request(`/runs/${runId}/confirmations/${confirmationId}`, {
      method: "DELETE",
      parse: () => null,
    });
  } catch (error: unknown) {
    // A confirmation that is already decided or gone is not a problem worth
    // surfacing: the outcome we wanted — nothing pending — already holds.
    if (!(error instanceof ApiError)) {
      throw error;
    }
  }
}

/** Post a human's decision (§14.5: the cookie authorizes, not the id). */
export async function decide(
  runId: string,
  confirmationId: string,
  decision: "approve_once" | "deny",
): Promise<Record<string, unknown>> {
  return await request(`/runs/${runId}/confirmations/${confirmationId}/decision`, {
    method: "POST",
    body: { decision },
    parse: (value) => (typeof value === "object" && value !== null
      ? (value as Record<string, unknown>)
      : {}),
  });
}
