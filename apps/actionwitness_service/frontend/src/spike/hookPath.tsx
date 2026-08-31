/**
 * The two ADR-0002 hook candidates, loaded at runtime rather than pinned.
 *
 * Neither `use-webmcp-tool` nor `usewebmcp` is a dependency of this package, and
 * that is deliberate: pinning one is the decision the spike exists to make. They
 * are imported through a runtime specifier that Vite is told not to resolve, so
 * the harness builds and ships with neither installed and reports each as
 * "not installed" until the operator adds one.
 *
 * The operator installs a candidate, reloads, and compares it against the native
 * control. See `tests/browser/webmcp-spike-checklist.md`.
 */

import { Component, type ReactNode, useEffect, useState } from "react";

export const HOOK_CANDIDATES = ["use-webmcp-tool", "usewebmcp"] as const;
export type HookCandidate = (typeof HOOK_CANDIDATES)[number];

export type HookLoadState =
  | { readonly status: "loading" }
  | { readonly status: "missing"; readonly detail: string }
  | { readonly status: "unexpected"; readonly detail: string; readonly exports: string[] }
  | { readonly status: "ready"; readonly hook: UnknownHook; readonly exportName: string };

/**
 * The candidates' signatures are not known until one is installed — that is part
 * of what the spike measures. The call site below is the ONE place to adjust if
 * the installed package wants different arguments.
 */
export type UnknownHook = (...args: unknown[]) => unknown;

const HOOK_EXPORT_NAMES = ["useWebMcpTool", "useWebMCPTool", "useWebmcpTool", "default"];

function pickHook(module: Record<string, unknown>): { hook: UnknownHook; name: string } | null {
  for (const name of HOOK_EXPORT_NAMES) {
    const candidate = module[name];
    if (typeof candidate === "function") {
      return { hook: candidate as UnknownHook, name };
    }
  }
  return null;
}

/** Load a candidate without making it a build-time dependency. */
export function useHookCandidate(specifier: HookCandidate): HookLoadState {
  const [state, setState] = useState<HookLoadState>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });

    import(/* @vite-ignore */ specifier).then(
      (module: Record<string, unknown>) => {
        if (cancelled) {
          return;
        }
        const picked = pickHook(module);
        if (picked === null) {
          setState({
            status: "unexpected",
            detail: `${specifier} exports no recognised hook. Adjust HOOK_EXPORT_NAMES.`,
            exports: Object.keys(module),
          });
          return;
        }
        setState({ status: "ready", hook: picked.hook, exportName: picked.name });
      },
      (error: unknown) => {
        if (cancelled) {
          return;
        }
        setState({
          status: "missing",
          detail:
            `${specifier} is not installed. Run \`npm install ${specifier}\` in ` +
            `apps/actionwitness_service/frontend and reload. ` +
            `(${error instanceof Error ? error.message : String(error)})`,
        });
      },
    );

    return () => {
      cancelled = true;
    };
  }, [specifier]);

  return state;
}

interface HookProbeProps {
  readonly hook: UnknownHook;
  readonly tool: WebMCP.ModelContextTool;
  readonly onError: (message: string) => void;
}

/**
 * Calls the loaded hook unconditionally, so the rules of hooks hold.
 *
 * Switching candidates remounts this component under a new `key`, which is what
 * makes a runtime hook swap legal at all — and it doubles as the mount/unmount
 * cleanup case the checklist asks the operator to watch.
 */
function HookProbeInner({ hook, tool }: Omit<HookProbeProps, "onError">): ReactNode {
  // ── The one adjustment point if the installed candidate wants a different
  //    call shape. Record whatever you change here in ADR-0002. ──────────────
  hook(tool);
  return <p>Hook invoked for {tool.name}. Compare DevTools against the native path.</p>;
}

interface BoundaryProps {
  readonly children: ReactNode;
  readonly onError: (message: string) => void;
}

/**
 * A hook whose signature does not match throws during render. Without a boundary
 * that takes down the whole harness, losing the comparison the spike is for.
 */
export class HookErrorBoundary extends Component<BoundaryProps, { failed: boolean }> {
  override state = { failed: false };

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: Error): void {
    this.props.onError(error.message);
  }

  override render(): ReactNode {
    if (this.state.failed) {
      return <p role="alert">The hook threw during render — see the log below.</p>;
    }
    return this.props.children;
  }
}

export function HookProbe({ hook, tool, onError }: HookProbeProps): ReactNode {
  return (
    <HookErrorBoundary onError={onError}>
      <HookProbeInner hook={hook} tool={tool} />
    </HookErrorBoundary>
  );
}