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
import { observedToolIdentityHash } from "../../webmcp/identity";

const ACTIVE_PHASES = ["armed", "running"];

/**
 * Registration must SURVIVE the confirmation pause, or the caller never hears
 * the answer: the Tier 1 gate run (2026-09-01) proved that unregistering a
 * tool mid-invocation orphans the pinned build's `executeTool` promise — the
 * handler keeps running and the server completes, but the invoking agent
 * waits forever. §14.3 requires the promise to stay pending across the human
 * decision, which structurally requires the registration to outlive it. A NEW
 * call during the pause is refused by the server (run state machine), which
 * stays the authoritative gate per §11.2.
 */
const REGISTERED_PHASES = [...ACTIVE_PHASES, "awaiting_confirmation"];

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

/**
 * One target action through the recorded route.
 *
 * ## Why the identity hash is read here rather than held from registration
 *
 * FR-169 requires each recorded invocation to carry "the identity hash of the
 * tool definition **as observed at invocation time**", and the whole point of
 * AC-25 is a definition altered between the armed capture and this call. A hash
 * derived from the literal this file registers would agree with the baseline by
 * construction and could never disagree with anything — it would be this module
 * confirming its own source code. So the definition is re-read from the browser
 * registry, through the adapter's single `getTools()` call, at the moment of the
 * call. If a look-alike replaced the tool since arming, that is what gets hashed
 * and that is what the server refuses.
 *
 * The server compares it against the armed baseline *it* hashed, so a hash this
 * page got wrong can only refuse an invocation, never admit one.
 *
 * Omitted when no honest hash exists — no WebMCP, no such tool, no secure
 * context. §15.3 makes the field optional for exactly that case, and the surface
 * capture still reaches `stable_tool_surface` on its own.
 */
async function invoke(
  runId: string,
  toolName: string,
  args: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<InvokeOutcome> {
  const identity = await observedToolIdentityHash(toolName);
  const body = await request(`/runs/${runId}/target-tools/${toolName}:invoke`, {
    method: "POST",
    body: {
      arguments: args,
      ...(identity === null ? {} : { tool_identity_hash: identity }),
    },
    ...(signal === undefined ? {} : { signal }),
    parse: (value) => {
      if (typeof value !== "object" || value === null) {
        throw new Error("The harness returned no invocation result.");
      }
      return value as Record<string, unknown>;
    },
  });
  failedInvocation(body);

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

/** The terminal event of an invocation that ran and did not succeed. */
const FAILED_TERMINAL_EVENT = "tool_invocation_failed";

/**
 * Throw when the harness recorded this invocation as failed (§11.4, §14.8).
 *
 * The route answers `200` for an invocation that *completed the round trip*,
 * whether or not the target did what was asked: the harness itself worked, so
 * the HTTP status is about the harness. The tool's own outcome lives in
 * `terminal_event`, and until this existed nothing read it — so a mutation
 * refused for a reused idempotency key resolved as an ordinary value, the
 * adapter normalized it as a success, and an agent branching on `isError` was
 * told a refused mutation had worked. That is the exact reading §14.8 forbids
 * for a denied confirmation, and a refusal is a refusal whichever rail produced
 * it.
 *
 * Thrown rather than returned as a flag, because `useHarnessTool` and
 * `useNativeTool` already turn a throw into `normalizeError`'s bounded
 * `isError` result. A second path to the same shape would eventually disagree
 * with the first.
 *
 * The message is the server's own summary, which §20 already keeps free of
 * internals; the error code is appended because it is the stable token an agent
 * can branch on when the prose changes.
 */
function failedInvocation(body: Record<string, unknown>): void {
  if (body["terminal_event"] !== FAILED_TERMINAL_EVENT) {
    return;
  }
  const reported = body["reported"];
  const detail = typeof reported === "object" && reported !== null
    ? (reported as Record<string, unknown>)
    : {};
  const summary = typeof detail["summary"] === "string" ? detail["summary"] : "";
  const code = typeof detail["error_code"] === "string" ? detail["error_code"] : "";
  throw new Error(
    summary === ""
      ? `The target refused the call${code === "" ? "." : ` (${code}).`}`
      : `${summary}${code === "" ? "" : ` (${code})`}`,
  );
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
  // verifying or awaiting confirmation" — which these phases already exclude
  // for NEW calls; the registration itself persists through the confirmation
  // pause so the in-flight caller's promise can resolve (see REGISTERED_PHASES).
  const active = runId !== null && REGISTERED_PHASES.includes(phase);

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
        // Appendix D.2's enum and bound, restored. The browser registration had
        // drifted to a plain bounded string while the Python adapter kept
        // publishing the enum, so the two discovery surfaces disagreed about
        // what an agent may send — and the looser one is the one an agent
        // actually reads. A `quantity` of 9 or a product id that was never
        // seeded then costs a refused round trip that the schema was supposed
        // to prevent.
        product_id: {
          type: "string",
          enum: ["mug-ceramic-001", "notebook-001", "tote-001"],
          description: "Stable seeded product id.",
        },
        quantity: {
          type: "integer",
          minimum: 0,
          maximum: 5,
          description: "Absolute quantity; zero removes the line.",
        },
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
    // Refreshed in a `finally`: a refused mutation still appended events, so
    // the phase the server reports may have moved even though nothing changed.
    // Leaving the page on its pre-call reading would be a UI that disagrees
    // with the timeline it is displaying.
    execute: async (args: Record<string, unknown>) => {
      try {
        return (await invoke(runId ?? "", UPDATE_CART, args)).body;
      } finally {
        await refresh();
      }
    },
  });

  const applyDiscount = useHarnessTool({
    name: APPLY_DISCOUNT,
    description: "Apply a discount code to the current cart.",
    inputSchema: {
      type: "object",
      properties: {
        // Appendix D.2's allowlist, restored for the same reason as
        // `update_cart`'s: a bounded free string invited an agent to try a code
        // the store will only ever refuse.
        code: { type: "string", enum: ["SAVE20"], description: "Allowlisted demo discount code." },
      },
      required: ["code"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    enabled: active,
    execute: async (args: Record<string, unknown>) => {
      try {
        return (await invoke(runId ?? "", APPLY_DISCOUNT, args)).body;
      } finally {
        await refresh();
      }
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
      let first: InvokeOutcome;
      try {
        first = await invoke(current, PROCEED_TO_CHECKOUT, args, signal);
      } finally {
        await refresh();
      }

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
