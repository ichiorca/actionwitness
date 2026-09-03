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

import {
  isRecord,
  optionalString,
  requireArray,
  requireRecord,
  requireString,
  stringList,
} from "./client";

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
  /**
   * The workspace a self-witnessing run observes (FR-172), or `null`.
   *
   * `null` is a statement, not an absence: it says this workspace observed a
   * target rather than itself, which is the ordinary case and the thing a
   * reader should be able to tell apart from a self run.
   */
  readonly observedWorkspaceId: string | null;
  readonly selectedContractId: string | null;
  readonly scenarioMode: string | null;
  readonly failureProfile: string | null;
  /**
   * §15.1's adapter-supported scenario controls, as the server published them.
   *
   * Opaque tokens all the way here: the panel offers them and never reads them.
   * Empty means the selected target advertises nothing — or that no target is
   * selected yet — which the panel must render as "no choice to offer" rather
   * than falling back to a list of its own. A hard-coded fallback here would be
   * the generic UI claiming to know a target's semantics, which is the one thing
   * §9.1 keeps it from doing.
   */
  readonly supportedScenarioModes: readonly string[];
  readonly supportedFaultProfiles: readonly string[];
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
  /**
   * Every optional module's state, targets and non-targets alike.
   *
   * `capabilities` answers "what can this run against?" and so covers only
   * registered target adapters — which left report import, the live evaluator
   * and the external-surface audit invisible in the UI, so a reader could not
   * tell a module that was switched off from one that had never been built.
   * The server has published this map all along; nothing read it.
   */
  readonly modules: readonly CapabilityReport[];
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
  const modules = isRecord(record["modules"]) ? record["modules"] : {};
  const activeRun = record["active_run"];
  return {
    workspaceId: requireString(record["workspace_id"], "workspace_id"),
    selectedTargetId: optionalString(record["selected_target_id"]),
    observedWorkspaceId: optionalString(record["observed_workspace_id"]),
    selectedContractId: optionalString(record["selected_contract_id"]),
    scenarioMode: optionalString(record["scenario_mode"]),
    failureProfile: optionalString(record["failure_profile"]),
    supportedScenarioModes: stringList(record["supported_scenario_modes"]),
    supportedFaultProfiles: stringList(record["supported_fault_profiles"]),
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
    modules: Object.entries(modules).map(([name, detail]) => ({
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

/** One side of §15.3's matched pre/post pair, as `ComparableRun.summary()` sends it. */
export interface ComparisonSide {
  readonly runId: string;
  readonly scenarioMode: string | null;
  /** Derived by the adapter, never by the harness (§9.1) — reported, not judged. */
  readonly faultActive: boolean;
  readonly overallResult: string | null;
  readonly criticalClassifications: readonly string[];
}

/**
 * A matched comparison, or the structured reason there is not one (FR-019).
 *
 * `comparable` decides which half of this view carries meaning, and the two
 * halves are never both populated by the server: a matched pair sends
 * `resolved`/`introduced` and no `reason`, a mismatch sends `reason` and
 * `differingFields` and no classifications. Empty lists here are therefore
 * "absent", not "none", which is why the tool that renders this emits only the
 * half `comparable` selects rather than showing an empty `resolved` list beside
 * a mismatch and inviting a reader to conclude nothing was resolved.
 */
export interface RunComparison {
  readonly comparable: boolean;
  readonly reason: string;
  readonly differingFields: readonly string[];
  readonly source: ComparisonSide;
  readonly candidate: ComparisonSide;
  readonly resolvedClassifications: readonly string[];
  readonly introducedClassifications: readonly string[];
}

function parseComparisonSide(value: unknown, field: string): ComparisonSide {
  const record = requireRecord(value, field);
  return {
    runId: requireString(record["run_id"], `${field}.run_id`),
    scenarioMode: optionalString(record["scenario_mode"]),
    faultActive: record["fault_active"] === true,
    overallResult: optionalString(record["overall_result"]),
    criticalClassifications: stringList(record["critical_classifications"]),
  };
}

export function parseComparison(value: unknown): RunComparison {
  const record = requireRecord(value, "comparison");
  return {
    // Compared against `true` rather than coerced: a body whose `comparable`
    // was missing must read as "not a pair", and a truthiness test would let
    // any non-empty value announce a matched experiment.
    comparable: record["comparable"] === true,
    reason: optionalString(record["reason"]) ?? "",
    differingFields: stringList(record["differing_fields"]),
    source: parseComparisonSide(record["source"], "comparison.source"),
    candidate: parseComparisonSide(record["candidate"], "comparison.candidate"),
    resolvedClassifications: stringList(record["resolved_classifications"]),
    introducedClassifications: stringList(record["introduced_classifications"]),
  };
}

export interface SurfaceDeltaView {
  readonly toolName: string;
  readonly kind: string;
  /** Canonical JSON text, already rendered by the server. Displayed as text. */
  readonly before: string | null;
  readonly after: string | null;
}

export interface Finding {
  readonly checkId: string;
  readonly checkType: string;
  readonly status: string;
  readonly severity: string;
  readonly classification: string | null;
  readonly path: string | null;
  /**
   * Every path this finding covers (§17.1). Empty for a single-path finding,
   * which carries `path` instead.
   *
   * `undeclared_state_change` is emitted once per run listing every undeclared
   * path, so rendering `path` alone would show one of them and read as the
   * whole answer.
   */
  readonly paths: readonly string[];
  /** Every `allow_paths` waiver applied to this finding (§9.5). */
  readonly appliedExemptions: readonly string[];
  /** FR-169's side-by-side tool-definition diff, when this finding carries one. */
  readonly surfaceDeltas: readonly SurfaceDeltaView[];
  /** Tools whose identity at invocation disagreed with the baseline (FR-169). */
  readonly identityMismatches: readonly string[];
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
        paths: stringList(finding["paths"]),
        appliedExemptions: stringList(finding["applied_exemptions"]),
        surfaceDeltas: requireArray(finding["surface_deltas"] ?? [], "surface_deltas").map(
          (entry) => {
            const delta = requireRecord(entry, "surface_delta");
            return {
              toolName: requireString(delta["tool_name"], "tool_name"),
              kind: requireString(delta["kind"], "kind"),
              before: optionalString(delta["before"]),
              after: optionalString(delta["after"]),
            };
          },
        ),
        identityMismatches: stringList(finding["identity_mismatches"]),
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
