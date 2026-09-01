/**
 * The hook-registered harness tools (§11.1, §11.5, AC-22).
 *
 * ## Enablement comes from the server's phase, never from a local guess
 *
 * §11.5 makes the browser-visible tool set change with workspace state, and
 * FR-120 makes FastAPI the authority on what that state is. So every `enabled`
 * below is a lookup against `guidance.phase` — a value the server derived —
 * rather than a condition this file evaluates over run fields.
 *
 * The difference matters when they disagree. A tool that decided its own
 * availability would offer an agent an action the server then refuses, and the
 * agent would have no way to tell a bug from a race. **Enablement is a hint;
 * the server is still the authority**, and every handler here goes through the
 * recorded API where the real check lives. A tool that is enabled and then
 * refused is correct behaviour, not a contradiction.
 *
 * ## What is deliberately absent
 *
 * `create_outcome_contract` (declarative form), `create_regression_eval` and
 * `run_regression_eval` (eval generation and replay), and `propose_assertions`
 * (proposal mode) belong to later milestones. They are named here so their
 * absence reads as a decision rather than an oversight — AC-22 measures "every
 * capability in the §11.1 table is reachable by tool" against that table, and a
 * silent gap would be the wrong kind of surprise at the Tier 1 gate.
 */

import { request } from "../api/client";
import {
  type WorkspaceStatus,
  parseFindings,
  parseRun,
  parseWorkspace,
} from "../api/workspace";
import {
  MAX_FINDINGS_RESULT_CHARS,
  type RegistrationState,
  useHarnessTool,
} from "../webmcp/adapter";

/** §11.5's phases, as the server spells them. */
const TERMINAL_PHASES = ["passed", "passed_with_warnings", "failed", "error", "cancelled"];

/** FR-080: only a failed or warning-bearing run can produce a case. A passing
 *  run has no failure to reproduce, and offering the tool there would invite an
 *  agent to ask for something the server refuses. */
const EVAL_ELIGIBLE_PHASES = ["failed", "passed_with_warnings"];

/** §15.4's path segment, named once so the routes below read as data. */
const EVAL_SEGMENT = "evals";

function isTerminal(phase: string): boolean {
  return TERMINAL_PHASES.includes(phase);
}

export interface HarnessToolset {
  readonly states: Readonly<Record<string, RegistrationState>>;
}

/**
 * Register the Tier 1 harness tools for this workspace.
 *
 * One hook per tool, called unconditionally — the rules of hooks do not bend
 * for `enabled`, which is why each definition carries its own flag rather than
 * the caller skipping a registration.
 */
export function useHarnessToolset(
  status: WorkspaceStatus | null,
  refresh: () => Promise<void>,
  evalCaseId: string | null = null,
): HarnessToolset {
  // Before the first load there is no authoritative state. Defaulting to a
  // phase would publish acting tools against a workspace nobody has read yet —
  // `reset_workspace` in particular, whose default-phase reading is "enabled".
  // `loaded` is what keeps a guess from becoming an offer.
  const loaded = status !== null;
  const phase = status?.guidance.phase ?? "";
  const runId = status?.activeRun?.runId ?? null;
  const hasContract = loaded && status.selectedContractId !== null;

  const listTemplates = useHarnessTool({
    name: "list_contract_templates",
    description: "List the built-in outcome contracts with their ids and short descriptions.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    enabled: true,
    execute: async () => {
      const body = await request("/contracts/templates", { parse: (value) => value });
      return body;
    },
  });

  const getContract = useHarnessTool({
    name: "get_outcome_contract",
    description:
      "Return the active outcome contract so a visiting agent can learn what is expected of it.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    // §11.1: "a contract is selected and marked publishable". Publishability is
    // the server's judgement, so an unpublishable contract is refused there
    // with CONTRACT_NOT_PUBLISHABLE rather than guessed at here.
    enabled: hasContract,
    execute: async () => {
      if (status?.selectedContractId == null) {
        throw new Error("No contract is selected.");
      }
      return await request(`/contracts/${status.selectedContractId}/published`, {
        parse: (value) => value,
      });
    },
  });

  const armContract = useHarnessTool({
    name: "arm_outcome_contract",
    description:
      "Capture the initial authoritative state and create a run for the active contract. " +
      "Optionally bind an eligible completed run as the matched-comparison source.",
    inputSchema: {
      type: "object",
      properties: {
        comparison_source_run_id: {
          type: "string",
          minLength: 1,
          maxLength: 128,
          description:
            "A terminal run in this workspace to compare this run against (§15.3 matched pre/post pair).",
        },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    // §11.1: "valid contract selected and no run active".
    enabled: loaded && phase === "contract_ready",
    execute: async (args: Record<string, unknown>) => {
      const source = args["comparison_source_run_id"];
      const armed = await request("/runs", {
        method: "POST",
        body: typeof source === "string" ? { comparison_source_run_id: source } : {},
        parse: (value) => value,
      });
      await refresh();
      return armed;
    },
  });

  const verifyOutcome = useHarnessTool({
    name: "verify_outcome",
    description: "Capture the final authoritative state and evaluate the armed contract.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: false },
    // §11.1 also requires "no in-flight invocation or confirmation". An
    // in-flight invocation is not visible in the phase, so the server's
    // PRECONDITION_FAILED is what enforces it — this flag keeps the tool out of
    // the agent's way in the states where it is obviously wrong.
    enabled: loaded && phase === "running",
    execute: async () => {
      if (runId === null) {
        throw new Error("No run is active.");
      }
      const verdict = await request(`/runs/${runId}/verify`, {
        method: "POST",
        parse: (value) => value,
      });
      await refresh();
      return verdict;
    },
  });

  const getFindings = useHarnessTool({
    name: "get_run_findings",
    description:
      "Return bounded, structured findings for a completed run, with the untruncated total.",
    inputSchema: {
      type: "object",
      properties: {
        limit: {
          type: "integer",
          minimum: 1,
          maximum: 10,
          description: "How many findings to return. Defaults to 3.",
        },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    // §11.1: "a run has reached any terminal state, including error and
    // cancelled" — a failed run is exactly when an agent needs to read findings.
    enabled: loaded && isTerminal(phase) && runId !== null,
    // §11.4's one normative exception: a finding an agent cannot read is
    // equivalent to a finding that was never produced.
    resultLimit: MAX_FINDINGS_RESULT_CHARS,
    execute: async (args: { limit?: number }) => {
      if (runId === null) {
        throw new Error("No run to report on.");
      }
      const query = args.limit === undefined ? "" : `?limit=${String(args.limit)}`;
      return await request(`/runs/${runId}/findings${query}`, { parse: parseFindings });
    },
  });

  const resetWorkspace = useHarnessTool({
    name: "reset_workspace",
    description: "Return the workspace to a ready state so a new attempt can begin.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: false },
    // §11.1: "a run or eval run is terminal, or no run is active". FR-152
    // instructs an agent to reset and retry, and an instruction an agent has no
    // tool to obey is a defect rather than guidance.
    enabled:
      loaded && (isTerminal(phase) || phase === "no_contract" || phase === "contract_ready"),
    execute: async () => {
      const outcome = await request("/workspace/reset", {
        method: "POST",
        body: {},
        parse: (value) => value,
      });
      await refresh();
      return outcome;
    },
  });

  const createEval = useHarnessTool({
    name: "create_regression_eval",
    description: "Turn this failed run into a portable, self-contained regression eval case.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: false },
    // §11.1: "run is failed or warning-bearing and replay-eligible under
    // FR-080". Eligibility is the server's judgement; this flag only keeps the
    // tool out of the states where it is obviously wrong.
    enabled: loaded && EVAL_ELIGIBLE_PHASES.includes(phase) && runId !== null,
    execute: async () => {
      if (runId === null) {
        throw new Error("No run to cut a case from.");
      }
      const created = await request(`/runs/${runId}/${EVAL_SEGMENT}`, {
        method: "POST",
        parse: (value) => value,
      });
      await refresh();
      return created;
    },
  });

  const runEval = useHarnessTool({
    name: "run_regression_eval",
    description:
      "Replay a regression eval case in an isolated workspace and report whether it met " +
      "its expectation.",
    inputSchema: {
      type: "object",
      properties: {
        environment: {
          type: "string",
          enum: ["current", "reproduce_source"],
          description: "Which implementation to replay against. Defaults to current.",
        },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    // §11.1: "eval case exists and no eval run is active".
    enabled: loaded && evalCaseId !== null,
    execute: async (args: { environment?: string }) => {
      if (evalCaseId === null) {
        throw new Error("No eval case to replay.");
      }
      // The profile travels only when the caller named one, so the *server's*
      // default applies (§24.4: `current` is always the default) rather than
      // one this layer chose on its behalf.
      const body = args.environment === undefined ? {} : { environment: args.environment };
      return await request(`/${EVAL_SEGMENT}/${evalCaseId}/runs`, {
        method: "POST",
        body,
        parse: (value) => value,
      });
    },
  });

  return {
    states: {
      create_regression_eval: createEval,
      run_regression_eval: runEval,
      list_contract_templates: listTemplates,
      get_outcome_contract: getContract,
      arm_outcome_contract: armContract,
      verify_outcome: verifyOutcome,
      get_run_findings: getFindings,
      reset_workspace: resetWorkspace,
    },
  };
}

/** Re-exported so panels can read a run without a second client. */
export async function readRun(runId: string, signal?: AbortSignal) {
  return await request(`/runs/${runId}`, {
    parse: parseRun,
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function readWorkspace(signal?: AbortSignal) {
  return await request("/workspace", {
    parse: parseWorkspace,
    ...(signal === undefined ? {} : { signal }),
  });
}
