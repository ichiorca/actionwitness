/**
 * 006-T8 — the Buggy Store browser bridge (§11.2, §14.3, §14.9).
 *
 * Two properties carry this file.
 *
 * **Everything goes through the generic harness route.** §11.2 forbids reaching
 * the store from React, and the reason is evidential rather than architectural:
 * a call that bypassed the harness would change the world with no start event,
 * no terminal event, and no independent observation.
 * `test_every_tool_calls_only_the_recorded_harness_route` is that rule.
 *
 * **Checkout waits without hanging.** §14.3 keeps the tool promise pending
 * while a human decides, and §14.9 says an abandoned invocation cancels its own
 * request. A wait that could not be interrupted would leave a person deciding
 * on an action nobody is listening for — so the cancellation path is tested as
 * carefully as the approval one.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConfirmationCoordinator } from "../../state/confirmations";
import { type InstalledDouble, installModelContextDouble } from "../../test/modelContextDouble";
import { readSurface } from "../../webmcp/adapter";
import { observedToolIdentityHash } from "../../webmcp/identity";
import {
  APPLY_DISCOUNT,
  PROCEED_TO_CHECKOUT,
  SEARCH_CATALOG,
  UPDATE_CART,
  useBuggyStoreTools,
} from "./tools";

let installed: InstalledDouble | null = null;
let calls: { url: string; method: string; body: unknown }[] = [];

function respond(handler: (url: string, method: string) => Response): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      // `input` is always a string path here — the client builds relative
      // URLs — but the DOM type admits a `Request`, whose `toString` is
      // Object's and would silently record "[object Object]" for every call,
      // making the route assertions below vacuous.
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const method = init?.method ?? "GET";
      // The body is recorded for the same reason the URL is narrowed: most of
      // `BodyInit` stringifies to "[object Object]", and an assertion against
      // that would pass for a request that carried nothing.
      const raw = init?.body;
      const body: unknown = typeof raw === "string" ? JSON.parse(raw) : null;
      calls.push({ url, method, body });
      return handler(url, method);
    }),
  );
}

/** The schema one registered tool publishes, as `getTools()` reports it. */
async function publishedSchema(name: string): Promise<Record<string, unknown>> {
  const surface = await readSurface();
  const tool = surface?.find((entry) => entry.name === name);
  if (tool === undefined) {
    throw new Error(`${name} is not registered`);
  }
  return tool.input_schema;
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  installed = installModelContextDouble();
  calls = [];
  respond(() => json({ status: "completed", observed: { state_changed: true } }));
});

afterEach(() => {
  installed?.uninstall();
  installed = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const noop = async (): Promise<void> => undefined;

async function mounted(phase = "running", coordinator?: ConfirmationCoordinator) {
  const rendered = renderHook(() =>
    useBuggyStoreTools("run_1", phase, noop, coordinator ?? new ConfirmationCoordinator()),
  );
  await waitFor(() =>
    expect(rendered.result.current.states[UPDATE_CART]?.phase).toBe("registered"),
  );
  return rendered;
}

describe("registration", () => {
  it("publishes all five tools while a run is open", async () => {
    await mounted("running");

    expect([...(installed?.modelContext.toolNames ?? [])].sort()).toEqual([
      "apply_discount",
      "get_cart",
      "proceed_to_checkout",
      "search_catalog",
      "update_cart",
    ]);
  });

  it("publishes nothing once the run is terminal", async () => {
    const rendered = renderHook(() =>
      useBuggyStoreTools("run_1", "failed", noop, new ConfirmationCoordinator()),
    );
    await waitFor(() =>
      expect(rendered.result.current.states[UPDATE_CART]?.phase).not.toBe("registered"),
    );

    // Offering a target action against a sealed run would invite an agent to
    // act on evidence that is already closed.
    expect(installed?.modelContext.toolNames).toEqual([]);
  });

  it("stays registered while a human is deciding", async () => {
    // Premise corrected by the Tier 1 gate run (2026-09-01): this test used to
    // assert the tools UNregister during `awaiting_confirmation`, and the real
    // browser proved that behaviour orphans the in-flight caller — the pinned
    // build never settles an executeTool promise whose registration was torn
    // down, so the agent that asked for checkout waits forever while the
    // server completes without it. §14.3 requires the promise to stay pending
    // across the decision, so the registration must outlive it. New calls
    // during the pause are refused by the SERVER's run state machine, which
    // §11.2 makes the authoritative gate.
    const rendered = renderHook(() =>
      useBuggyStoreTools("run_1", "awaiting_confirmation", noop, new ConfirmationCoordinator()),
    );
    await waitFor(() =>
      expect(rendered.result.current.states[UPDATE_CART]?.phase).toBe("registered"),
    );

    expect(installed?.modelContext.toolNames).toContain(PROCEED_TO_CHECKOUT);
  });
});

describe("dispatch", () => {
  it("every tool calls only the recorded harness route", async () => {
    await mounted();

    await installed?.modelContext.invoke("search_catalog", { query: "mug" });
    await installed?.modelContext.invoke(UPDATE_CART, {
      product_id: "mug-ceramic-001",
      quantity: 1,
      request_id: "req_onemug",
    });
    await installed?.modelContext.invoke("apply_discount", { code: "SAVE20" });

    // Nothing reaches the store: a direct call would mutate the world with no
    // start event, no terminal event, and no independent observation.
    expect(calls.every((call) => call.url.startsWith("/api/v1/runs/run_1/target-tools/"))).toBe(
      true,
    );
    expect(calls.some((call) => call.url.includes("/demo/"))).toBe(false);
  });
});

describe("each invocation carries the identity it observed (FR-169)", () => {
  it("sends the identity hash of the definition the browser reports", async () => {
    // FR-169: "each recorded target-tool invocation shall carry the identity
    // hash of the tool definition as observed at invocation time". The server
    // has refused on mismatch since 005, but the field is optional and no
    // client sent one — so the check was dead in production and green only
    // where a test hand-fed the hash.
    //
    // Asserted against the shared computation rather than a literal, because a
    // literal here would pass while the *client* and the *server* disagreed;
    // `identity.test.ts` is where the shared computation is pinned to Python's.
    await mounted();

    await installed?.modelContext.invoke(UPDATE_CART, {
      product_id: "mug-ceramic-001",
      quantity: 1,
      request_id: "req_onemug",
    });

    const expected = await observedToolIdentityHash(UPDATE_CART);
    expect(expected).not.toBeNull();
    const invocation = calls.find((call) => call.url.includes(`${UPDATE_CART}:invoke`));
    expect(invocation?.body).toEqual({
      arguments: { product_id: "mug-ceramic-001", quantity: 1, request_id: "req_onemug" },
      tool_identity_hash: expected,
    });
  });

  it("sends the altered definition's hash when the registry reports one", async () => {
    // AC-25 from the client's side. The registry reports a definition that no
    // longer matches the armed baseline while the genuine handler still runs —
    // which is the case a hash frozen at registration could never notice, and
    // the case FR-169 requires to fail "even if no `toolchange` event was
    // observed". A hash taken from this module's own source literal would agree
    // with the baseline by construction and could never disagree with anything.
    await mounted();
    const registry = installed?.modelContext;
    expect(registry).toBeDefined();
    const armed = await observedToolIdentityHash(APPLY_DISCOUNT);
    expect(armed).not.toBeNull();

    const reported = await (registry as NonNullable<typeof registry>).getTools();
    vi.spyOn(registry as NonNullable<typeof registry>, "getTools").mockResolvedValue(
      reported.map((tool) =>
        tool.name === APPLY_DISCOUNT
          ? { ...tool, description: "Apply a discount code. [look-alike]" }
          : tool,
      ),
    );
    await registry?.invoke(APPLY_DISCOUNT, { code: "SAVE20" });

    const invocation = calls.find((call) => call.url.includes(`${APPLY_DISCOUNT}:invoke`));
    const sent = (invocation?.body as { tool_identity_hash?: string } | undefined)
      ?.tool_identity_hash;
    expect(sent).toBeDefined();
    // Different from the armed identity, which is what the server refuses on.
    expect(sent).not.toBe(armed);
  });

  it("still invokes, without the field, when no hash can be computed", async () => {
    // §15.3 keeps the field optional, and the server documents why: a client
    // that cannot compute one must still be able to invoke. Omitting it narrows
    // the evidence — the surface capture still reaches `stable_tool_surface` —
    // where refusing would make an un-instrumented browser unable to act at all,
    // which is this page inventing a policy the server does not have.
    await mounted();
    const registry = installed?.modelContext;
    expect(registry).toBeDefined();
    vi.spyOn(registry as NonNullable<typeof registry>, "getTools").mockRejectedValue(
      new Error("the registry is unavailable"),
    );

    const result = await registry?.invoke(SEARCH_CATALOG, { query: "mug" });

    // The call happened, and it carried arguments and nothing else.
    const invocation = calls.find((call) => call.url.includes(`${SEARCH_CATALOG}:invoke`));
    expect(invocation?.body).toEqual({ arguments: { query: "mug" } });
    expect((result as { isError?: boolean }).isError).toBeFalsy();
  });
});

describe("the published schemas are Appendix D.2's", () => {
  it("constrains update_cart to the seeded products and the quantity ceiling", async () => {
    // D.2 gives `product_id` an enum of the three seeded ids and `quantity` a
    // maximum of 5. The browser registration had drifted to a plain bounded
    // string and an unbounded integer while the Python adapter kept publishing
    // the enum — so the agent-facing discovery surface described a wider tool
    // than the one that exists, and the looser description is the one an agent
    // reads.
    await mounted();

    const schema = await publishedSchema(UPDATE_CART);

    const properties = schema["properties"] as Record<string, Record<string, unknown>>;
    expect(properties["product_id"]?.["enum"]).toEqual([
      "mug-ceramic-001",
      "notebook-001",
      "tote-001",
    ]);
    expect(properties["quantity"]?.["minimum"]).toBe(0);
    expect(properties["quantity"]?.["maximum"]).toBe(5);
    expect(schema["required"]).toEqual(["product_id", "quantity", "request_id"]);
    expect(schema["additionalProperties"]).toBe(false);
  });

  it("constrains apply_discount to the allowlisted code", async () => {
    await mounted();

    const schema = await publishedSchema(APPLY_DISCOUNT);

    const properties = schema["properties"] as Record<string, Record<string, unknown>>;
    expect(properties["code"]?.["enum"]).toEqual(["SAVE20"]);
    expect(schema["required"]).toEqual(["code"]);
  });

  it("keeps search_catalog's D.2 result bound", async () => {
    // The one D.2 bound that had not drifted. Asserted so a future edit cannot
    // quietly widen it while the two tests above hold the others.
    await mounted();

    const schema = await publishedSchema(SEARCH_CATALOG);

    const properties = schema["properties"] as Record<string, Record<string, unknown>>;
    expect(properties["max_results"]?.["minimum"]).toBe(1);
    expect(properties["max_results"]?.["maximum"]).toBe(5);
  });
});

describe("checkout waits for a human", () => {
  it("stays pending until the decision arrives, then acts once", async () => {
    const coordinator = new ConfirmationCoordinator();
    let seen = 0;
    respond((url) => {
      if (url.includes(":invoke")) {
        seen += 1;
        return seen === 1
          ? json({ status: "awaiting_confirmation", confirmation: { confirmation_id: "cnf_1" } })
          : json({ status: "completed", reported: { status: "success" } });
      }
      return json({});
    });
    await mounted("running", coordinator);

    const pending = installed?.modelContext.invoke(PROCEED_TO_CHECKOUT, {
      request_id: "req_checkoutone",
    });

    // The promise is genuinely unresolved: the agent is waiting, exactly as
    // §14.3 requires.
    await waitFor(() => expect(coordinator.isWaiting("cnf_1")).toBe(true));
    expect(seen).toBe(1);

    coordinator.settle("cnf_1", { kind: "approved" });

    // Results leave the adapter normalized, so the body is inside the text
    // block rather than at the top level.
    expect(JSON.stringify(await pending)).toContain("completed");
    // Exactly once more — the approval authorizes one action.
    expect(seen).toBe(2);
  });

  it("still settles when the phase moves to awaiting_confirmation mid-call", async () => {
    // **The Tier 1 gate's actual failure, as a test.** The corrected premise
    // above asserts the tools are registered *at* `awaiting_confirmation`,
    // which is static. What broke in the browser was the transition: the app
    // refreshes after the pause, the phase moves, the hook re-renders, and the
    // old code tore the registration down while its own invocation was still
    // waiting. The pinned build never settles an `executeTool` promise whose
    // registration vanished — the server completed and the agent waited
    // forever.
    //
    // Invoked the way that build really does, with no context argument, so
    // this exercises the same path the crash came from rather than the
    // double's more generous one.
    const coordinator = new ConfirmationCoordinator();
    let seen = 0;
    respond((url) => {
      if (url.includes(":invoke")) {
        seen += 1;
        return seen === 1
          ? json({ status: "awaiting_confirmation", confirmation: { confirmation_id: "cnf_1" } })
          : json({ status: "completed", reported: { status: "success" } });
      }
      return json({});
    });

    const rendered = renderHook(
      ({ phase }: { phase: string }) =>
        useBuggyStoreTools("run_1", phase, noop, coordinator),
      { initialProps: { phase: "running" } },
    );
    await waitFor(() =>
      expect(rendered.result.current.states[PROCEED_TO_CHECKOUT]?.phase).toBe("registered"),
    );

    const pending = installed?.modelContext.invokeAsPinnedBuild(PROCEED_TO_CHECKOUT, {
      request_id: "req_checkoutone",
    });
    await waitFor(() => expect(coordinator.isWaiting("cnf_1")).toBe(true));

    // The workspace moves, exactly as `refresh()` makes it.
    rendered.rerender({ phase: "awaiting_confirmation" });
    await waitFor(() =>
      expect(installed?.modelContext.toolNames).toContain(PROCEED_TO_CHECKOUT),
    );

    coordinator.settle("cnf_1", { kind: "approved" });

    // The caller hears the answer. Before the fix this promise never settled.
    expect(JSON.stringify(await pending)).toContain("completed");
    expect(seen).toBe(2);
  });

  it("cancels its own request when the agent walks away", async () => {
    const coordinator = new ConfirmationCoordinator();
    respond((url) => {
      if (url.includes(":invoke")) {
        return json({ status: "awaiting_confirmation", confirmation: { confirmation_id: "cnf_1" } });
      }
      return json({});
    });
    await mounted("running", coordinator);

    const controller = new AbortController();
    const pending = installed?.modelContext.invoke(
      PROCEED_TO_CHECKOUT,
      { request_id: "req_checkoutone" },
      controller.signal,
    );
    await waitFor(() => expect(coordinator.isWaiting("cnf_1")).toBe(true));

    controller.abort();

    // §14.9: the request is cancelled rather than left for a person to answer
    // on nobody's behalf.
    await waitFor(() =>
      expect(calls.some((call) => call.method === "DELETE" && call.url.includes("cnf_1"))).toBe(
        true,
      ),
    );
    // And the agent is told, as a normalized error rather than a hang.
    await expect(pending).resolves.toMatchObject({ isError: true });
  });

  it("reports a denial as an error rather than an order", async () => {
    const coordinator = new ConfirmationCoordinator();
    respond((url) =>
      url.includes(":invoke")
        ? json({ status: "awaiting_confirmation", confirmation: { confirmation_id: "cnf_1" } })
        : json({}),
    );
    await mounted("running", coordinator);

    const pending = installed?.modelContext.invoke(PROCEED_TO_CHECKOUT, {
      request_id: "req_checkoutone",
    });
    await waitFor(() => expect(coordinator.isWaiting("cnf_1")).toBe(true));

    coordinator.settle("cnf_1", {
      kind: "refused",
      status: "denied",
      detail: "The action was refused. Nothing was changed.",
    });

    // A safe block for the run, but still an error to the agent: reading a
    // refusal as an order is the one misunderstanding that must not happen.
    await expect(pending).resolves.toMatchObject({ isError: true });
    expect(JSON.stringify(await pending)).toContain("Nothing was changed");
  });

  it("does not wait when the contract does not protect checkout", async () => {
    const coordinator = new ConfirmationCoordinator();
    respond(() => json({ status: "completed", reported: { status: "success" } }));
    await mounted("running", coordinator);

    const outcome = await installed?.modelContext.invoke(PROCEED_TO_CHECKOUT, {
      request_id: "req_checkoutone",
    });

    // Nothing paused, so nothing waits — a gate that engaged unconditionally
    // would stall a journey the operator never asked to gate.
    expect(coordinator.pendingIds).toEqual([]);
    expect(JSON.stringify(outcome)).toContain("completed");
  });
});

describe("the coordinator settles exactly once", () => {
  it("ignores a decision that arrives after a cancellation", async () => {
    const coordinator = new ConfirmationCoordinator();
    const controller = new AbortController();
    const waiting = coordinator.wait("cnf_1", controller.signal);

    controller.abort();
    coordinator.settle("cnf_1", { kind: "approved" });

    // Whichever arrives first is the outcome; a late approval must not
    // resurrect a call the agent has already abandoned.
    await expect(waiting).resolves.toEqual({ kind: "cancelled" });
  });

  it("returns immediately for an already-abandoned invocation", async () => {
    const coordinator = new ConfirmationCoordinator();
    const controller = new AbortController();
    controller.abort();

    await expect(coordinator.wait("cnf_1", controller.signal)).resolves.toEqual({
      kind: "cancelled",
    });
    expect(coordinator.pendingIds).toEqual([]);
  });
});

describe("a recorded failure reaches the agent as a failure", () => {
  it("returns isError when the harness recorded the invocation as failed", async () => {
    // The route answers 200 for an invocation that completed the round trip,
    // whether or not the target did what was asked — the status is about the
    // harness. Until the terminal event was read, a mutation refused for a
    // reused idempotency key resolved as an ordinary value, the adapter
    // normalized it as a success, and an agent branching on `isError` was told
    // a refused mutation had worked. §14.8 forbids exactly that reading for a
    // denied confirmation, and a refusal is a refusal whichever rail made it.
    respond(() =>
      json({
        status: "completed",
        terminal_event: "tool_invocation_failed",
        reported: {
          status: null,
          summary: "request_id 'k' was already used for 'update_cart' with a different payload",
          error_code: "idempotency_key_reused",
        },
        observed: { state_changed: false },
      }),
    );
    await mounted("running");

    // Invoked the way the pinned build does — arguments only, no context.
    const result = (await installed?.modelContext.invokeAsPinnedBuild(UPDATE_CART, {
      product_id: "mug-ceramic-001",
      quantity: 1,
      request_id: "k",
    })) as { content: { text: string }[]; isError?: boolean };

    expect(result.isError).toBe(true);
    // The server's own summary, plus the stable token an agent branches on when
    // the prose changes. Nothing internal: §20 keeps the summary user-facing.
    expect(result.content[0]?.text).toContain("already used");
    expect(result.content[0]?.text).toContain("idempotency_key_reused");
  });

  it("still refreshes the workspace after a refused mutation", async () => {
    // A refused mutation still appended events, so the phase the server reports
    // may have moved. Leaving the page on its pre-call reading would be a UI
    // that disagrees with the timeline beside it.
    respond(() =>
      json({
        status: "completed",
        terminal_event: "tool_invocation_failed",
        reported: { status: null, summary: "refused", error_code: "idempotency_key_reused" },
      }),
    );
    let refreshes = 0;
    const refresh = async (): Promise<void> => {
      refreshes += 1;
    };
    const rendered = renderHook(() =>
      useBuggyStoreTools("run_1", "running", refresh, new ConfirmationCoordinator()),
    );
    await waitFor(() =>
      expect(rendered.result.current.states[UPDATE_CART]?.phase).toBe("registered"),
    );

    await installed?.modelContext.invokeAsPinnedBuild(UPDATE_CART, {
      product_id: "mug-ceramic-001",
      quantity: 1,
      request_id: "k",
    });

    expect(refreshes).toBeGreaterThan(0);
  });

  it("leaves a successful invocation alone", async () => {
    // The guard on both tests above: a terminal event that is not a failure
    // must still resolve, or every call would read as an error.
    respond(() =>
      json({
        status: "completed",
        terminal_event: "tool_invocation_completed",
        reported: { status: "success" },
      }),
    );
    await mounted("running");

    const result = (await installed?.modelContext.invokeAsPinnedBuild(UPDATE_CART, {
      product_id: "mug-ceramic-001",
      quantity: 1,
      request_id: "k",
    })) as { isError?: boolean };

    expect(result.isError).toBeFalsy();
  });
});
