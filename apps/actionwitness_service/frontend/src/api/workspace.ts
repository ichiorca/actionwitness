/**
 * Narrowing the harness's workspace and run payloads (§15.1, §15.3).
 *
 * These validators are where `unknown` becomes a typed value. They are
 * deliberately tolerant about fields the UI does not use and strict about the
 * ones it does: a payload missing `guidance` is a real problem worth reporting,
 * while a new field the server started sending is not.
 *
 * **Nothing here decides anything.** Phase, next action, and enablement are all
 * server-derived (FR-120), and the frontend "shall not invent a conflicting next
 * action". So this module narrows and nothing more — the moment it started
 * computing a phase from a run status, there would be two opinions about whose
 * turn it is and no way to tell which the user was looking at.
 */

import { isRecord, optionalString, requireArray, requireRecord, requireString } from "./client";

/** §11.5's phases and FR-121's compact action, exactly as the server sends them. */
export interface Guidance {
  readonly phase: string;
  readonly activeActor: string;
  readonly nextActor: string | null;
  readonly headline: string;
  readonly instruction: string;
  readonly reason: string;
  readonly expectedConsequence: string;
  /** `null` when no safe action exists (§15.1); `headline` still renders. */
  readonly actionCode: string | null;
  readonly recoveryActionCode: string | null;
  readonly waitingFor: string | null;
  readonly requiresHumanInput: boolean;
}

export interface CapabilityReport {
  /** The registry's own key — a module name such as `buggy_store`. */
  readonly name: string;
  readonly status: string;
  /** Why, when it is not available. A bar that showed only working targets
   *  would make a misconfiguration look like a feature nobody built. */
  readonly reason: string;
}

export interface WorkspaceStatus {
  readonly workspaceId: string;
  readonly selectedTargetId: string | null;
  readonly selectedContractId: string | null;
  readonly scenarioMode: string | null;
  readonly failureProfile: string | null;
  /** The run in flight, if any. Absent rather than empty when there is none. */
  readonly activeRun: {
    readonly runId: string;
    readonly status: string;
    readonly targetId: string;
    readonly contractId: string | null;
    readonly completedAt: string | null;
  } | null;
  readonly guidance: Guidance;
  readonly nextAction: NextAction;
  readonly capabilities: readonly CapabilityReport[];
}

/** FR-121's compact projection, as sent. */
export interface NextAction {
  readonly actor: string;
  readonly actionCode: string | null;
  readonly instruction: string;
  readonly requiresHumanInput: boolean;
}

export function parseNextAction(value: unknown): NextAction {
  const record = requireRecord(value, "next_action");
  return {
    actor: requireString(record["actor"], "next_action.actor"),
    actionCode: optionalString(record["action_code"]),
    instruction: requireString(record["instruction"], "next_action.instruction"),
    requiresHumanInput: record["requires_human_input"] === true,
  };
}

export function parseGuidance(value: unknown): Guidance {
  const record = requireRecord(value, "guidance");
  return {
    phase: requireString(record["phase"], "guidance.phase"),
    activeActor: requireString(record["active_actor"], "guidance.active_actor"),
    nextActor: optionalString(record["next_actor"]),
    headline: requireString(record["headline"], "guidance.headline"),
    instruction: requireString(record["instruction"], "guidance.instruction"),
    reason: requireString(record["reason"], "guidance.reason"),
    expectedConsequence: requireString(
      record["expected_consequence"],
      "guidance.expected_consequence",
    ),
    actionCode: optionalString(record["action_code"]),
    recoveryActionCode: optionalString(record["recovery_action_code"]),
    waitingFor: optionalString(record["waiting_for"]),
    requiresHumanInput: record["requires_human_input"] === true,
  };
}

export function parseWorkspace(value: unknown): WorkspaceStatus {
  const record = requireRecord(value, "workspace");
  const capabilities = isRecord(record["capabilities"]) ? record["capabilities"] : {};
  const activeRun = record["active_run"];
  return {
    workspaceId: requireString(record["workspace_id"], "workspace_id"),
    selectedTargetId: optionalString(record["selected_target_id"]),
    selectedContractId: optionalString(record["selected_contract_id"]),
    scenarioMode: optionalString(record["scenario_mode"]),
    failureProfile: optionalString(record["failure_profile"]),
    activeRun: isRecord(activeRun)
      ? {
          runId: requireString(activeRun["id"], "active_run.id"),
          status: requireString(activeRun["status"], "active_run.status"),
          targetId: requireString(activeRun["target_id"], "active_run.target_id"),
          contractId: optionalString(activeRun["contract_id"]),
          completedAt: optionalString(activeRun["completed_at"]),
        }
      : null,
    guidance: parseGuidance(record["guidance"]),
    nextAction: parseNextAction(record["next_action"]),
    capabilities: Object.entries(capabilities).map(([name, detail]) => ({
      name,
      status: isRecord(detail) ? (optionalString(detail["status"]) ?? "unknown") : "unknown",
      reason: isRecord(detail) ? (optionalString(detail["reason"]) ?? "") : "",
    })),
  };
}

export interface PendingConfirmation {
  readonly confirmationId: string;
  readonly toolName: string;
  readonly expiresAt: string;
  readonly consequence: Record<string, unknown>;
}

export interface RunSummary {
  readonly runId: string;
  readonly status: string;
  readonly overallResult: string | null;
  readonly scenarioMode: string | null;
  readonly failureProfile: string | null;
  readonly comparisonSourceRunId: string | null;
  readonly completedAt: string | null;
  readonly pendingConfirmation: PendingConfirmation | null;
}

export function parseRun(value: unknown): RunSummary {
  const record = requireRecord(value, "run");
  const pending = record["pending_confirmation"];
  return {
    runId: requireString(record["run_id"], "run_id"),
    status: requireString(record["status"], "run.status"),
    overallResult: optionalString(record["overall_result"]),
    scenarioMode: optionalString(record["scenario_mode"]),
    failureProfile: optionalString(record["failure_profile"]),
    comparisonSourceRunId: optionalString(record["comparison_source_run_id"]),
    completedAt: optionalString(record["completed_at"]),
    pendingConfirmation: isRecord(pending)
      ? {
          confirmationId: requireString(pending["confirmation_id"], "confirmation_id"),
          toolName: requireString(pending["tool_name"], "tool_name"),
          expiresAt: requireString(pending["expires_at"], "expires_at"),
          consequence: isRecord(pending["consequence"]) ? pending["consequence"] : {},
        }
      : null,
  };
}

export interface TimelineEvent {
  readonly id: string;
  readonly sequenceNumber: number;
  readonly eventType: string;
  readonly actor: string;
  readonly toolName: string | null;
  readonly status: string | null;
  readonly reportedStatus: string | null;
  readonly createdAt: string;
}

export interface EventPage {
  readonly runId: string;
  readonly runStatus: string;
  readonly events: readonly TimelineEvent[];
  readonly nextAfterSequence: number;
  readonly hasMore: boolean;
}

export function parseEventPage(value: unknown): EventPage {
  const record = requireRecord(value, "events");
  return {
    runId: requireString(record["run_id"], "run_id"),
    runStatus: requireString(record["run_status"], "run_status"),
    events: requireArray(record["events"], "events").map((entry) => {
      const event = requireRecord(entry, "event");
      return {
        id: requireString(event["id"], "event.id"),
        sequenceNumber: Number(event["sequence_number"]),
        eventType: requireString(event["event_type"], "event.event_type"),
        actor: requireString(event["actor"], "event.actor"),
        toolName: optionalString(event["tool_name"]),
        status: optionalString(event["status"]),
        reportedStatus: optionalString(event["reported_status"]),
        createdAt: requireString(event["created_at"], "event.created_at"),
      };
    }),
    nextAfterSequence: Number(record["next_after_sequence"]),
    hasMore: record["has_more"] === true,
  };
}

export interface Finding {
  readonly checkId: string;
  readonly checkType: string;
  readonly status: string;
  readonly severity: string;
  readonly classification: string | null;
  readonly path: string | null;
  readonly expected: unknown;
  readonly actual: unknown;
}

export interface FindingsPage {
  readonly runId: string;
  readonly overallResult: string | null;
  readonly findings: readonly Finding[];
  readonly returned: number;
  readonly total: number;
  readonly failed: number;
  readonly elided: number;
  readonly report: string;
}

export function parseFindings(value: unknown): FindingsPage {
  const record = requireRecord(value, "findings");
  return {
    runId: requireString(record["run_id"], "run_id"),
    overallResult: optionalString(record["overall_result"]),
    findings: requireArray(record["findings"], "findings").map((entry) => {
      const finding = requireRecord(entry, "finding");
      return {
        checkId: requireString(finding["check_id"], "check_id"),
        checkType: requireString(finding["check_type"], "check_type"),
        status: requireString(finding["status"], "status"),
        severity: requireString(finding["severity"], "severity"),
        classification: optionalString(finding["classification"]),
        path: optionalString(finding["path"]),
        expected: finding["expected"],
        actual: finding["actual"],
      };
    }),
    returned: Number(record["returned"]),
    total: Number(record["total"]),
    failed: Number(record["failed"]),
    elided: Number(record["elided"]),
    report: requireString(record["report"], "report"),
  };
}
