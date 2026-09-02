/**
 * The workspace's top-level error boundary (FR-124).
 *
 * `main.tsx` is the only place this wraps `<App />`: without a boundary here,
 * a render error anywhere in the workspace tree unmounts React back to an
 * empty `<div id="root">`, leaving a person looking at a blank page with no
 * explanation and no way back short of already knowing to reload. FR-124
 * requires visible recovery guidance for exactly that case, and "blank" is
 * not guidance.
 *
 * The error is logged rather than swallowed. `console.error` is what the
 * release checklist's console check is there to catch, and a boundary that
 * hid the failure from the console would defeat that check at the same
 * moment it hides the failure from the person looking at the fallback.
 *
 * `src/spike/hookPath.tsx`'s `HookErrorBoundary` is a narrower boundary
 * scoped to the developer spike, which the Dockerfile strips from the
 * production bundle. This is the product's only top-level boundary.
 */

import { Component, type ReactNode } from "react";

export interface WorkspaceErrorBoundaryProps {
  readonly children: ReactNode;
}

interface WorkspaceErrorBoundaryState {
  readonly failed: boolean;
}

export class WorkspaceErrorBoundary extends Component<
  WorkspaceErrorBoundaryProps,
  WorkspaceErrorBoundaryState
> {
  override state: WorkspaceErrorBoundaryState = { failed: false };

  static getDerivedStateFromError(): WorkspaceErrorBoundaryState {
    return { failed: true };
  }

  override componentDidCatch(error: Error): void {
    // Never swallowed: a person staring at the fallback below deserves the
    // same evidence a developer reading the console would get from an
    // unhandled render error.
    console.error("ActionWitness workspace crashed:", error);
  }

  override render(): ReactNode {
    if (!this.state.failed) {
      return this.props.children;
    }
    return (
      <main role="alert" className="workspace-crash">
        <h1>Something went wrong</h1>
        <p>
          The workspace hit an unexpected error and stopped rendering. Nothing you did caused
          this, and reloading is safe: no protected action can complete without your
          confirmation, so nothing you had not yet approved was taken while this happened.
        </p>
        <button type="button" onClick={() => { window.location.reload(); }}>
          Reload
        </button>
      </main>
    );
  }
}
