import { render, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type InstalledDouble, installModelContextDouble } from "../test/modelContextDouble";
import { useNativeToolRegistration } from "./nativePath";
import { SPIKE_TOOL_NAME, createSpikeTool } from "./tool";

/**
 * The M0 exit gate: "the selected WebMCP path registers and cleans up one
 * read-only test tool without StrictMode duplication."
 *
 * The native control path is testable here today, so it is asserted here rather
 * than left entirely to the manual browser run. The two hook candidates cannot
 * be — neither is installed until the operator picks one — so the checklist at
 * `tests/browser/webmcp-spike-checklist.md` covers those.
 *
 * jsdom supplies no WebMCP, so these install the deterministic double from
 * §26.3 and remove it afterwards; a leaked double would make the
 * unsupported-browser tests quietly meaningless.
 */

let installed: InstalledDouble;

function Harness({ tool }: { tool: WebMCP.ModelContextTool | null }): React.ReactNode {
  const state = useNativeToolRegistration(tool);
  return <span data-testid="phase">{state.phase}</span>;
}

beforeEach(() => {
  installed = installModelContextDouble();
});

afterEach(() => {
  installed.uninstall();
});

describe("native registration lifecycle", () => {
  it("leaves exactly one tool registered after a StrictMode double-mount", async () => {
    const tool = createSpikeTool(() => undefined);

    render(
      <React.StrictMode>
        <Harness tool={tool} />
      </React.StrictMode>,
    );

    await waitFor(async () => {
      expect(await installed.modelContext.getTools()).toHaveLength(1);
    });

    // StrictMode really did run the effect twice — otherwise this test would
    // pass without exercising the condition it claims to cover.
    expect(installed.modelContext.registerCalls.length).toBeGreaterThan(1);

    const tools = await installed.modelContext.getTools();
    expect(tools.map((entry) => entry.name)).toEqual([SPIKE_TOOL_NAME]);
  });

  it("unregisters on unmount, leaving no tool behind", async () => {
    const { unmount } = render(
      <React.StrictMode>
        <Harness tool={createSpikeTool(() => undefined)} />
      </React.StrictMode>,
    );

    await waitFor(async () => {
      expect(await installed.modelContext.getTools()).toHaveLength(1);
    });

    unmount();

    expect(await installed.modelContext.getTools()).toHaveLength(0);
    expect(installed.modelContext.registerCalls.every((call) => call.aborted)).toBe(true);
  });

  it("registers the tool as read-only", async () => {
    render(
      <React.StrictMode>
        <Harness tool={createSpikeTool(() => undefined)} />
      </React.StrictMode>,
    );

    await waitFor(async () => {
      const [entry] = await installed.modelContext.getTools();
      expect(entry?.annotations?.readOnlyHint).toBe(true);
    });
  });

  it("forwards the per-invocation execution signal to the handler", async () => {
    // A path that drops the signal cannot carry proceed_to_checkout (FR-037).
    const reports: { signalPresent: boolean; aborted: boolean }[] = [];
    render(
      <React.StrictMode>
        <Harness tool={createSpikeTool((report) => reports.push(report))} />
      </React.StrictMode>,
    );

    await waitFor(async () => {
      expect(await installed.modelContext.getTools()).toHaveLength(1);
    });

    const controller = new AbortController();
    await installed.modelContext.invoke(SPIKE_TOOL_NAME, {}, controller.signal);

    expect(reports).toHaveLength(1);
    expect(reports[0]?.signalPresent).toBe(true);
    expect(reports[0]?.aborted).toBe(false);
  });

  it("reports unsupported without throwing when modelContext is absent", async () => {
    installed.uninstall();

    const { getByTestId } = render(
      <React.StrictMode>
        <Harness tool={createSpikeTool(() => undefined)} />
      </React.StrictMode>,
    );

    await waitFor(() => {
      expect(getByTestId("phase").textContent).toBe("unsupported");
    });

    installed = installModelContextDouble(); // restore for the shared teardown
  });
});