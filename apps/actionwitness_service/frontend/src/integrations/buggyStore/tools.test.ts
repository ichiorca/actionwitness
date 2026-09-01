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
import { PROCEED_TO_CHECKOUT, UPDATE_CART, useBuggyStoreTools } from "./tools";

let installed: InstalledDouble | null = null;
let calls: { url: string; method: string }[] = [];

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
      calls.push({ url, method });
      return handler(url, method);
    }),
  );
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
