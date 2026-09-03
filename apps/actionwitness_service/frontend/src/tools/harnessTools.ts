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
 * Two of §11.1's eleven tools are not registered by this hook, for two
 * different reasons. They are named here so each absence reads as a decision
 * rather than an oversight — AC-22 measures "every capability in the §11.1
 * table is reachable by tool" against that table, and a silent gap would be the
 * wrong kind of surprise at the Tier 1 gate.
 *
 * - `create_outcome_contract` is registered elsewhere, not missing. §11.1 gives
 *   it the declarative mechanism, so it exists because the contract panel's
 *   `<form>` exists (§25.2, `useDeclarativeTool`); nothing here can register it
 *   without giving the agent a second, divergent affordance.
 * - `propose_assertions` genuinely does not exist. It needs proposal-mode runs,
 *   and `run_service` refuses `mode: "proposal"` by name in this build, so a
 *   tool that derived candidates would have no run to derive them from.
 *
 * ## Beyond §11.1: the rest of the reachable surface
 *
 * §32 makes the agent "a first-class user of ActionWitness, not only its
 * subject", and §11.1's table is the Tier 1 floor rather than the ceiling. The
 * second group below publishes the reads an agent needs to finish a job it can
 * already start — the run timeline, the matched comparison its own
 * `arm_outcome_contract` can bind, the eval cases it may replay, the audit
 * catalogue and report, and the benchmark matrix. Every one is read-only, and
 * that is a finding rather than a convenience: **what remains unpublished is
 * unpublished because publishing it would hand an agent something §7.5 reserves
 * to a person, or would let the subject of a check author its own evidence.**
 *
 * - **Confirmation decisions** (`POST …/confirmations/{id}/decision`) stay
 *   human. Constitution §5: an agent cannot create, broaden, or approve its own
 *   consent, and §32 names confirmation decisions as reserved.
 * - **Audit authorization** (`POST /audits`) stays human. It is an assertion
 *   that a person is allowed to audit somebody else's storefront; §32 reserves
 *   authorization assertion by name, and a tool would let an agent make that
 *   claim on an operator's behalf.
 * - **Audit evidence submission** (`POST /audits/current/evidence`) stays
 *   human, and this is the least obvious exclusion. The transcript *is* the
 *   independent observation channel — §12.17 puts it in the operator's own
 *   browser session precisely so it is not the tool's own account of itself. An
 *   agent that exercised a storefront and then handed in the transcript would be
 *   the subject of the audit authoring its evidence, which collapses the
 *   independence the whole feature rests on.
 * - **Scenario mode and fault profile** stay human (§32, reserved by name).
 * - **Benchmark curation** — creating a suite, importing an evaluator report,
 *   freezing variants, replaying and finalizing — stays human. FR-100 forbids an
 *   agent approving its own material, and the rest is an operator's own file and
 *   an operator's own seal.
 * - **The full run report** (`GET /runs/{id}/report`) is not a tool because it
 *   cannot be one honestly: §11.4 budgets a tool result at 1,500 characters and
 *   asks for "detailed evidence server-side rather than in tool output". A
 *   truncated verdict document is worse than a pointer to the whole one, so
 *   `get_run_findings` carries the pointer and this does not compete with it.
 *
 * Every capability published here is also reachable without WebMCP: each has a
 * panel, and nothing below is the only route to anything.
 *
 * ## Appendix D is the schema authority, and where this build departs from it
 *
 * Appendix D is normative for names, descriptions, required fields and enum
 * values, and `harnessTools.test.ts` pins every schema below against it. Three
 * departures are deliberate, and each is a case where D.1 describes a server
 * this build does not have:
 *
 * - **Spec'd arguments are accepted but not required.** D.1 marks `run_id` and
 *   `eval_case_id` required; the workspace already knows its active run and
 *   selected case. Accepting the argument keeps a D.1-following agent from
 *   meeting a validation error, and treating it as optional keeps the human
 *   panel's zero-argument invocation working. Supplied wins over inferred, so
 *   an agent that names a run gets the run it named.
 * - **Options the server refuses are not advertised.** D.1 offers
 *   `mode: "proposal"`; `run_service` refuses it. Publishing the enum value
 *   would offer an action the server rejects, which §11.5 calls exactly
 *   backwards, so only `verification` is published. `create_regression_eval`'s
 *   optional `name` is omitted for the same reason: `POST /runs/{id}/evals`
 *   accepts no body, and a name this layer swallowed would be a parameter the
 *   agent believes it set.
 * - **`get_run_findings`'s bounds follow §11.4 and the service.** D.1 says
 *   `limit` is 1–25 defaulting to 10; §11.4 says the default is 3; the service
 *   enforces 1–10 with a default of 3. Advertising 25 would produce a 422 from
 *   FastAPI, so the published bounds are the ones that actually hold.
 */

import { useCallback, useState } from "react";

import { request } from "../api/client";
import { API_PREFIX, isRecord, optionalString, stringList } from "../api/client";
import { type AuditOutcome, listAuditPacks, readAuditReport } from "../api/audit";
import { type BenchmarkView, listBenchmarks, readBenchmark } from "../api/benchmark";
import { readOutcomeContract } from "../api/contracts";
import { parseEvalCases } from "../api/evals";
import {
  type EventPage,
  type Finding,
  type FindingsPage,
  type RunComparison,
  type WorkspaceStatus,
  parseComparison,
  parseEventPage,
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

/**
 * Appendix D's shared identifier bounds, named once so the schemas read as data.
 *
 * D.1 gives every `run_id`, `contract_id` and `eval_case_id` the same 8–80
 * range. Repeating the numbers per property is how one of them eventually
 * becomes 8–90 and nobody notices.
 */
const D1_IDENTIFIER = { type: "string", minLength: 8, maxLength: 80 } as const;

/**
 * §11.4 and `findings_service.py`, which agree with each other and not with
 * D.1 — see this module's header for the three-way contradiction.
 */
const FINDING_LIMIT = { minimum: 1, maximum: 10, default: 3 } as const;

/**
 * The timeline page `get_run_timeline` asks for when the caller names none.
 *
 * Smaller than the service's own default of 50, and deliberately: §11.4 budgets
 * a tool result at 1,500 characters, fifty events do not fit inside it, and a
 * page that arrived truncated would read as a timeline that ended. Asking for a
 * page this tool can actually deliver is the honest version — `next_after_sequence`
 * is what carries the reader to the rest.
 *
 * The *bounds* stay the server's. 1–100 is what `GET /runs/{id}/events` accepts,
 * it is published unchanged, and a caller's own value is forwarded verbatim so
 * an out-of-range limit comes back as FastAPI's refusal rather than a silent
 * correction made here.
 */
const TIMELINE_PAGE = { minimum: 1, maximum: 100, default: 10 } as const;

/** §15.6's identifier bounds, which are the route's rather than D.1's. */
const SUITE_IDENTIFIER = { type: "string", minLength: 1, maxLength: 128 } as const;

/** D.1's `include` values. `failures` is its default, and ours. */
type FindingsInclude = "failures" | "all";

/**
 * One string argument from a WebMCP call, or `null`.
 *
 * Arguments arrive from an agent, so they arrive as `unknown` however the
 * schema was written: the published schema is a discovery aid and the browser
 * is under no obligation to have enforced it. Empty is treated as absent —
 * `run_id: ""` is a caller that filled nothing in, and forwarding it would build
 * `/runs//verify`.
 */
function stringArgument(args: Record<string, unknown>, name: string): string | null {
  const value = args[name];
  return typeof value === "string" && value !== "" ? value : null;
}

/**
 * A numeric argument, as a query value, or `null` when absent.
 *
 * Deliberately *not* range-checked: §11.4's bounds belong to the service, and a
 * client that applied them itself could simply not. A string is accepted
 * alongside a number because a browser is free to hand arguments over
 * unparsed, and both forward verbatim so an out-of-range value comes back as
 * the server's own refusal.
 *
 * Anything that is not a scalar is refused here rather than forwarded, because
 * it has no query representation at all: stringifying an object would send
 * `[object Object]` and turn a caller's mistake into a puzzle about the
 * harness.
 */
function numericArgument(args: Record<string, unknown>, name: string): string | null {
  const value = args[name];
  if (value === undefined) {
    return null;
  }
  if (typeof value === "number" || typeof value === "string") {
    return String(value);
  }
  throw new Error(`${name} must be an integer.`);
}

/**
 * D.1's `include`, narrowed.
 *
 * An unrecognised value is refused rather than defaulted. Defaulting would
 * answer a question the caller did not ask — an agent that asked for something
 * this build cannot do should hear so, not receive a different view and believe
 * it got the one it named.
 */
function includeArgument(args: Record<string, unknown>): FindingsInclude {
  const value = args["include"];
  if (value === undefined || value === "failures") {
    return "failures";
  }
  if (value === "all") {
    return "all";
  }
  throw new Error("include must be either 'failures' or 'all'.");
}

/**
 * FR-150's agent-facing finding, from the validated page.
 *
 * `parseFindings` exists to narrow an untrusted body and it keeps doing that;
 * this only renames what it produced. The rename is the point: FR-150 names
 * `check_id` and `overall_result`, and returning the parsed object handed the
 * agent `checkId` and `overallResult` — field names that appear in no document
 * the agent can read, so a tool result could not be matched against the
 * requirement that describes it.
 *
 * The optional lists are omitted when empty rather than sent as `[]`. §11.4
 * budgets this result at 4,000 characters, and a single-path finding that
 * carried three empty arrays would spend that budget saying nothing.
 * `surface_deltas` is dropped entirely: FR-150 does not name it, the deltas are
 * canonical JSON documents that would exhaust the budget on their own, and
 * §11.4 asks for "detailed evidence server-side rather than in tool output" —
 * the `report` path below is where a reader goes for it.
 */
function findingDocument(finding: Finding): Record<string, unknown> {
  return {
    check_id: finding.checkId,
    check_type: finding.checkType,
    classification: finding.classification,
    severity: finding.severity,
    status: finding.status,
    path: finding.path,
    expected: finding.expected,
    actual: finding.actual,
    // §17.1: a finding covering many paths carries them all, and
    // `undeclared_state_change` is emitted once per run listing every one.
    ...(finding.paths.length === 0 ? {} : { paths: finding.paths }),
    // §9.5: a waiver a reader cannot see is a waiver nobody reviews.
    ...(finding.appliedExemptions.length === 0
      ? {}
      : { applied_exemptions: finding.appliedExemptions }),
    ...(finding.identityMismatches.length === 0
      ? {}
      : { identity_mismatches: finding.identityMismatches }),
  };
}

/**
 * FR-150's page, with D.1's `include` applied as a view over it.
 *
 * The filter runs here rather than in a query parameter because the service has
 * no `include`: `GET /runs/{id}/findings` returns every check, ordered failures
 * first. That ordering is what makes the client-side filter safe — the limit
 * has already kept the most severe findings, so dropping passes can only remove
 * entries the caller explicitly said it did not want, never hide a failure the
 * limit would have shown.
 *
 * `failures` drops findings whose status is `passed`, which is one notch wider
 * than the literal reading of D.1's "only failed checks". A warning is a
 * non-pass an agent has to act on, and a filter that hid one would be hiding a
 * finding — the one outcome §11.4 says a bounded result may never produce.
 *
 * `returned` and `elided` are recomputed so the counts still describe the list
 * that was actually sent. `total` and `failed` are the server's own figures for
 * the whole run and are passed through untouched: they are what stops an agent
 * that received two findings from concluding there were two.
 */
function findingsDocument(page: FindingsPage, include: FindingsInclude): Record<string, unknown> {
  const shown =
    include === "all" ? page.findings : page.findings.filter((entry) => entry.status !== "passed");
  return {
    run_id: page.runId,
    overall_result: page.overallResult,
    include,
    findings: shown.map(findingDocument),
    returned: shown.length,
    total: page.total,
    failed: page.failed,
    elided: Math.max(0, page.total - shown.length),
    report: page.report,
  };
}

/**
 * One page of the harness's own record of a run (§15.3).
 *
 * The stored event carries more than an agent can be handed: §20.3's
 * `redacted_payload`, hashes, correlation and request identifiers. `parseEventPage`
 * already drops those; this names what is left in the keys the API uses, so a
 * reader can match a line here against `GET /runs/{id}/events`.
 *
 * `status` and `reported_status` both travel and are never merged. One is what
 * the harness recorded about an invocation and the other is what the tool said
 * about itself, and the distance between them is the entire subject of this
 * product — a document that carried one of them would be read as the other.
 */
function timelineDocument(page: EventPage): Record<string, unknown> {
  return {
    run_id: page.runId,
    run_status: page.runStatus,
    events: page.events.map((event) => ({
      sequence_number: event.sequenceNumber,
      event_type: event.eventType,
      actor: event.actor,
      ...(event.toolName === null ? {} : { tool_name: event.toolName }),
      ...(event.status === null ? {} : { status: event.status }),
      ...(event.reportedStatus === null ? {} : { reported_status: event.reportedStatus }),
    })),
    returned: page.events.length,
    // The cursor for the next call, so a bounded page is a page rather than a
    // silently shortened timeline.
    next_after_sequence: page.nextAfterSequence,
    has_more: page.hasMore,
  };
}

function comparisonSideDocument(side: RunComparison["source"]): Record<string, unknown> {
  return {
    run_id: side.runId,
    scenario_mode: side.scenarioMode,
    fault_active: side.faultActive,
    overall_result: side.overallResult,
    critical_classifications: side.criticalClassifications,
  };
}

/**
 * FR-019's matched pair, or the named reason there is not one.
 *
 * Only the half `comparable` selects is emitted. The server populates one half
 * or the other, so an empty `resolved_classifications` beside a mismatch would
 * not mean "nothing was resolved" — it would mean the question was never asked,
 * and a reader has no way to tell those apart from an empty list.
 */
function comparisonDocument(runId: string, comparison: RunComparison): Record<string, unknown> {
  return {
    run_id: runId,
    comparable: comparison.comparable,
    source: comparisonSideDocument(comparison.source),
    candidate: comparisonSideDocument(comparison.candidate),
    ...(comparison.comparable
      ? {
          resolved_classifications: comparison.resolvedClassifications,
          introduced_classifications: comparison.introducedClassifications,
        }
      : {
          differing_fields: comparison.differingFields,
          reason: comparison.reason,
        }),
  };
}

/**
 * The merchant-facing half of a sealed audit report, bounded (§11.4, FR-163).
 *
 * The stored report is composed for a person to read and comfortably exceeds a
 * tool result's 1,500 characters: every tool carries a sentence of advice, and
 * the engineer-grade `evidence` section is the whole transcript. So the advice
 * and the evidence are dropped and `report` points at the endpoint that serves
 * the sealed bytes — the same shape `get_run_findings` uses, and the same reason:
 * §11.4 asks for detailed evidence server-side rather than in tool output.
 *
 * `content_hash` travels because it is what makes the pointer checkable. A
 * reader who fetches the full report can confirm they were given the document
 * this result described rather than one that replaced it.
 *
 * Everything read out of `report` is narrowed rather than cast. It arrives as
 * `unknown` like any other response body, and an unexpected shape must show as
 * absent — `String()` on an object would render `[object Object]` into a
 * statement about somebody's storefront.
 */
function auditReportDocument(outcome: AuditOutcome): Record<string, unknown> {
  const summary = isRecord(outcome.report["summary"]) ? outcome.report["summary"] : {};
  const tools = Array.isArray(summary["tools"]) ? summary["tools"] : [];
  return {
    audited_site: optionalString(outcome.report["audited_site"]),
    checked_using: optionalString(outcome.report["checked_using"]),
    headline: optionalString(summary["headline"]) ?? "",
    what_this_means: optionalString(summary["what_this_means"]) ?? "",
    tools: tools.filter(isRecord).map((entry) => ({
      tool: optionalString(entry["tool"]) ?? "",
      says: optionalString(entry["says"]) ?? "",
    })),
    not_checked: stringList(summary["not_checked"]),
    content_hash: outcome.contentHash,
    report: `${API_PREFIX}/audits/current/report`,
  };
}

/**
 * §15.6's matrix and rates, without the parts that would not fit (§11.4).
 *
 * `trials`, `by_scenario`, `by_failure_profile` and the sealed manifest are
 * dropped rather than truncated: a matrix cut off halfway reads as a smaller
 * matrix, and the panel and `GET /benchmarks/{id}` already serve the whole
 * document to anyone who needs it.
 *
 * Each rate is emitted as the server's own presentation string, which is `null`
 * over an empty population. FR-092 is explicit that this is never `0` — a rate
 * nobody could compute is not a rate of zero, and rendering it as one would
 * report a suite with no eligible trials as a total failure.
 */
function benchmarkDocument(view: BenchmarkView): Record<string, unknown> {
  return {
    benchmark_id: view.benchmarkId,
    status: view.status,
    // AC-16: the declared source travels with every figure, so a recorded
    // fixture is never read as a live execution.
    source_kind: view.sourceKind,
    correlation_mode: view.correlationMode,
    manifest_content_hash: view.manifestContentHash,
    counts: {
      call_level_pass_outcome_pass: view.counts.callLevelPassOutcomePass,
      call_level_pass_outcome_fail: view.counts.callLevelPassOutcomeFail,
      call_level_fail_outcome_pass: view.counts.callLevelFailOutcomePass,
      call_level_fail_outcome_fail: view.counts.callLevelFailOutcomeFail,
      eligible_trials: view.counts.eligibleTrials,
      excluded_trials: view.counts.excludedTrials,
      error_trials: view.counts.errorTrials,
      total_trials: view.counts.totalTrials,
    },
    metrics: {
      call_level_pass_rate: view.metrics.callLevelPassRate.value,
      outcome_pass_rate: view.metrics.outcomePassRate.value,
      end_to_end_success_rate: view.metrics.endToEndSuccessRate.value,
      silent_outcome_failure_rate: view.metrics.silentOutcomeFailureRate.value,
      incremental_outcome_failure_trials: view.metrics.incrementalOutcomeFailureTrials,
    },
  };
}

export interface HarnessToolset {
  readonly states: Readonly<Record<string, RegistrationState>>;
  /** The case `run_regression_eval` will replay, or `null` when there is none. */
  readonly evalCaseId: string | null;
}

export interface HarnessToolsetOptions {
  /**
   * Awaited before verification, so evidence still in flight is on the record.
   *
   * Verification seals the timeline. The tool-surface witness is debounced and
   * asynchronous, so without this a delta read a moment before `verify_outcome`
   * is posted a moment after it, meets a sealed timeline, and never reaches the
   * verdict it was evidence for (014-T6's own race).
   */
  readonly beforeVerify?: () => Promise<void>;
  /**
   * The case a replay should target.
   *
   * Optional because the toolset already knows the case it just created: an
   * agent that generates a regression case and then cannot replay it has half a
   * capability, and AC-22 measures the §11.1 table for reachability rather than
   * for existence.
   *
   * A caller that can see the workspace's whole list — the panel — passes the
   * one it shows first, and that wins. It has to: a case cut in an earlier
   * session is invisible to this hook's own memory, and a person and an agent
   * looking at the same screen must be replaying the same case.
   */
  readonly evalCaseId?: string | null;
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
  options: HarnessToolsetOptions = {},
): HarnessToolset {
  // The case this toolset generated, so `run_regression_eval` has something to
  // replay without the caller having to plumb it back round.
  const [createdCaseId, setCreatedCaseId] = useState<string | null>(null);
  const evalCaseId = options.evalCaseId ?? createdCaseId;
  const beforeVerify = options.beforeVerify;
  const flushEvidence = useCallback(async () => {
    await beforeVerify?.();
  }, [beforeVerify]);
  // Before the first load there is no authoritative state. Defaulting to a
  // phase would publish acting tools against a workspace nobody has read yet —
  // `reset_workspace` in particular, whose default-phase reading is "enabled".
  // `loaded` is what keeps a guess from becoming an offer.
  const loaded = status !== null;
  const phase = status?.guidance.phase ?? "";
  const runId = status?.activeRun?.runId ?? null;
  // Guidance can advance beyond the source run — `eval_ready` is the clearest
  // example — while that run remains failed underneath. Tools that read or
  // repeat a terminal run must follow the run's status, not the overlay phase.
  const runStatus = status?.activeRun?.status ?? "";
  const isTerminalRun = isTerminal(runStatus);
  const hasContract = loaded && status.selectedContractId !== null;
  // A module the deployment reports as unavailable must be unavailable
  // everywhere (009-T12): a tool that registered and then refused every call is
  // exactly the half-shipped failure that rule exists to prevent, so the two
  // audit tools below are not registered at all unless the server says the
  // module is on. The answer is the server's own module report, read the same
  // way the audit panel reads it — nothing here decides availability.
  const auditModule = status?.modules.find((entry) => entry.name === "external_audit") ?? null;
  const auditAvailable = auditModule?.status === "enabled";

  const listTemplates = useHarnessTool({
    name: "list_contract_templates",
    description:
      "List the built-in outcome-contract templates and the flat parameters each " +
      "template accepts.",
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
    // D.1's copy, with one word removed. It says "the active publishable
    // outcome contract"; publication is a proposal-mode concept and
    // `/contracts/{id}/published` does not exist in this build, so calling the
    // returned document publishable would be a property the tool asserts and
    // nothing checks.
    description:
      "Return the active outcome contract selected for this workspace so you can learn " +
      "what this site expects of an agent before acting. Reading a contract grants no " +
      "permission and is not evidence of compliance.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    // §11.1: "a contract is selected and marked publishable". Selection is the
    // half of that this build implements, and it is the server's answer — the
    // workspace reports it — rather than a judgement made here.
    enabled: hasContract,
    execute: async () => {
      const contractId = status?.selectedContractId ?? null;
      if (contractId === null) {
        throw new Error("No contract is selected.");
      }
      return await readOutcomeContract(contractId);
    },
  });

  const armContract = useHarnessTool({
    name: "arm_outcome_contract",
    description:
      "Arm one immutable contract, validate its preconditions, capture initial " +
      "authoritative state, and return a new run identifier.",
    inputSchema: {
      type: "object",
      properties: {
        contract_id: {
          ...D1_IDENTIFIER,
          description: "Immutable contract to arm. Defaults to the one this workspace selected.",
        },
        // D.1 publishes `mode` as an enum of `verification` and `proposal`.
        // Only the first is here, because `run_service._require_supported_mode`
        // refuses the second by name: "proposal-mode runs are not available in
        // this build". Publishing a value the server rejects would be §11.5
        // exactly backwards — a tool offering an action that cannot succeed —
        // and the project's standing answer for an unimplemented option is to
        // refuse it rather than quietly downgrade it to the one that works.
        mode: {
          type: "string",
          enum: ["verification"],
          default: "verification",
          description: "Verify an existing contract. Only verification runs exist in this build.",
        },
        // Beyond D.1, and deliberately: §15.3 lets arming "optionally bind an
        // eligible immutable comparison_source_run_id", the service validates
        // it at arming, and 07-matched-comparison drives the pair through this
        // argument. Dropping it to match D.1 exactly would delete a shipped
        // capability with a live caller. Bounds are the server's, not D.1's,
        // because D.1 does not describe this property at all.
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
      const requested = stringArgument(args, "contract_id");
      const selected = status?.selectedContractId ?? null;
      // `POST /runs` takes no contract identifier on purpose: FR-024 already
      // made exactly one contract active with its target, and accepting a
      // second identifier would reintroduce the combination it forbids. So a
      // named contract is checked rather than forwarded — and a mismatch is
      // refused rather than resolved in the workspace's favour. Arming a
      // different contract from the one the caller named, and reporting success,
      // is precisely the gap between claim and outcome this harness exists to
      // catch; it must not appear in the harness itself.
      if (requested !== null && requested !== selected) {
        throw new Error(
          `This workspace has ${selected ?? "no contract"} selected, not ${requested}. ` +
            "Select that contract first, then arm.",
        );
      }
      const mode = stringArgument(args, "mode");
      const source = stringArgument(args, "comparison_source_run_id");
      const armed = await request("/runs", {
        method: "POST",
        // Only what the caller actually named travels, so the server's own
        // defaults apply rather than ones chosen here. `mode` in particular is
        // forwarded verbatim: the schema above discourages `proposal`, and the
        // server is what refuses it.
        body: {
          ...(mode === null ? {} : { mode }),
          ...(source === null ? {} : { comparison_source_run_id: source }),
        },
        parse: (value) => value,
      });
      await refresh();
      return armed;
    },
  });

  const verifyOutcome = useHarnessTool({
    name: "verify_outcome",
    description:
      "Freeze the active journey, capture final authoritative state, evaluate its outcome " +
      "contract, and return the layered verdict summary.",
    inputSchema: {
      type: "object",
      properties: {
        // D.1 marks this required. It is accepted and optional here: the
        // workspace already knows its active run, the human panel invokes with
        // no arguments at all, and an agent that follows D.1 must not meet a
        // validation error for supplying exactly what D.1 told it to supply.
        run_id: {
          ...D1_IDENTIFIER,
          description: "Active outcome run to verify. Defaults to this workspace's active run.",
        },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    // §11.1 also requires "no in-flight invocation or confirmation". An
    // in-flight invocation is not visible in the phase, so the server's
    // PRECONDITION_FAILED is what enforces it — this flag keeps the tool out of
    // the agent's way in the states where it is obviously wrong.
    enabled: loaded && phase === "running",
    execute: async (args: Record<string, unknown>) => {
      // Supplied wins over inferred. An agent that named a run asked about that
      // run, and the route is workspace-scoped, so a run this workspace does not
      // own is refused by the server rather than silently redirected here.
      const target = stringArgument(args, "run_id") ?? runId;
      if (target === null) {
        throw new Error("No run is active.");
      }
      // Evidence first, verdict second. Verification seals the timeline, so a
      // capture that is still debounced when this runs would be judged by
      // nothing.
      await flushEvidence();
      const verdict = await request(`/runs/${encodeURIComponent(target)}/verify`, {
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
      "Return structured findings for one completed run so you can act on the result: " +
      "overall status, layered results, counts, and each failed check with its path, " +
      "expected value, and actual value. Call this after verify_outcome to learn what " +
      "actually happened before reporting success to your user.",
    inputSchema: {
      type: "object",
      properties: {
        run_id: {
          ...D1_IDENTIFIER,
          description: "Terminal run to read findings for. Defaults to this workspace's run.",
        },
        limit: {
          type: "integer",
          ...FINDING_LIMIT,
          description:
            "Maximum findings to return, ordered most severe first. The total count is always reported.",
        },
        include: {
          type: "string",
          enum: ["failures", "all"],
          default: "failures",
          description: "Return only checks that did not pass, or every evaluated check.",
        },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    // §11.1: "a run has reached any terminal state, including error and
    // cancelled" — a failed run is exactly when an agent needs to read findings.
    enabled: loaded && isTerminalRun && runId !== null,
    // §11.4's one normative exception: a finding an agent cannot read is
    // equivalent to a finding that was never produced.
    resultLimit: MAX_FINDINGS_RESULT_CHARS,
    execute: async (args: Record<string, unknown>) => {
      const target = stringArgument(args, "run_id") ?? runId;
      if (target === null) {
        throw new Error("No run to report on.");
      }
      const include = includeArgument(args);
      // Forwarded verbatim when present rather than range-checked here. §11.4's
      // bounds are the service's to enforce — a client that applied them itself
      // could simply not — so an out-of-range limit comes back as the server's
      // own refusal instead of a browser's silent correction.
      const limit = numericArgument(args, "limit");
      const query = limit === null ? "" : `?limit=${encodeURIComponent(limit)}`;
      // Validated first, then reshaped. The validation is what makes the body
      // safe to read at all; the reshaping is what makes the field names the
      // ones FR-150 actually specifies.
      const page = await request(`/runs/${encodeURIComponent(target)}/findings${query}`, {
        parse: parseFindings,
      });
      return findingsDocument(page, include);
    },
  });

  const resetWorkspace = useHarnessTool({
    name: "reset_workspace",
    description:
      "Return this workspace to a ready state so you can start a fresh attempt. Use this " +
      "when findings tell you to reset and retry. It cancels nonterminal work, keeps " +
      "completed artifacts, and never touches another workspace.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: false },
    // §11.1: "a run or eval run is terminal, or no run is active". FR-152
    // instructs an agent to reset and retry, and an instruction an agent has no
    // tool to obey is a defect rather than guidance.
    enabled:
      loaded && (isTerminalRun || phase === "no_contract" || phase === "contract_ready"),
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
    description:
      "Create or return the deterministic regression eval for one failed or " +
      "warning-bearing outcome run.",
    inputSchema: {
      type: "object",
      properties: {
        run_id: {
          ...D1_IDENTIFIER,
          description:
            "Terminal source run to generate the eval from. Defaults to this workspace's run.",
        },
        // D.1 also offers an optional `name`. It is not published, because
        // `POST /runs/{run_id}/evals` accepts no body: the case name is derived
        // server-side from the source run. A name accepted here would be
        // dropped on the way out, and a caller told its eval was created would
        // believe it had named it.
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    // §11.1: "run is failed or warning-bearing and replay-eligible under
    // FR-080". Eligibility is the server's judgement; this flag only keeps the
    // tool out of the states where it is obviously wrong.
    enabled: loaded && EVAL_ELIGIBLE_PHASES.includes(runStatus) && runId !== null,
    execute: async (args: Record<string, unknown>) => {
      const target = stringArgument(args, "run_id") ?? runId;
      if (target === null) {
        throw new Error("No run to cut a case from.");
      }
      const created = await request(`/runs/${encodeURIComponent(target)}/${EVAL_SEGMENT}`, {
        method: "POST",
        parse: (value) => value,
      });
      // Remembered so the replay tool becomes reachable. Read defensively: this
      // is a response body, and a missing id must leave the replay unavailable
      // rather than pointed at `"undefined"`.
      if (isRecord(created) && typeof created["eval_case_id"] === "string") {
        setCreatedCaseId(created["eval_case_id"]);
      }
      await refresh();
      return created;
    },
  });

  const runEval = useHarnessTool({
    name: "run_regression_eval",
    description:
      "Replay one built-in eval in an isolated workspace and compare its outcome and " +
      "exact critical classifications with the selected environment expectation.",
    inputSchema: {
      type: "object",
      properties: {
        eval_case_id: {
          ...D1_IDENTIFIER,
          description: "Eval case to run. Defaults to the case this workspace has selected.",
        },
        environment: {
          type: "string",
          enum: ["current", "reproduce_source"],
          // Declared to match D.1, and it agrees with the server: §24.4 says
          // "`current` is always the default". The handler still omits the
          // field when the caller did not name one, so the default that
          // actually applies is the service's rather than this layer's.
          default: "current",
          description: "Use current corrected logic or explicitly reproduce the source fault.",
        },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: false },
    // §11.1: "eval case exists and no eval run is active".
    enabled: loaded && evalCaseId !== null,
    execute: async (args: Record<string, unknown>) => {
      const target = stringArgument(args, "eval_case_id") ?? evalCaseId;
      if (target === null) {
        throw new Error("No eval case to replay.");
      }
      // The profile travels only when the caller named one, so the *server's*
      // default applies (§24.4: `current` is always the default) rather than
      // one this layer chose on its behalf.
      const environment = stringArgument(args, "environment");
      return await request(`/${EVAL_SEGMENT}/${encodeURIComponent(target)}/runs`, {
        method: "POST",
        body: environment === null ? {} : { environment },
        parse: (value) => value,
      });
    },
  });

  // --- beyond §11.1: the reads that finish a job an agent can already start ---

  const getTimeline = useHarnessTool({
    name: "get_run_timeline",
    // Read-only. It reads the harness's own record; it starts nothing and
    // changes nothing, and the description says what the record is so a reader
    // does not mistake it for a verdict.
    description:
      "Return the harness's own ordered record of one run: the recorded events, what " +
      "each tool reported about itself, and what the harness observed. Page with " +
      "after_sequence. This is the record of what happened, not the verdict on it.",
    inputSchema: {
      type: "object",
      properties: {
        run_id: {
          ...D1_IDENTIFIER,
          description: "Run to read the timeline of. Defaults to this workspace's active run.",
        },
        after_sequence: {
          type: "integer",
          minimum: 0,
          default: 0,
          description: "Return events after this sequence number. Use next_after_sequence to page.",
        },
        limit: {
          type: "integer",
          ...TIMELINE_PAGE,
          description: "Events per page. Smaller than the service default so one page fits.",
        },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    // A timeline is worth reading while a run is in flight as well as after it
    // ends — an agent checking whether its last invocation was recorded is
    // asking mid-run. So this follows the run, not the phase.
    enabled: loaded && runId !== null,
    execute: async (args: Record<string, unknown>) => {
      const target = stringArgument(args, "run_id") ?? runId;
      if (target === null) {
        throw new Error("No run to read a timeline for.");
      }
      const after = numericArgument(args, "after_sequence");
      // The caller's limit wins; this tool's smaller page applies only when
      // nobody named one, and either value is forwarded for the server to judge.
      const limit = numericArgument(args, "limit") ?? String(TIMELINE_PAGE.default);
      const query = [
        ...(after === null ? [] : [`after_sequence=${encodeURIComponent(after)}`]),
        `limit=${encodeURIComponent(limit)}`,
      ].join("&");
      const page = await request(`/runs/${encodeURIComponent(target)}/events?${query}`, {
        parse: parseEventPage,
      });
      return timelineDocument(page);
    },
  });

  const getComparison = useHarnessTool({
    name: "get_run_comparison",
    // Read-only. `arm_outcome_contract` already lets an agent bind a comparison
    // source; without this it could bind a pair and never read the result,
    // which is half a capability rather than a capability.
    description:
      "Return the matched pre/post comparison for a run armed against a comparison " +
      "source: which critical classifications the change resolved and which it " +
      "introduced, or the named fields that stop the two runs being a comparable pair.",
    inputSchema: {
      type: "object",
      properties: {
        run_id: {
          ...D1_IDENTIFIER,
          description: "Terminal run armed with a comparison source. Defaults to this workspace's.",
        },
      },
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    // §23.7 compares "after both runs terminate". A run still in flight has no
    // outcome to compare, and the service says so; this keeps the tool out of
    // the states where the answer is knowable in advance.
    enabled: loaded && isTerminalRun && runId !== null,
    execute: async (args: Record<string, unknown>) => {
      const target = stringArgument(args, "run_id") ?? runId;
      if (target === null) {
        throw new Error("No run to compare.");
      }
      const comparison = await request(`/runs/${encodeURIComponent(target)}/comparison`, {
        parse: parseComparison,
      });
      return comparisonDocument(target, comparison);
    },
  });

  const listEvals = useHarnessTool({
    name: "list_regression_evals",
    // Read-only. `run_regression_eval` otherwise replays only the case this
    // session created or a person selected, so a case cut in an earlier session
    // was invisible to an agent — it could not name what it could not list.
    description:
      "List the regression eval cases this workspace holds, with the run each was cut " +
      "from, so a case created earlier can be named to run_regression_eval by id.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    // Cases outlive the run that produced them, so this follows the workspace
    // rather than the phase; an empty list is a real answer.
    enabled: loaded,
    execute: async () => {
      const cases = await request("/evals", { parse: parseEvalCases });
      return {
        cases: cases.map((entry) => ({
          eval_case_id: entry.evalCaseId,
          name: entry.name,
          source_run_id: entry.sourceRunId,
        })),
        returned: cases.length,
      };
    },
  });

  const auditPacks = useHarnessTool({
    name: "list_audit_packs",
    // Read-only, and a static catalogue: nothing here names or contacts an
    // origin. FR-161 requires the pack to be offered and chosen explicitly.
    description:
      "List the built-in audit contract packs offered for an authorized external-surface " +
      "audit: what each pack expects a storefront to publish, and the tools it reports as " +
      "present and deliberately never invokes.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    enabled: loaded && auditAvailable,
    execute: async () => {
      const packs = await listAuditPacks();
      return {
        packs: packs.map((pack) => ({
          pack_id: pack.packId,
          title: pack.title,
          signature: pack.signature,
          never_invoked: pack.neverInvoked,
        })),
        returned: packs.length,
      };
    },
  });

  const auditReport = useHarnessTool({
    name: "get_audit_report",
    // Read-only. Reading a sealed report is safe; producing one is not, which
    // is why `POST /audits` and the evidence submission stay human — see this
    // module's header.
    description:
      "Return the sealed report for this workspace's completed external-surface audit: " +
      "which of the audited store's tools reported success without a matching change, and " +
      "which were left alone. A clean result is evidence about what was tried, not a " +
      "guarantee; the full report states its own limits.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: {
      readOnlyHint: true,
      // The prose in this report describes a storefront the harness does not
      // own, and the tool names inside it came from that storefront's own
      // surface. It is content to read, never instructions to follow.
      untrustedContentHint: true,
    },
    enabled: loaded && auditAvailable,
    execute: async () => auditReportDocument(await readAuditReport()),
  });

  const benchmarkList = useHarnessTool({
    name: "list_benchmarks",
    // Read-only. Every other benchmark route needs an id the caller already
    // holds, which is workable for an API client and useless to an agent.
    description:
      "List the benchmark suites this workspace holds, with the status and declared " +
      "source kind of each, so a suite can be summarised by id. A suite built from a " +
      "recorded fixture is never presented as a live execution.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
    annotations: { readOnlyHint: true },
    enabled: loaded,
    execute: async () => {
      const suites = await listBenchmarks();
      return {
        benchmarks: suites.map((suite) => ({
          benchmark_id: suite.benchmarkId,
          status: suite.status,
          source_kind: suite.sourceKind,
          created_at: suite.createdAt,
        })),
        returned: suites.length,
      };
    },
  });

  const benchmarkSummary = useHarnessTool({
    name: "get_benchmark_summary",
    // Read-only. This is the product's own claim stated as numbers, and an
    // agent that can read it can check that claim rather than take it.
    description:
      "Return one benchmark suite's call-level versus outcome-level matrix and its rates, " +
      "so you can see how often a call-level pass accompanied an outcome-level failure. " +
      "Individual trials and the sealed manifest stay server-side.",
    inputSchema: {
      type: "object",
      properties: {
        benchmark_id: {
          ...SUITE_IDENTIFIER,
          description: "Suite to summarise. Obtain an identifier from list_benchmarks.",
        },
      },
      // Required, unlike the §11.1 tools' identifiers: a workspace holds many
      // suites and selects none, so there is nothing to infer and a default
      // would pick a suite on the caller's behalf.
      required: ["benchmark_id"],
      additionalProperties: false,
    },
    annotations: { readOnlyHint: true },
    enabled: loaded,
    execute: async (args: Record<string, unknown>) => {
      // Checked again here rather than trusted to the published schema: a
      // browser is under no obligation to have enforced `required`, and an
      // absent id would otherwise be requested as `/benchmarks/null`.
      const benchmarkId = stringArgument(args, "benchmark_id");
      if (benchmarkId === null) {
        throw new Error("benchmark_id is required; list_benchmarks returns the identifiers.");
      }
      // Encoded here, because the client interpolates it into the path as
      // given. An agent-supplied `../workspace` would otherwise be normalized
      // by the browser into a request for an entirely different route — still
      // workspace-scoped, so nothing leaks, but the caller would be answered
      // about something it did not ask for. Encoding turns that into a 404.
      return benchmarkDocument(await readBenchmark(encodeURIComponent(benchmarkId)));
    },
  });

  return {
    evalCaseId,
    states: {
      create_regression_eval: createEval,
      run_regression_eval: runEval,
      list_contract_templates: listTemplates,
      get_outcome_contract: getContract,
      arm_outcome_contract: armContract,
      verify_outcome: verifyOutcome,
      get_run_findings: getFindings,
      reset_workspace: resetWorkspace,
      get_run_timeline: getTimeline,
      get_run_comparison: getComparison,
      list_regression_evals: listEvals,
      list_audit_packs: auditPacks,
      get_audit_report: auditReport,
      list_benchmarks: benchmarkList,
      get_benchmark_summary: benchmarkSummary,
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
