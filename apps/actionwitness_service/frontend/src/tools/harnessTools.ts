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
import { isRecord } from "../api/client";
import { readOutcomeContract } from "../api/contracts";
import {
  type Finding,
  type FindingsPage,
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
  const hasContract = loaded && status.selectedContractId !== null;

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
    enabled: loaded && isTerminal(phase) && runId !== null,
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
    enabled: loaded && EVAL_ELIGIBLE_PHASES.includes(phase) && runId !== null,
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
