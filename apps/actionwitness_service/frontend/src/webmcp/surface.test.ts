/**
 * 014-T2 — capturing the tool surface from the browser (FR-166, FR-167).
 *
 * The capture side is deliberately thin, so these tests are mostly about what
 * it refuses to do: it computes no hash, assigns no namespace, and infers
 * nothing from what this app thinks it registered. Each of those would move a
 * decision the server must own onto the least trustworthy side of the boundary.
 *
 * The lifecycle half matters as much. StrictMode mounts twice, `toolchange`
 * fires per change without coalescing (ADR-0002), and a run ends while a
 * capture is in flight — all three produce evidence, and evidence that arrived
 * from a page that has gone is worse than none.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// `describeTool` and `readSurface` moved to the adapter in 012-T6: the
// product has one `getTools()` call, shared by the capture and the
// registration view, so the two cannot disagree about what is registered.
import { describeTool, readSurface } from "./adapter";
import { TOOLCHANGE_QUIET_PERIOD_MS, useToolSurfaceWitness } from "./surface";
import { type InstalledDouble, installModelContextDouble } from "../test/modelContextDouble";

let installed: InstalledDouble | null = null;
let posted: { url: string; body: unknown }[] = [];

beforeEach(() => {
  posted = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      // `RequestInit["body"]` is `BodyInit | null`, most of which stringifies to
      // "[object Object]". The client under test sends JSON strings; anything
      // else is a defect worth failing on rather than silently recording.
      const raw = init?.body;
      if (typeof raw !== "string") {
        throw new TypeError(`the capture client must send a JSON string; got ${typeof raw}`);
      }
      posted.push({ url, body: JSON.parse(raw) as unknown });
      return new Response(JSON.stringify({ surface_hash: "sha256:x", deltas: [] }), {
        status: 201,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
});

afterEach(() => {
  installed?.uninstall();
  installed = null;
  vi.unstubAllGlobals();
});

async function register(name: string, extra: Record<string, unknown> = {}): Promise<void> {
  await installed?.modelContext.registerTool({
    name,
    description: "a tool",
    inputSchema: { type: "object" },
    execute: async () => ({ ok: true }),
    ...extra,
  } as never);
}

// --- narrowing an untrusted descriptor ---------------------------------------

describe("describeTool", () => {
  it("drops a descriptor with no usable name", () => {
    // A tool the server cannot name is one it cannot compare against a
    // baseline, and inventing a name would invent a delta.
    expect(describeTool({ description: "nameless" })).toBeNull();
    expect(describeTool({ name: "" })).toBeNull();
    expect(describeTool(null)).toBeNull();
    expect(describeTool("apply_discount")).toBeNull();
  });

  it("keeps an absent hint absent rather than defaulting it to false", () => {
    // A tool that stopped *declaring* itself read-only changed its hints.
    expect(describeTool({ name: "a" })?.read_only_hint).toBeNull();
    expect(describeTool({ name: "a", annotations: {} })?.read_only_hint).toBeNull();
  });

  it("reads both hints from `annotations`, where getTools() puts them", () => {
    // `webmcp-types` nests them inside `RegisteredTool.annotations`. Reading the
    // top level instead — which is what this did — made every captured hint
    // `null`, and so made `hint_change` a delta kind no run could ever produce
    // while `one_mug_stable_surface` listed it among the kinds that fail a run.
    const captured = describeTool({
      name: "a",
      annotations: { readOnlyHint: false, untrustedContentHint: true },
    });

    expect(captured?.read_only_hint).toBe(false);
    expect(captured?.untrusted_content_hint).toBe(true);
  });

  it("still reads a flattened hint, because the registry is untrusted either way", () => {
    // Both readings come from the same place and carry the same trust — none —
    // so tolerating the older layout costs nothing and keeps the capture
    // working against a browser that reports it.
    expect(describeTool({ name: "a", readOnlyHint: true })?.read_only_hint).toBe(true);
    // The declared shape wins when both are present.
    expect(
      describeTool({ name: "a", readOnlyHint: true, annotations: { readOnlyHint: false } })
        ?.read_only_hint,
    ).toBe(false);
  });

  it("ignores a non-boolean hint rather than coercing it", () => {
    expect(describeTool({ name: "a", annotations: { readOnlyHint: "yes" } })?.read_only_hint)
      .toBeNull();
    expect(describeTool({ name: "a", annotations: "not a record" })?.read_only_hint).toBeNull();
  });

  it("submits no hash and no namespace", () => {
    // Both are the server's to compute; supplying either here would be the
    // tool surface vouching for its own integrity.
    const captured = describeTool({ name: "a", identityHash: "sha256:evil", namespace: "harness" });

    expect(captured).not.toBeNull();
    expect(Object.keys(captured as object).sort()).toEqual([
      "description",
      "input_schema",
      "name",
      "read_only_hint",
      "untrusted_content_hint",
    ]);
  });

  it("ignores a non-object input schema rather than forwarding it", () => {
    expect(describeTool({ name: "a", inputSchema: "not a schema" })?.input_schema).toEqual({});
  });
});

// --- reading the surface -----------------------------------------------------

describe("readSurface", () => {
  it("returns null when the browser has no WebMCP", async () => {
    await expect(readSurface()).resolves.toBeNull();
  });

  it("carries a declared hint through to the captured surface", async () => {
    // The end-to-end half of the fix above: `registerTool` takes `annotations`,
    // `getTools()` reports them nested, and the capture has to survive both.
    installed = installModelContextDouble();
    await register("search_catalog", { annotations: { readOnlyHint: true } });

    const surface = await readSurface();

    expect(surface?.[0]?.read_only_hint).toBe(true);
  });

  it("reads from getTools rather than from what this app registered", async () => {
    // FR-167: "Reconciliation shall use getTools() as the authority and never
    // infer the surface from component mount state." Another script on the
    // origin registers tools this app never mounted, which is the attack.
    installed = installModelContextDouble();
    await register("apply_discount");
    await register("look_alike");

    const surface = await readSurface();

    expect(surface?.map((tool) => tool.name).sort()).toEqual(["apply_discount", "look_alike"]);
  });
});

// --- the witness lifecycle ---------------------------------------------------

describe("useToolSurfaceWitness", () => {
  it("captures nothing before a run exists", () => {
    installed = installModelContextDouble();
    renderHook(() => useToolSurfaceWitness(null));

    expect(posted).toEqual([]);
  });

  it("captures nothing when the browser has no WebMCP", () => {
    // The run then has no baseline, and §16.1 makes that an explicit non-pass
    // at verification rather than a silent gap here.
    renderHook(() => useToolSurfaceWitness("run_1"));

    expect(posted).toEqual([]);
  });

  it("captures once at arming, to the run's own route", async () => {
    installed = installModelContextDouble();
    await register("apply_discount");

    renderHook(() => useToolSurfaceWitness("run_1"));

    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]?.url).toBe("/api/v1/runs/run_1/tool-surface");
    expect(posted[0]?.body).toEqual({
      tools: [
        {
          name: "apply_discount",
          description: "a tool",
          read_only_hint: null,
          untrusted_content_hint: null,
          input_schema: { type: "object" },
        },
      ],
    });
  });

  it("re-captures after a toolchange", async () => {
    installed = installModelContextDouble();
    await register("apply_discount");
    renderHook(() => useToolSurfaceWitness("run_1"));
    await waitFor(() => expect(posted).toHaveLength(1));

    await act(async () => {
      await register("look_alike");
    });

    await waitFor(() => expect(posted).toHaveLength(2), { timeout: 2000 });
    const second = posted[1]?.body as { tools: { name: string }[] };
    expect(second.tools.map((tool) => tool.name).sort()).toEqual([
      "apply_discount",
      "look_alike",
    ]);
  });

  it("coalesces a burst into one re-capture rather than one per firing", async () => {
    // ADR-0002 recorded that `toolchange` fires per change and does not
    // coalesce. Without a quiet period a busy registration sequence posts a
    // dozen near-identical captures; with one, the read that follows sees the
    // settled surface, which is the one that matters.
    installed = installModelContextDouble();
    renderHook(() => useToolSurfaceWitness("run_1"));
    await waitFor(() => expect(posted).toHaveLength(1));

    await act(async () => {
      await register("a");
      await register("b");
      await register("c");
    });

    await waitFor(() => expect(posted).toHaveLength(2), { timeout: 2000 });
    const settled = posted[1]?.body as { tools: { name: string }[] };
    expect(settled.tools.map((tool) => tool.name).sort()).toEqual(["a", "b", "c"]);
  });

  it("stops capturing once unmounted", async () => {
    installed = installModelContextDouble();
    const { unmount } = renderHook(() => useToolSurfaceWitness("run_1"));
    await waitFor(() => expect(posted).toHaveLength(1));

    unmount();
    await act(async () => {
      await register("after_unmount");
    });
    await new Promise((resolve) => setTimeout(resolve, 400));

    expect(posted).toHaveLength(1);
  });

  it("survives StrictMode's double mount without double-capturing per change", async () => {
    installed = installModelContextDouble();
    renderHook(() => useToolSurfaceWitness("run_1"), { wrapper: StrictMode });

    await waitFor(() => expect(posted.length).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 400));

    // The in-flight guard collapses the double mount's overlapping reads.
    // Overlapping posts would append events whose order does not match the
    // order the surfaces were read in, and the baseline is whichever landed
    // first.
    expect(posted.length).toBeLessThanOrEqual(2);
  });

  it("re-captures a change that arrived while a capture was posting", async () => {
    // The defect this replaces: the in-flight guard *returned* on a change that
    // landed during a post, and scheduled nothing afterwards — so the change
    // was dropped, not deferred. The demo's own look-alike registers in the
    // same commit that arms the run, which puts its `toolchange` squarely
    // inside the baseline's POST, and a run whose injection went unrecorded
    // passes `stable_tool_surface`.
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const raw = init?.body;
        // Narrowed rather than stringified, for the reason the shared stub
        // gives: most of `BodyInit` stringifies to "[object Object]", which
        // would record a capture nobody sent.
        if (typeof raw !== "string") {
          throw new TypeError(`the capture client must send a JSON string; got ${typeof raw}`);
        }
        posted.push({ url, body: JSON.parse(raw) as unknown });
        // Only the first post is held open, so the change below has to arrive
        // while a capture is genuinely in flight.
        if (++call === 1) {
          await held;
        }
        return new Response("{}", { status: 201 });
      }),
    );

    installed = installModelContextDouble();
    await register("apply_discount");
    renderHook(() => useToolSurfaceWitness("run_1"));
    await waitFor(() => expect(posted).toHaveLength(1));

    // Arrive mid-post, then let the baseline finish.
    await act(async () => {
      await register("look_alike");
    });
    await new Promise((resolve) => setTimeout(resolve, TOOLCHANGE_QUIET_PERIOD_MS + 50));
    await act(async () => {
      release?.();
      await held;
    });

    await waitFor(() => expect(posted).toHaveLength(2), { timeout: 2000 });
    const caught = posted[1]?.body as { tools: { name: string }[] };
    expect(caught.tools.map((tool) => tool.name).sort()).toEqual([
      "apply_discount",
      "look_alike",
    ]);
  });

  it("flushes a debounced capture on demand, before anything seals the run", async () => {
    // Verification seals the timeline and the witness is debounced, so without
    // this a delta read a moment before `verify_outcome` is posted a moment
    // after it and is judged by nothing.
    installed = installModelContextDouble();
    const { result } = renderHook(() => useToolSurfaceWitness("run_1"));
    await waitFor(() => expect(posted).toHaveLength(1));

    await act(async () => {
      await register("look_alike");
      // No waiting out the quiet period: the caller is about to verify.
      await result.current.flush();
    });

    expect(posted).toHaveLength(2);
    const flushed = posted[1]?.body as { tools: { name: string }[] };
    expect(flushed.tools.map((tool) => tool.name)).toEqual(["look_alike"]);
  });

  it("costs nothing to flush a surface that has not moved", async () => {
    // A flush runs before every verification, and a request per verdict spent
    // re-reading an unchanged surface is a request the page's own polling does
    // not get to make — FR-009's budget is shared across the whole client.
    installed = installModelContextDouble();
    await register("apply_discount");
    const { result } = renderHook(() => useToolSurfaceWitness("run_1"));
    await waitFor(() => expect(posted).toHaveLength(1));

    await act(async () => {
      await result.current.flush();
      await result.current.flush();
    });

    expect(posted).toHaveLength(1);
  });

  it("resolves a flush when there is no run and no browser support", async () => {
    // "There is no evidence to flush" and "the evidence is flushed" are the same
    // postcondition, so neither case may hang the caller that awaits it.
    const withoutRun = renderHook(() => useToolSurfaceWitness(null));
    await expect(withoutRun.result.current.flush()).resolves.toBeUndefined();

    const withoutWebMcp = renderHook(() => useToolSurfaceWitness("run_1"));
    await expect(withoutWebMcp.result.current.flush()).resolves.toBeUndefined();
    expect(posted).toEqual([]);
  });

  it("does not fail the page when a capture is rejected", async () => {
    // A failed capture is not a failed run: the absence of a baseline is
    // already an explicit non-pass at verification (§16.1), and throwing here
    // would take down the workspace UI for an evidence-collection problem.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("nope", { status: 500 })),
    );
    installed = installModelContextDouble();

    expect(() => renderHook(() => useToolSurfaceWitness("run_1"))).not.toThrow();
    await new Promise((resolve) => setTimeout(resolve, 200));
  });
});
