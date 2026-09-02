/**
 * The top-level error boundary (FR-124).
 *
 * The property worth testing: a render error below `<App />` must stop at
 * this boundary rather than propagating past `main.tsx` and unmounting the
 * whole tree to a blank `<div id="root">`, and the fallback it shows must be
 * reachable rather than merely present in the DOM.
 */

import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkspaceErrorBoundary } from "./WorkspaceErrorBoundary";

function Bomb(): React.ReactElement {
  throw new Error("deliberate render failure");
}

describe("WorkspaceErrorBoundary (FR-124)", () => {
  let consoleError: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // React itself logs the error too; spying rather than silencing keeps
    // that intact while letting the test assert on ours specifically.
    consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it("renders the child tree when nothing has thrown", () => {
    render(
      <WorkspaceErrorBoundary>
        <p>ordinary workspace content</p>
      </WorkspaceErrorBoundary>,
    );

    expect(screen.getByText("ordinary workspace content")).toBeDefined();
  });

  it("renders recovery guidance instead of propagating a child's render error", () => {
    render(
      <WorkspaceErrorBoundary>
        <Bomb />
      </WorkspaceErrorBoundary>,
    );

    // A person is told something broke and given a way back — not a blank
    // page, and not silence.
    expect(screen.getByRole("alert")).toBeDefined();
    expect(screen.getByText(/something went wrong/i)).toBeDefined();
    expect(screen.getByRole("button", { name: /reload/i })).toBeDefined();
  });

  it("logs the caught error rather than swallowing it", () => {
    render(
      <WorkspaceErrorBoundary>
        <Bomb />
      </WorkspaceErrorBoundary>,
    );

    // Never silent: the release checklist's console check depends on this
    // reaching the console, same as any other unhandled render error would.
    expect(consoleError).toHaveBeenCalledWith(
      "ActionWitness workspace crashed:",
      expect.objectContaining({ message: "deliberate render failure" }),
    );
  });
});
