/**
 * `get_workspace_status` — the always-available native tool (§11.1, AC-21).
 *
 * Registered natively rather than through the pinned hook, as §11.1 specifies.
 * It is also the tool AC-21 leans on hardest: its result, the guidance banner,
 * the enabled controls, the previous tool's `next_action`, and the action
 * history must all name the same action code at every transition. That holds
 * here for one reason — **it re-reads the server rather than reporting what the
 * page currently believes.**
 *
 * Reporting cached page state would be faster and would be wrong precisely when
 * it matters: an agent asks "whose turn is it?" exactly when something has
 * changed underneath it, and a stale answer would send it to act on a run that
 * has already moved on.
 *
 * `readOnlyHint: true` because it changes nothing. Always enabled, because a
 * workspace always has a state worth reporting — including "no contract yet",
 * which is the answer an agent needs in order to know what to do first.
 */

import { request } from "../api/client";
import { parseWorkspace } from "../api/workspace";
import { type RegistrationState, useNativeTool } from "../webmcp/adapter";

export const GET_WORKSPACE_STATUS = "get_workspace_status";

const DESCRIPTION =
  "Report the current target, scenario mode, active contract, run status, " +
  "who acts next, and the one available next action.";

/** No arguments: the workspace is identified by the session cookie, never by
 *  an argument a caller could change (§20.1). */
const INPUT_SCHEMA = {
  type: "object",
  properties: {},
  additionalProperties: false,
} as const;

export function useWorkspaceStatusTool(): RegistrationState {
  return useNativeTool({
    name: GET_WORKSPACE_STATUS,
    description: DESCRIPTION,
    inputSchema: INPUT_SCHEMA,
    annotations: { readOnlyHint: true },
    enabled: true,
    execute: async (_args, { signal }) => {
      const status = await request("/workspace", { parse: parseWorkspace, signal });
      // Compact by design (§23.3): identifiers and the next action, not
      // evidence. Full detail lives in the UI and the workspace-scoped
      // endpoints, and putting it here would blow §11.4's budget while
      // inviting an agent to treat a tool result as state.
      return {
        workspace_id: status.workspaceId,
        target: status.selectedTargetId,
        scenario_mode: status.scenarioMode,
        failure_profile: status.failureProfile,
        contract_id: status.selectedContractId,
        run_id: status.activeRun?.runId ?? null,
        run_status: status.activeRun?.status ?? null,
        phase: status.guidance.phase,
        active_actor: status.guidance.activeActor,
        // The same projection the banner renders, so the two cannot disagree.
        next_action: {
          actor: status.nextAction.actor,
          action_code: status.nextAction.actionCode,
          instruction: status.nextAction.instruction,
          requires_human_input: status.nextAction.requiresHumanInput,
        },
        capabilities: status.capabilities.map((capability) => ({
          name: capability.name,
          status: capability.status,
        })),
      };
    },
  });
}
