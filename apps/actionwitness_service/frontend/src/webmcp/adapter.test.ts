/**
 * 006-T5 — the local WebMCP lifecycle adapter (§25.1, §11.4, AC-09).
 *
 * The cases that carry weight:
 *
 * **StrictMode.** React deliberately mounts, unmounts and remounts effects. A
 * correct adapter therefore calls `registerTool` twice and leaves exactly one
 * live tool. Both halves are asserted, because counting only the survivors
 * would hide a leak and counting only the calls would report a false one.
 *
 * **The unsupported browser.** jsdom supplies no `document.modelContext`, which
 * makes it an honest stand-in for a browser without WebMCP — the configuration
 * the entire human UI must keep working in (AC-09).
 *
 * **The signal.** The pinned hook's `execute` takes no `AbortSignal`, which is
 * precisely why the native path exists. A test that never checked the signal
 * arrived would let that path silently regress into the hook's behaviour and
 * leave `proceed_to_checkout` unable to notice its caller had walked away.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import {
  MAX_TOOL_RESULT_CHARS,
  isWebMcpSupported,
  normalizeError,
  normalizeResult,
  useNativeTool,
  useRegisteredToolNames,
} from "./adapter";
import { type InstalledDouble, installModelContextDouble } from "../test/modelContextDouble";

let installed: InstalledDouble | null = null;

afterEach(() => {
  installed?.uninstall();
  installed = null;
});

function tool(overrides: Partial<Parameters<typeof useNativeTool>[0]> = {}) {
  return {
    name: "get_workspace_status",
    description: "Report the workspace phase and the one available next action.",
    enabled: true,
    execute: async () => ({ phase: "armed" }),
    ...overrides,
  };
}

describe("support detection", () => {
  it("reports WebMCP as unsupported when document.modelContext is absent", () => {
    expect("modelContext" in document).toBe(false);
    expect(isWebMcpSupported()).toBe(false);
  });

  it("detects support from the document, not from a user agent string", () => {
    installed = installModelContextDouble();
    expect(isWebMcpSupported()).toBe(true);
  });
});

describe("registration lifecycle", () => {
  it("registers one tool and reports it registered", async () => {
    installed = installModelContextDouble();

    const { result } = renderHook(() => useNativeTool(tool()));

    await waitFor(() => expect(result.current.phase).toBe("registered"));
    expect(installed.modelContext.toolNames).toEqual(["get_workspace_status"]);
  });

  it("leaves exactly one live tool under StrictMode's double mount", async () => {
    installed = installModelContextDouble();

    const { result } = renderHook(() => useNativeTool(tool()), { wrapper: StrictMode });

    await waitFor(() => expect(result.current.phase).toBe("registered"));
    // Two calls is correct behaviour, not a bug: StrictMode remounts.
    expect(installed.modelContext.registerCalls.length).toBeGreaterThanOrEqual(2);
    // One survivor is the property that matters.
    expect(installed.modelContext.toolNames).toEqual(["get_workspace_status"]);
  });

  it("unregisters on unmount, so a closed panel leaves no callable tool", async () => {
    installed = installModelContextDouble();

    const { result, unmount } = renderHook(() => useNativeTool(tool()));
    await waitFor(() => expect(result.current.phase).toBe("registered"));

    unmount();

    await waitFor(() => expect(installed?.modelContext.toolNames).toEqual([]));
  });

  it("registers nothing while the server says the action is unavailable", async () => {
    installed = installModelContextDouble();

    // §11.5: `enabled` follows server state. Registering a tool the server
    // would refuse offers an agent an action it cannot take.
    const { result } = renderHook(() => useNativeTool(tool({ enabled: false })));

    await waitFor(() => expect(result.current.phase).toBe("registering"));
    expect(installed.modelContext.toolNames).toEqual([]);
  });

  it("is a safe no-op in a browser without WebMCP", async () => {
    const { result } = renderHook(() => useNativeTool(tool()));

    await waitFor(() => expect(result.current.phase).toBe("unsupported"));
    // AC-09: the copy tells a person the UI still works, rather than reading
    // as a failure they need to fix.
    expect(result.current.detail).toMatch(/remains usable/i);
  });
});

describe("invocation", () => {
  it("hands the handler its own abort signal", async () => {
    installed = installModelContextDouble();
    let seen: AbortSignal | undefined;

    const { result } = renderHook(() =>
      useNativeTool(
        tool({
          execute: async (_args, context) => {
            seen = context.signal;
            return "ok";
          },
        }),
      ),
    );
    await waitFor(() => expect(result.current.phase).toBe("registered"));

    const controller = new AbortController();
    await installed.modelContext.invoke("get_workspace_status", {}, controller.signal);

    // The identity matters: a fresh signal would never fire when the caller
    // aborts, which is the whole point of the native path.
    expect(seen).toBe(controller.signal);
  });

  it("survives the pinned build's context-free invocation (ADR-0002)", async () => {
    // The real Chrome build calls execute(args) with NO context — the Tier 1
    // gate run proved an unguarded context.signal turned every native
    // invocation into an isError envelope. The handler must run, and receive
    // an undefined signal rather than a crash.
    installed = installModelContextDouble();
    let seenSignal: AbortSignal | undefined | "never-called" = "never-called";

    const { result } = renderHook(() =>
      useNativeTool(
        tool({
          execute: async (_args, context) => {
            seenSignal = context.signal;
            return "ok";
          },
        }),
      ),
    );
    await waitFor(() => expect(result.current.phase).toBe("registered"));

    const outcome = (await installed.modelContext.invokeAsPinnedBuild(
      "get_workspace_status",
    )) as { isError?: boolean };

    expect(outcome.isError).not.toBe(true);
    expect(seenSignal).toBeUndefined();
  });

  it("normalizes a thrown handler into isError rather than rejecting", async () => {
    installed = installModelContextDouble();

    const { result } = renderHook(() =>
      useNativeTool(
        tool({
          execute: async () => {
            throw new Error("the run is already verifying");
          },
        }),
      ),
    );
    await waitFor(() => expect(result.current.phase).toBe("registered"));

    // A rejection would reach an agent as a transport failure and tell it
    // nothing about what to do next.
    const outcome = await installed.modelContext.invoke("get_workspace_status");

    expect(outcome).toMatchObject({ isError: true });
    expect(JSON.stringify(outcome)).toContain("already verifying");
  });
});

describe("result normalization", () => {
  it("wraps a value as one text block", () => {
    expect(normalizeResult({ phase: "armed" })).toEqual({
      content: [{ type: "text", text: '{"phase":"armed"}' }],
    });
  });

  it("marks truncation instead of clipping silently", () => {
    const long = "x".repeat(MAX_TOOL_RESULT_CHARS * 2);

    const [block] = normalizeResult(long).content;

    expect(block?.text.length).toBe(MAX_TOOL_RESULT_CHARS);
    // A silently clipped result is worse than a short one: a reader cannot
    // tell a complete answer from half of one.
    expect(block?.text).toMatch(/truncated/);
  });

  it("respects a larger budget where §11.4 grants one", () => {
    const long = "x".repeat(5_000);

    const [block] = normalizeResult(long, 4_000).content;

    expect(block?.text.length).toBe(4_000);
  });

  it("keeps internals out of an error result", () => {
    const outcome = normalizeError(new Error("contract not selected"));

    expect(outcome.isError).toBe(true);
    expect(outcome.content[0]?.text).toBe("contract not selected");
  });

  it("says something useful when a thrown value carries no message", () => {
    const outcome = normalizeError("nope");

    expect(outcome.isError).toBe(true);
    expect(outcome.content[0]?.text).toBe("The tool call failed.");
  });
});

describe("reconciliation", () => {
  it("reports what the browser says is registered, not what we asked for", async () => {
    installed = installModelContextDouble();
    const { result } = renderHook(() => useRegisteredToolNames());

    await waitFor(() => expect(result.current).toEqual([]));

    // Something else on the origin registers a tool. FR-003 makes the browser
    // the authority, so it has to appear here without us registering it.
    await act(async () => {
      await installed?.modelContext.registerTool({
        name: "search_catalog",
        description: "Search seeded products.",
        execute: async () => ({ content: [] }),
      } as unknown as WebMCP.ModelContextTool);
    });

    await waitFor(() => expect(result.current).toEqual(["search_catalog"]));
  });

  it("reports nothing in a browser without WebMCP", async () => {
    const { result } = renderHook(() => useRegisteredToolNames());

    await waitFor(() => expect(result.current).toEqual([]));
  });
});
