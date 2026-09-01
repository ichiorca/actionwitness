/**
 * 006-T10 — paged polling by event sequence (§15.3).
 *
 * The three bugs this hook exists to not have, each of which ships easily:
 *
 * - **Stopping on an empty page.** `has_more: false` means "nothing more right
 *   now", not "the run ended". A client that stopped there would miss the
 *   events a failing run is judged by — and would look correct in every test
 *   written against a finished run.
 * - **Rendering a stale response.** A slow page landing after a fast one would
 *   rewind the timeline in front of the user.
 * - **Setting state after unmount.** A poll whose component has gone must not
 *   write, and must not keep running.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useRunTimeline } from "./useRunTimeline";

interface FakeEvent {
  readonly sequence: number;
  readonly type: string;
}

/**
 * A cursor-aware stand-in for the events endpoint.
 *
 * It filters by `after_sequence` the way the real one does, rather than
 * replaying a fixed page. That matters: a fixture that re-served the same page
 * would make a hook with a broken cursor look like it was collecting events,
 * and the duplicate-delivery test below would pass for the wrong reason.
 */
let timeline: FakeEvent[] = [];
let runStatus = "running";
let served = 0;
let failing = false;

function serve(): void {
  served = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      served += 1;
      if (failing) {
        return new Response("nope", { status: 500 });
      }
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const after = Number(/after_sequence=(\d+)/.exec(url)?.[1] ?? "0");
      const events = timeline.filter((event) => event.sequence > after);
      return new Response(
        JSON.stringify({
          run_id: "run_1",
          run_status: runStatus,
          events: events.map((event) => ({
            id: `evt_${String(event.sequence)}`,
            sequence_number: event.sequence,
            event_type: event.type,
            actor: "agent",
            tool_name: "update_cart",
            status: null,
            reported_status: "success",
            created_at: "2026-01-01T00:00:00+00:00",
          })),
          next_after_sequence: events.length === 0 ? after : events[events.length - 1]!.sequence,
          has_more: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }),
  );
}

beforeEach(() => {
  timeline = [];
  runStatus = "running";
  failing = false;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("polling", () => {
  it("collects events as they appear, using the cursor", async () => {
    timeline = [{ sequence: 1, type: "run_armed" }];
    serve();

    const { result } = renderHook(() => useRunTimeline("run_1", 5));
    await waitFor(() => expect(result.current.events.length).toBe(1));

    // The run continues; the next poll must pick up only what is new.
    timeline = [...timeline, { sequence: 2, type: "tool_invocation_started" }];

    await waitFor(() => expect(result.current.events.length).toBe(2));
    expect(result.current.events.map((event) => event.sequenceNumber)).toEqual([1, 2]);
  });

  it("keeps polling after an empty page while the run is live", async () => {
    // The bug this is for: `has_more: false` on a running run means "nothing
    // yet", and a client that stopped would miss everything that followed.
    serve();
    const { result } = renderHook(() => useRunTimeline("run_1", 5));
    await waitFor(() => expect(served).toBeGreaterThan(1));
    expect(result.current.events).toEqual([]);

    timeline = [{ sequence: 1, type: "tool_invocation_completed" }];

    await waitFor(() => expect(result.current.events.length).toBe(1));
    expect(result.current.polling).toBe(true);
  });

  it("stops once the run reaches a terminal state", async () => {
    timeline = [{ sequence: 1, type: "verification_completed" }];
    runStatus = "failed";
    serve();

    const { result } = renderHook(() => useRunTimeline("run_1", 5));
    await waitFor(() => expect(result.current.polling).toBe(false));
    expect(result.current.runStatus).toBe("failed");

    // And it really stops: no further requests after the terminal page.
    const after = served;
    await new Promise((resolve) => setTimeout(resolve, 40));
    expect(served).toBe(after);
  });

  it("does not deliver an event twice", async () => {
    // A cursor that failed to advance would redeliver the same page forever,
    // and the timeline would fill with duplicates rather than obviously break.
    timeline = [
      { sequence: 1, type: "run_armed" },
      { sequence: 2, type: "tool_invocation_started" },
    ];
    serve();

    const { result } = renderHook(() => useRunTimeline("run_1", 5));
    await waitFor(() => expect(result.current.events.length).toBe(2));
    await new Promise((resolve) => setTimeout(resolve, 30));

    const ids = result.current.events.map((event) => event.id);
    expect(new Set(ids).size).toBe(ids.length);
    expect(ids.length).toBe(2);
  });

  it("stops and writes nothing after unmount", async () => {
    serve();
    const { unmount } = renderHook(() => useRunTimeline("run_1", 5));
    await waitFor(() => expect(served).toBeGreaterThan(0));

    unmount();
    const after = served;
    await new Promise((resolve) => setTimeout(resolve, 40));

    // A poll that outlived its component would keep the connection alive on a
    // page nobody is looking at.
    expect(served).toBe(after);
  });

  it("polls nothing without a run", async () => {
    serve();

    const { result } = renderHook(() => useRunTimeline(null, 5));

    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(served).toBe(0);
    expect(result.current.polling).toBe(false);
  });

  it("resets its cursor when the run changes", async () => {
    timeline = [{ sequence: 1, type: "run_armed" }];
    runStatus = "passed";
    serve();

    const { result, rerender } = renderHook(({ id }: { id: string }) => useRunTimeline(id, 5), {
      initialProps: { id: "run_1" },
    });
    await waitFor(() => expect(result.current.events.length).toBe(1));

    rerender({ id: "run_2" });

    // A cursor carried across runs would silently skip the beginning of the
    // second one, which is exactly where arming lives.
    await waitFor(() => expect(result.current.events.length).toBe(1));
    expect(result.current.events[0]?.sequenceNumber).toBe(1);
  });

  it("surfaces a read failure without discarding what it already has", async () => {
    timeline = [{ sequence: 1, type: "run_armed" }];
    serve();
    const { result } = renderHook(() => useRunTimeline("run_1", 5));
    await waitFor(() => expect(result.current.events.length).toBe(1));

    failing = true;

    await waitFor(() => expect(result.current.error).not.toBeNull());
    // The timeline a person was reading does not vanish because one poll
    // failed — losing it would be a worse outcome than the failure.
    expect(result.current.events.length).toBe(1);
  });
});
