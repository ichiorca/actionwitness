/**
 * Direct-native registration — the ADR-0002 control group.
 *
 * This path has no package dependency, so it works today and is the fallback if
 * neither hook forwards the per-invocation execution context (spec §25.1, LD-4).
 * Everything the two hook candidates are measured against is measured here first.
 *
 * The lifecycle rule that matters: registration is undone by aborting the
 * AbortSignal handed to `registerTool`, so cleanup is a property of the
 * registration itself rather than a separate deregister call that can be missed.
 */

import { useEffect, useState } from "react";

export type RegistrationPhase = "unsupported" | "registering" | "registered" | "failed";

export interface RegistrationState {
  readonly phase: RegistrationPhase;
  readonly detail: string;
}

const UNSUPPORTED: RegistrationState = {
  phase: "unsupported",
  detail: "document.modelContext is absent; the human UI must remain fully usable.",
};

/**
 * Register `tool` for as long as the caller is mounted.
 *
 * StrictMode double-mounts effects on purpose. That is safe here because the
 * cleanup aborts the controller, which unregisters the first registration before
 * the second is created — so two calls leave exactly one live tool.
 */
export function useNativeToolRegistration(
  tool: WebMCP.ModelContextTool | null,
): RegistrationState {
  const [state, setState] = useState<RegistrationState>(UNSUPPORTED);

  useEffect(() => {
    if (tool === null) {
      return;
    }
    const modelContext = document.modelContext;
    if (modelContext === undefined) {
      setState(UNSUPPORTED);
      return;
    }

    const controller = new AbortController();
    let cancelled = false;
    setState({ phase: "registering", detail: `Registering ${tool.name}…` });

    modelContext.registerTool(tool, { signal: controller.signal }).then(
      () => {
        // Reject a completion that lost its race with unmount, or StrictMode's
        // first pass would overwrite the second pass's state.
        if (!cancelled) {
          setState({ phase: "registered", detail: `${tool.name} registered natively.` });
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setState({
            phase: "failed",
            detail: error instanceof Error ? error.message : String(error),
          });
        }
      },
    );

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [tool]);

  return state;
}

/** Feature detection. Type availability never proves browser support (§25.12). */
export function isWebMcpAvailable(): boolean {
  return typeof document !== "undefined" && document.modelContext !== undefined;
}

/** Snapshot of what the browser currently believes is registered. */
export async function listRegisteredTools(): Promise<WebMCP.RegisteredTool[]> {
  const modelContext = document.modelContext;
  if (modelContext === undefined) {
    return [];
  }
  return modelContext.getTools();
}