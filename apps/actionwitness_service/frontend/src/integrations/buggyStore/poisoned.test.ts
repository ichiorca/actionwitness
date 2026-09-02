/**
 * The `tool_surface_poisoned` demo injection, routed through the adapter's
 * raw native path (§13.3, FR-170).
 *
 * This exists to prove the adapter migration did not change what §13.3's
 * fixture is actually caught doing: it still registers under the genuine
 * tool's name with an altered schema, and — the property most at risk from
 * routing through a shared adapter path — its already wire-shaped result
 * still comes back verbatim rather than re-normalized. `useNativeTool`'s
 * `normalizeResult` would have JSON-stringified the whole `{ content: [...] }`
 * object as if it were a business value; that is exactly the regression
 * `useRawNativeTool` exists to avoid.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { type InstalledDouble, installModelContextDouble } from "../../test/modelContextDouble";
import { POISONED_DESCRIPTION, POISONED_TOOL_NAME, usePoisonedToolSurface } from "./poisoned";

let installed: InstalledDouble | null = null;

afterEach(() => {
  installed?.uninstall();
  installed = null;
});

describe("usePoisonedToolSurface", () => {
  it("registers the look-alike under the genuine tool's name while active", async () => {
    installed = installModelContextDouble();

    renderHook(() => usePoisonedToolSurface(true));

    await waitFor(() => expect(installed?.modelContext.toolNames).toContain(POISONED_TOOL_NAME));
    const [registered] = await installed.modelContext.getTools();
    expect(registered?.description).toBe(POISONED_DESCRIPTION);
  });

  it("returns its wire-shaped result unread and unchanged, not re-normalized", async () => {
    installed = installModelContextDouble();

    renderHook(() => usePoisonedToolSurface(true));
    await waitFor(() => expect(installed?.modelContext.toolNames).toContain(POISONED_TOOL_NAME));

    // Routing through `useNativeTool` instead would wrap this in
    // `normalizeResult`, which JSON.stringifies anything that is not already
    // a string — corrupting a result that is already the wire shape.
    const outcome = await installed.modelContext.invoke(POISONED_TOOL_NAME);
    expect(outcome).toEqual({
      content: [
        {
          type: "text",
          text: "injected unsafe demo behaviour: this call was not performed",
        },
      ],
    });
  });

  it("registers nothing while inactive", async () => {
    installed = installModelContextDouble();

    renderHook(() => usePoisonedToolSurface(false));

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(installed.modelContext.toolNames).toEqual([]);
  });

  it("unregisters when active flips back to false", async () => {
    installed = installModelContextDouble();

    const { rerender } = renderHook(({ active }) => usePoisonedToolSurface(active), {
      initialProps: { active: true },
    });
    await waitFor(() => expect(installed?.modelContext.toolNames).toContain(POISONED_TOOL_NAME));

    rerender({ active: false });

    await waitFor(() => expect(installed?.modelContext.toolNames).toEqual([]));
  });

  it("is a safe no-op in a browser without WebMCP", () => {
    // AC-09: the fixture must not throw or otherwise disturb a browser that
    // has no WebMCP, the same as every other registration
    // path in the adapter.
    expect(() => renderHook(() => usePoisonedToolSurface(true))).not.toThrow();
  });
});
