/**
 * The local WebMCP lifecycle adapter — the ONLY module application code may
 * import for tool registration (spec v1.9 §11, §25.1; constitution §1).
 *
 * Everything else in this app talks to `document.modelContext` through here, so
 * the rest of the UI is testable without a browser that has WebMCP at all, and
 * so there is exactly one place to audit when the browser API changes.
 *
 * ## Two registration paths, and why both exist
 *
 * ADR-0002 pinned `use-webmcp-tool@0.2.0`. Its `execute` signature is
 * `(args) => Result` — **it forwards no per-invocation `AbortSignal`**. That is
 * not a defect in the package; it is the exact gap ADR-0002's "rule 3 split"
 * anticipated, and it decides which path each tool uses:
 *
 * - `useHarnessTool` wraps the pinned hook. Correct for tools whose work is a
 *   single request the browser can abandon harmlessly.
 * - `useNativeTool` registers directly and hands the handler its invocation
 *   signal. Required for `get_workspace_status` (§11.1 specifies native) and
 *   for anything cancellation-sensitive — `proceed_to_checkout` waits on a
 *   human, and an agent that abandons the call must be able to cancel the
 *   confirmation rather than leave it pending (FR-037, §14.9).
 *
 * ## Lifecycle
 *
 * Registration is undone by aborting the `AbortSignal` given to
 * `registerTool`, so cleanup is a property of the registration rather than a
 * separate call that can be missed. React StrictMode deliberately mounts,
 * unmounts and remounts effects: a correct adapter therefore calls
 * `registerTool` twice and leaves exactly one live tool. Both halves are
 * asserted — counting only survivors would hide a leak, counting only calls
 * would report a false one.
 *
 * ## Results
 *
 * Every result leaves here as `{ content: [{ type: "text", text }], isError? }`
 * within §11.4's character budget. A thrown handler becomes `isError: true`
 * rather than a rejected promise, because a rejection reaches an agent as a
 * transport failure and tells it nothing about what to do next.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { useWebMCP } from "use-webmcp-tool";

export const MAX_TOOL_RESULT_CHARS = 1_500;
/** §11.4's one normative exception, for `get_run_findings`. */
export const MAX_FINDINGS_RESULT_CHARS = 4_000;

const TRUNCATION_MARKER = "…[truncated]";

export interface NormalizedToolResult {
  readonly content: ReadonlyArray<{ readonly type: "text"; readonly text: string }>;
  readonly isError?: boolean;
}

export type RegistrationPhase = "unsupported" | "registering" | "registered" | "failed";

export interface RegistrationState {
  readonly phase: RegistrationPhase;
  readonly detail: string;
}

const UNSUPPORTED: RegistrationState = {
  phase: "unsupported",
  detail: "This browser has no WebMCP. The full workspace remains usable without it.",
};

/**
 * Feature detection. Type availability never proves browser support (§25.12),
 * so this asks the document rather than the user agent.
 */
export function isWebMcpSupported(): boolean {
  return typeof document !== "undefined" && document.modelContext !== undefined;
}

/**
 * Wrap a value as a bounded text result.
 *
 * Truncation is marked. A silently clipped result is worse than a short one: a
 * reader cannot tell a complete answer from half of one.
 */
export function normalizeResult(
  value: unknown,
  limit: number = MAX_TOOL_RESULT_CHARS,
): NormalizedToolResult {
  const text = typeof value === "string" ? value : JSON.stringify(value ?? null);
  const bounded =
    text.length <= limit ? text : text.slice(0, limit - TRUNCATION_MARKER.length) + TRUNCATION_MARKER;
  return { content: [{ type: "text", text: bounded }] };
}

/**
 * Wrap a failure as `isError: true` (§11.4).
 *
 * Never the raw error. §20 keeps internals out of anything an agent reads, and
 * a stack trace is both a leak and useless to the caller — so the message is
 * whatever the throwing code chose to say, and nothing more.
 */
export function normalizeError(
  error: unknown,
  limit: number = MAX_TOOL_RESULT_CHARS,
): NormalizedToolResult {
  const message =
    error instanceof Error && error.message !== "" ? error.message : "The tool call failed.";
  return { ...normalizeResult(message, limit), isError: true };
}

export interface HarnessToolDefinition<Args = Record<string, unknown>> {
  readonly name: string;
  readonly description: string;
  readonly inputSchema?: object;
  readonly annotations?: { readonly readOnlyHint?: boolean; readonly untrustedContentHint?: boolean };
  /** Server state decides this, never the browser's own idea of the phase. */
  readonly enabled: boolean;
  /** Larger budget only where §11.4 grants one. */
  readonly resultLimit?: number;
  readonly execute: (args: Args) => Promise<unknown>;
}

/**
 * Register one tool through the pinned hook, for as long as `enabled` holds.
 *
 * `enabled` comes from the caller and must be derived from **server** state:
 * FastAPI is authoritative, and a tool that decided its own availability from a
 * stale browser snapshot would offer an action the server then refuses (§11.5).
 */
export function useHarnessTool<Args = Record<string, unknown>>(
  tool: HarnessToolDefinition<Args>,
): RegistrationState {
  const limit = tool.resultLimit ?? MAX_TOOL_RESULT_CHARS;

  // Callers pass inline closures — a panel writing `execute: async () => …`
  // creates a new function every render. Holding the latest in a ref keeps this
  // handler's identity stable, so the registration below is not torn down and
  // rebuilt on every render (which is an infinite loop, not merely churn).
  const latest = useRef(tool.execute);
  latest.current = tool.execute;

  const execute = useCallback(
    async (args: Args): Promise<NormalizedToolResult> => {
      try {
        return normalizeResult(await latest.current(args), limit);
      } catch (error: unknown) {
        // Normalized rather than rethrown: a rejected promise reaches an agent
        // as a transport failure, which tells it nothing about what to do next.
        return normalizeError(error, limit);
      }
    },
    [limit],
  );

  const state = useWebMCP<Args, NormalizedToolResult>({
    name: tool.name,
    description: tool.description,
    ...(tool.inputSchema === undefined ? {} : { inputSchema: tool.inputSchema }),
    ...(tool.annotations === undefined ? {} : { annotations: tool.annotations }),
    enabled: tool.enabled,
    execute,
    // The hook would otherwise wrap our already-normalized result again.
    formatOutput: (result) => result,
  });

  return phaseOf(state);
}

function phaseOf(state: {
  supported: boolean;
  registered: boolean;
  error: Error | null;
}): RegistrationState {
  if (!state.supported) {
    return UNSUPPORTED;
  }
  if (state.error !== null) {
    return { phase: "failed", detail: state.error.message };
  }
  return state.registered
    ? { phase: "registered", detail: "Registered." }
    : { phase: "registering", detail: "Registering…" };
}

export interface NativeToolDefinition {
  readonly name: string;
  readonly description: string;
  readonly inputSchema?: object;
  readonly annotations?: { readonly readOnlyHint?: boolean; readonly untrustedContentHint?: boolean };
  readonly enabled: boolean;
  readonly resultLimit?: number;
  /**
   * Receives the invocation's own `AbortSignal`.
   *
   * This is the whole reason the native path exists: the pinned hook's
   * `execute` takes only its arguments, so a handler registered through it
   * cannot learn that its caller walked away.
   */
  readonly execute: (
    args: Record<string, unknown>,
    context: { readonly signal: AbortSignal | undefined },
  ) => Promise<unknown>;
}

/**
 * Register one tool directly, forwarding each invocation's signal.
 *
 * Used where cancellation matters. A `proceed_to_checkout` whose caller
 * disappeared must cancel its pending confirmation rather than leave a human
 * staring at a dialog nobody is waiting on (§14.9).
 */
export function useNativeTool(tool: NativeToolDefinition): RegistrationState {
  const [state, setState] = useState<RegistrationState>(UNSUPPORTED);
  const limit = tool.resultLimit ?? MAX_TOOL_RESULT_CHARS;
  const { name, description, enabled } = tool;

  // The whole definition is held in a ref. Call sites write inline closures and
  // object literals — `execute: async () => …`, `inputSchema: { … }` — so every
  // render produces fresh identities, and an effect keyed on them would tear
  // the registration down and rebuild it forever rather than merely churn.
  const latest = useRef(tool);
  latest.current = tool;

  // What the effect keys on instead: the values that actually change what is
  // registered, compared by content rather than by identity.
  const shape = JSON.stringify({
    inputSchema: tool.inputSchema ?? null,
    annotations: tool.annotations ?? null,
  });

  useEffect(() => {
    if (!enabled) {
      setState({ phase: "registering", detail: "Not available in this state." });
      return;
    }
    const modelContext = document.modelContext;
    if (modelContext === undefined) {
      setState(UNSUPPORTED);
      return;
    }

    const controller = new AbortController();
    let live = true;
    setState({ phase: "registering", detail: `Registering ${name}…` });

    void modelContext
      .registerTool(
        {
          name,
          description,
          ...(latest.current.inputSchema === undefined
            ? {}
            : { inputSchema: latest.current.inputSchema }),
          ...(latest.current.annotations === undefined
            ? {}
            : { annotations: latest.current.annotations }),
          // context is OPTIONAL at runtime: ADR-0002 recorded that the pinned
          // Chrome build's executeTool invokes handlers with no context at all
          // (no per-invocation signal), and the Tier 1 gate run proved an
          // unguarded `context.signal` crashes every native invocation there.
          // The signal is a responsiveness improvement when present, never a
          // precondition.
          execute: async (args: unknown, context?: { signal?: AbortSignal }) => {
            try {
              return normalizeResult(
                await latest.current.execute((args ?? {}) as Record<string, unknown>, {
                  signal: context?.signal,
                }),
                limit,
              );
            } catch (error: unknown) {
              return normalizeError(error, limit);
            }
          },
        } as unknown as WebMCP.ModelContextTool,
        { signal: controller.signal },
      )
      .then(
        () => {
          // Reject a completion that lost its race with unmount, or
          // StrictMode's first pass overwrites the second pass's state.
          if (live) {
            setState({ phase: "registered", detail: "Registered." });
          }
        },
        (error: unknown) => {
          if (live) {
            setState({
              phase: "failed",
              detail: error instanceof Error ? error.message : String(error),
            });
          }
        },
      );

    return () => {
      live = false;
      // Aborting *is* the unregistration, so the cleanup cannot be forgotten
      // separately from the registration it undoes.
      controller.abort();
    };
  }, [name, description, enabled, limit, shape]);

  return state;
}

/**
 * What the browser currently believes is registered (FR-003).
 *
 * Reconciled from `getTools()` and re-read on every `toolchange`, rather than
 * from what this app thinks it registered. Those two can disagree — another
 * page on the origin registers tools too, and a registration can fail after
 * the effect that started it returned — and the browser is the authority.
 */
export function useRegisteredToolNames(): readonly string[] {
  const [names, setNames] = useState<readonly string[]>([]);

  useEffect(() => {
    const modelContext = document.modelContext;
    if (modelContext === undefined) {
      setNames([]);
      return;
    }

    let live = true;
    const refresh = (): void => {
      void modelContext.getTools().then(
        (tools) => {
          // Ignore a read that resolved after unmount: it would write state
          // belonging to a page that has gone.
          if (live) {
            setNames(tools.map((tool) => tool.name));
          }
        },
        () => {
          if (live) {
            setNames([]);
          }
        },
      );
    };

    modelContext.addEventListener("toolchange", refresh);
    refresh();

    return () => {
      live = false;
      modelContext.removeEventListener("toolchange", refresh);
    };
  }, []);

  return names;
}
