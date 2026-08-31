/**
 * The one tool the ADR-0002 spike registers.
 *
 * Read-only by design. The spike measures registration lifecycle, not mutation:
 * a tool that changed state would make a failed cleanup destructive, and the M0
 * exit gate asks specifically for "one read-only test tool".
 */

export const SPIKE_TOOL_NAME = "get_workspace_status";

export interface SpikeStatus {
  readonly workspaceId: string;
  readonly phase: string;
  readonly nextAction: string;
}

/** Stub payload. The real tool projects server guidance state (FR-121). */
export function readSpikeStatus(): SpikeStatus {
  return {
    workspaceId: "spike-workspace",
    phase: "contract_ready",
    nextAction: "select_contract",
  };
}

/**
 * Build the tool definition. `execute` receives the per-invocation
 * `{ signal }`; the spike reports whether it arrives, because a hook that drops
 * it cannot be used for cancellation-sensitive tools (FR-037, LD-4) and that is
 * one of the two questions ADR-0002 has to answer.
 */
export function createSpikeTool(
  onInvocation: (report: { signalPresent: boolean; aborted: boolean }) => void,
): WebMCP.ModelContextTool {
  return {
    name: SPIKE_TOOL_NAME,
    title: "Get workspace status",
    description:
      "Report the ActionWitness workspace phase and the single permitted next action.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    execute: (_input, options) => {
      const signal: AbortSignal | undefined = options?.signal;
      onInvocation({
        signalPresent: signal instanceof AbortSignal,
        aborted: signal?.aborted ?? false,
      });
      return readSpikeStatus();
    },
  };
}
