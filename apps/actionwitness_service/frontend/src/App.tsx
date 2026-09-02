/**
 * The Tier 1 workspace (§8.4, AC-01, AC-09, AC-21).
 *
 * One desktop-first page: capability bar, guidance banner, and the working
 * panels beneath it. Everything it shows comes from FastAPI, and everything it
 * does goes back through the recorded API — so the page is a view of the
 * harness rather than a second copy of its rules.
 *
 * **The whole page works without WebMCP.** Tool registration happens in hooks
 * whose absence is a safe no-op (AC-09), and no control below is gated on a
 * tool existing. That is the property that makes "an unsupported browser
 * completes the manual equivalent" true rather than aspirational: there is no
 * separate manual path to maintain, because the human path is the only path and
 * the tools drive the same endpoints.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, request, requireRecord, stringList } from "./api/client";
import {
  type FindingsPage,
  type PendingConfirmation,
  parseFindings,
  parseRun,
} from "./api/workspace";
import { type ContractTemplateSummary, listContractTemplates } from "./api/contracts";
import {
  type EvalCaseDetail,
  createEvalCase,
  listEvalCaseDetails,
  replayEvalCase,
} from "./api/evals";
import { ConfirmationDialog } from "./components/ConfirmationDialog";
import { CREATE_CONTRACT_TOOL, ContractForm } from "./components/ContractForm";
import { GuidanceBanner } from "./components/GuidanceBanner";
import {
  CapabilityBar,
  ComparisonPanel,
  ConfigPanel,
  type ContractTemplate,
  ContractPanel,
  type EvalCaseSummary,
  EvalPanel,
  FindingsPanel,
  ToolRegistrationPanel,
  ToolSurfacePanel,
  UndeclaredChangesPanel,
  RunTimeline,
  TargetPanel,
} from "./components/panels";
import { usePoisonedToolSurface } from "./integrations/buggyStore/poisoned";
import { decide, useBuggyStoreTools } from "./integrations/buggyStore/tools";
import { confirmations } from "./state/confirmations";
import { useRunTimeline } from "./state/useRunTimeline";
import { useWorkspace } from "./state/useWorkspace";
import { useHarnessToolset } from "./tools/harnessTools";
import { GET_WORKSPACE_STATUS, useWorkspaceStatusTool } from "./tools/workspaceStatus";
import {
  type ToolExpectation,
  expectationOf,
  isWebMcpSupported,
  useToolReconciliation,
} from "./webmcp/adapter";
import { useToolSurfaceWitness } from "./webmcp/surface";

const TERMINAL = ["passed", "passed_with_warnings", "failed", "error", "cancelled"];

/** FR-080's eligible sources for a regression case. */
const EVAL_ELIGIBLE_PHASES = ["failed", "passed_with_warnings"];

/** The lifecycle groups the panels are laid out in, in reading order. */
type StageId = "setup" | "contract" | "run" | "verdict" | "regression";

/**
 * Which stage §11.5's workspace phase belongs to — presentation only.
 *
 * This map highlights a group of panels; it never gates a control, computes a
 * next action, or stands in for the guidance banner (FR-120: the server's
 * guidance is the only authority on what happens next). A phase this map does
 * not know simply highlights nothing, which is why it is a lookup with a
 * `null` fallthrough rather than an exhaustive switch that would have to
 * invent an answer for a phase added after it.
 */
const STAGE_OF_PHASE: Readonly<Record<string, StageId>> = {
  no_contract: "contract",
  proposing: "contract",
  candidates: "contract",
  contract_ready: "run",
  armed: "run",
  running: "run",
  awaiting_confirmation: "run",
  verifying: "run",
  passed: "verdict",
  passed_with_warnings: "verdict",
  failed: "verdict",
  error: "verdict",
  cancelled: "verdict",
  eval_ready: "regression",
  eval_running: "regression",
};

/**
 * Where each server-named action is performed on this page (AC-21's "the
 * banner and the enabled controls agree", made walkable).
 *
 * A lookup, not a decision: the server chose the `action_code`, and this map
 * only says which element carries it here. Codes with no human control on
 * this page — `invoke_target_tool` (the agent's turn), `decide_confirmation`
 * (the modal presents itself), `curate_candidates` (no panel yet), `wait` —
 * are deliberately absent, and an unknown code degrades to no shortcut.
 */
const ACTION_TARGET_IDS: Readonly<Record<string, string>> = {
  select_contract: "panel-contract",
  arm_run: "action-arm-run",
  verify_outcome: "action-verify-outcome",
  review_findings: "panel-findings",
  run_regression_eval: "panel-regression-evals",
  reset_workspace: "action-reset-workspace",
};

interface StageProps {
  readonly id: StageId;
  readonly step: number;
  readonly title: string;
  readonly activeStage: StageId | null;
  readonly children: React.ReactNode;
}

/**
 * One lifecycle group. The highlight is a second channel: the badge says
 * "current phase" in words, and the banner above already names the phase, so
 * nothing about the workspace's state rides on the border colour alone (§8.4).
 */
function Stage({ id, step, title, activeStage, children }: StageProps): React.ReactElement {
  const active = activeStage === id;
  return (
    <section className="stage" data-stage={id} data-active={active ? "true" : undefined}>
      <h2 className="stage__title">
        <span className="stage__step" aria-hidden="true">
          {step}
        </span>
        {title}
        {active ? <span className="stage__badge">current phase</span> : null}
      </h2>
      <div className="stage__panels">{children}</div>
    </section>
  );
}

/** A string field from an untrusted response, or a stated fallback. */
function asText(value: unknown, fallback: string): string {
  return typeof value === "string" && value !== "" ? value : fallback;
}

export default function App(): React.ReactElement {
  const { status, error, loading, refresh } = useWorkspace();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [templates, setTemplates] = useState<readonly ContractTemplateSummary[]>([]);
  const [findings, setFindings] = useState<FindingsPage | null>(null);
  const [pending, setPending] = useState<PendingConfirmation | null>(null);
  const [evalCases, setEvalCases] = useState<readonly EvalCaseDetail[]>([]);
  // The same list, readable from a callback that must not re-derive itself when
  // the list changes: `refreshEvals` runs on every phase transition, and a
  // dependency on the state it sets would rebuild the effect on its own result.
  const heldCases = useRef<readonly EvalCaseDetail[]>([]);
  heldCases.current = evalCases;
  const [comparison, setComparison] = useState<{
    comparable: boolean | null;
    differingFields: readonly string[];
    resolved: readonly string[];
    introduced: readonly string[];
  }>({ comparable: null, differingFields: [], resolved: [], introduced: [] });

  const phase = status?.guidance.phase ?? "";
  const runId = status?.activeRun?.runId ?? null;

  // Registration is unconditional — the hooks themselves are no-ops without
  // WebMCP, which keeps the rules of hooks satisfied and AC-09 true.
  const statusTool = useWorkspaceStatusTool();
  const harnessTools = useHarnessToolset(status, refresh, {
    // Verification seals the timeline, so any capture the surface witness still
    // owes this run has to land first (see `surface` below).
    //
    // Wrapped in an arrow rather than passed as `surface.flush`, and it has to
    // be: the witness is declared *after* this call — deliberately, so its
    // baseline capture sees the toolsets above it registered — so reading the
    // property here would be a temporal-dead-zone error, while calling it later
    // through a closure is fine.
    beforeVerify: () => surface.flush(),
    // The case a replay targets: the newest, which §15.4 lists first and the
    // panel therefore shows at the top. Supplying it means an agent replays the
    // case a person can see rather than one only it knows about — and it works
    // for a case cut in an earlier session, which the toolset's own memory of
    // what it created does not.
    evalCaseId: evalCases[0]?.evalCaseId ?? null,
  });
  const storeTools = useBuggyStoreTools(runId, phase, refresh);

  // FR-003 (012-T6): reconcile what this app claims against what the browser
  // reports. Assembled here because this is the only place that sees all three
  // registration mechanisms at once — the native status tool, the hook-driven
  // toolsets, and the declarative form, which is *declared* but never claimed
  // because nothing in this app registers it.
  const harnessExpectation: ToolExpectation = (() => {
    const hooked = expectationOf(harnessTools.states);
    const native = expectationOf({ [GET_WORKSPACE_STATUS]: statusTool });
    return {
      declared: [...hooked.declared, ...native.declared, CREATE_CONTRACT_TOOL],
      claimed: [...hooked.claimed, ...native.claimed],
    };
  })();
  const reconciliation = useToolReconciliation(harnessExpectation, expectationOf(storeTools.states));
  // FR-166/FR-167: capture the surface at arming and on every `toolchange`,
  // for the life of this run. The server judges what it is sent.
  //
  // Registered *after* the toolsets above, deliberately: this effect takes the
  // armed baseline, and running it before their registrations would record a
  // surface missing the tools the run is about to be judged against.
  //
  // Its `flush` is awaited by every path that verifies — the button below and
  // the `verify_outcome` tool alike — because verification seals the timeline
  // and a debounced capture would otherwise land after the verdict it was
  // evidence for.
  const surface = useToolSurfaceWitness(runId);
  // §13.3's injected surface fault. Active only in `pre_fix`, only for the
  // embedded demo target, and only while a run is open — the server already
  // refuses to record this profile for an external target, so this is the
  // second lock rather than the only one.
  usePoisonedToolSurface(
    runId !== null &&
      status?.scenarioMode === "pre_fix" &&
      status?.failureProfile === "tool_surface_poisoned",
  );

  /** One place for "do a thing, then re-read what the server now says". */
  const act = useCallback(
    async (work: () => Promise<unknown>) => {
      setBusy(true);
      setActionError(null);
      try {
        await work();
        await refresh();
      } catch (caught: unknown) {
        setActionError(caught instanceof ApiError ? caught.message : "That did not work.");
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    // 012-T5: the shared validator, because the declarative form needs each
    // template's `parameters` — which scalars it allowlists — and a second
    // hand-rolled parse of the same payload is a second thing to keep correct.
    void listContractTemplates(controller.signal).then(
      (loaded) => {
        if (live) {
          setTemplates(loaded);
        }
      },
      () => undefined,
    );
    return () => {
      live = false;
      controller.abort();
    };
  }, []);

  // The pending confirmation and the findings both follow the run, and both are
  // read from the server rather than remembered — a page that cached them would
  // show a dialog for a decision somebody already made in another tab.
  useEffect(() => {
    if (runId === null) {
      setPending(null);
      setFindings(null);
      return;
    }
    let live = true;
    const controller = new AbortController();

    void request(`/runs/${runId}`, { parse: parseRun, signal: controller.signal }).then(
      (run) => {
        if (live) {
          setPending(run.pendingConfirmation);
        }
      },
      () => undefined,
    );

    if (TERMINAL.includes(phase)) {
      void request(`/runs/${runId}/findings?limit=10`, {
        parse: parseFindings,
        signal: controller.signal,
      }).then(
        (page) => {
          if (live) {
            setFindings(page);
          }
        },
        () => undefined,
      );
      // §15.3: a matched pre/post comparison, or a structured refusal when the
      // run was armed without a source — the refusal maps to the panel's null
      // (no pair exists), never to an error the user has to dismiss. The body
      // is untrusted like every other response (constitution §5), so it is
      // narrowed with the shared helpers rather than cast — a malformed
      // record falls through to the same rejection branch as a refusal.
      void request(`/runs/${runId}/comparison`, {
        parse: (value) => requireRecord(value, "comparison"),
        signal: controller.signal,
      }).then(
        (doc) => {
          if (live) {
            setComparison({
              comparable: doc["comparable"] === true,
              differingFields: stringList(doc["differing_fields"]),
              resolved: stringList(doc["resolved_classifications"]),
              introduced: stringList(doc["introduced_classifications"]),
            });
          }
        },
        () => {
          if (live) {
            setComparison({ comparable: null, differingFields: [], resolved: [], introduced: [] });
          }
        },
      );
    }

    return () => {
      live = false;
      controller.abort();
    };
  }, [runId, phase]);

  /**
   * Re-read this workspace's regression cases, each with its latest replay.
   *
   * The cases already held are passed back in so only unseen ones cost a
   * request. This runs on every phase transition, and re-reading every case
   * each time would spend a double-figure share of FR-009's per-minute budget
   * on a panel nobody was looking at — starving the polling the page depends on.
   *
   * A superseded read is discarded rather than applied. Replaying a case takes
   * seconds and updates this list from its own response, so a listing that
   * started before the replay would otherwise land after it and put the
   * pre-replay row back on screen — the same stale-response bug `useWorkspace`
   * and `useRunTimeline` each guard against, arriving here by a different route.
   */
  const evalRead = useRef(0);
  const refreshEvals = useCallback(async (signal?: AbortSignal) => {
    const generation = (evalRead.current += 1);
    try {
      const next = await listEvalCaseDetails(heldCases.current, signal);
      if (generation === evalRead.current) {
        setEvalCases(next);
      }
    } catch {
      // A workspace with no cases is not a broken page: the panel shows an
      // empty list, which is what "none yet" looks like either way.
    }
  }, []);

  // Keyed on the run, not the phase. Cases only appear through this page's own
  // create action, which refreshes explicitly, so re-listing on every phase
  // transition would spend four requests a run re-reading an unchanged list —
  // against a budget the workspace read and the timeline poll are also drawing
  // on, and the first casualty of exhausting it is a page that silently stops
  // updating.
  useEffect(() => {
    const controller = new AbortController();
    void refreshEvals(controller.signal);
    return () => {
      controller.abort();
    };
  }, [refreshEvals, runId]);

  const timeline = useRunTimeline(runId);

  const onDecision = useCallback(
    async (decision: "approve_once" | "deny") => {
      if (runId === null || pending === null) {
        return;
      }
      await act(async () => {
        const outcome = await decide(runId, pending.confirmationId, decision);
        // Release the waiting tool handler (§14.14's correlation map). The
        // server has already recorded the decision, so this only unblocks the
        // promise — it never decides anything itself.
        confirmations.settle(
          pending.confirmationId,
          decision === "approve_once"
            ? { kind: "approved" }
            : {
                kind: "refused",
                // Narrowed rather than coerced: the response is untrusted
                // input, and `String()` on an unexpected object would put
                // "[object Object]" in front of a person as the reason their
                // action was refused.
                status: asText(outcome["status"], "denied"),
                detail: asText(outcome["detail"], "The action was refused."),
              },
        );
        setPending(null);
      });
    },
    [act, pending, runId],
  );

  // Presentation only, like the map itself: an unknown phase highlights no
  // stage rather than guessing one.
  const activeStage = STAGE_OF_PHASE[phase] ?? null;

  return (
    <main className="workspace">
      <header className="workspace__header">
        <h1>ActionWitness</h1>
        {/* A persistent glance at facts the Target and Findings panels already
            show in full — repeated display, never a second source: both values
            arrive from the server responses those panels render. */}
        {status === null ? null : (
          <p className="workspace__status">
            <span className="panel__label">Run:</span>{" "}
            {status.activeRun === null
              ? "none"
              : `${status.activeRun.runId} — ${status.activeRun.status}`}
            {findings?.overallResult == null ? null : (
              <>
                {" · "}
                <span className="panel__label">Result:</span>{" "}
                <strong data-status={findings.overallResult}>{findings.overallResult}</strong>
              </>
            )}
          </p>
        )}
      </header>

      <CapabilityBar
        capabilities={status?.capabilities ?? []}
        webMcpSupported={isWebMcpSupported()}
        registeredToolCount={reconciliation.count}
      />

      <GuidanceBanner
        guidance={status?.guidance ?? null}
        loading={loading}
        actionTargetId={
          status?.guidance.actionCode == null
            ? null
            : (ACTION_TARGET_IDS[status.guidance.actionCode] ?? null)
        }
      />

      {error === null ? null : <p role="alert">{error}</p>}
      {actionError === null ? null : <p role="alert">{actionError}</p>}

      {status === null ? null : (
        <div className="workspace__panels">
          <Stage id="setup" step={1} title="Set up" activeStage={activeStage}>
            <ConfigPanel
            status={status}
            busy={busy}
            onScenarioMode={(mode) => {
              void act(async () =>
                request("/workspace/scenario-mode", {
                  method: "PUT",
                  body: { scenario_mode: mode },
                  parse: (value) => value,
                }),
              );
            }}
            onFailureProfile={(profile) => {
              void act(async () =>
                request("/workspace/failure-profile", {
                  method: "PUT",
                  body: { failure_profile: profile },
                  parse: (value) => value,
                }),
              );
            }}
            onReset={() => {
              void act(async () =>
                request("/workspace/reset", { method: "POST", body: {}, parse: (value) => value }),
              );
            }}
          />

            <ToolRegistrationPanel reconciliation={reconciliation} />
          </Stage>

          <Stage id="contract" step={2} title="Contract" activeStage={activeStage}>
            <ContractForm
            templates={templates}
            onCreated={(contract) => {
              // §6.3 steps 6–7: the guidance banner moves to "arm the contract"
              // and the arming tools register — both of which need the new
              // contract *selected*. Creating one and leaving the workspace
              // pointing at the old one would end the journey a step short of
              // where the form promised to take it.
              void act(async () =>
                request(`/contracts/${contract.contractId}/select`, {
                  method: "POST",
                  parse: (value) => value,
                }),
              );
            }}
          />

          <ContractPanel
            templates={templates.map(
              (template): ContractTemplate => ({
                contractId: template.contractId,
                sourceTemplateId: template.sourceTemplateId,
                // §15.2's summary carries no `title`; this is what the panel
                // already showed, kept as it was rather than changed in passing.
                title: template.sourceTemplateId,
              }),
            )}
            selectedContractId={status.selectedContractId}
            busy={busy}
            canSelect={status.activeRun === null || TERMINAL.includes(status.activeRun.status)}
            onSelect={(contractId) => {
              void act(async () =>
                request(`/contracts/${contractId}/select`, {
                  method: "POST",
                  parse: (value) => value,
                }),
              );
            }}
          />
          </Stage>

          <Stage id="run" step={3} title="Run" activeStage={activeStage}>
            <TargetPanel
            status={status}
            busy={busy}
            canArm={phase === "contract_ready"}
            canVerify={phase === "running"}
            onArm={() => {
              void act(async () =>
                request("/runs", { method: "POST", body: {}, parse: (value) => value }),
              );
            }}
            onVerify={() => {
              void act(async () =>
                request(`/runs/${runId ?? ""}/verify`, { method: "POST", parse: (value) => value }),
              );
            }}
          />

          <RunTimeline
            events={timeline.events}
            runStatus={timeline.runStatus}
            polling={timeline.polling}
            // A poll that could not land is said out loud. The hook has always
            // computed this and nothing rendered it, so a dropped connection
            // looked exactly like a quiet run.
            error={timeline.error}
          />
          </Stage>

          <Stage id="verdict" step={4} title="Verdict" activeStage={activeStage}>
            <FindingsPanel
            findings={findings?.findings ?? []}
            total={findings?.total ?? 0}
            elided={findings?.elided ?? 0}
            reportPath={findings?.report ?? null}
            overallResult={findings?.overallResult ?? null}
          />

          {/* §9.10's partition, beside the findings it is drawn from. Renders
              nothing when the run's contract carried no such policy. */}
          <UndeclaredChangesPanel findings={findings?.findings ?? []} />

          {/* FR-169's side-by-side definition diff, beside the finding it
              belongs to. Renders nothing when the contract carried no
              `stable_tool_surface` policy. */}
          <ToolSurfacePanel findings={findings?.findings ?? []} />

          <ComparisonPanel
            comparable={comparison.comparable}
            differingFields={comparison.differingFields}
            resolved={comparison.resolved}
            introduced={comparison.introduced}
          />
          </Stage>

          <Stage id="regression" step={5} title="Regression" activeStage={activeStage}>
          {/* §24: a failed run becomes a file CI can replay. Rendered rather
              than left to the agent tools alone — AC-22 measures the §11.1
              table for reachability, and a capability with no human path is
              half present. */}
          <EvalPanel
            cases={evalCases.map(
              (entry): EvalCaseSummary => ({
                evalCaseId: entry.evalCaseId,
                name: entry.name,
                contentHash: entry.contentHash,
                // §24.3's two results, carried separately all the way here: a
                // reproduced failure is a *passing* eval whose target failed.
                latestStatus: entry.latest?.status ?? null,
                latestOutcome: entry.latest?.overallResult ?? null,
                latestEnvironment: entry.latest?.environment ?? null,
              }),
            )}
            busy={busy}
            // FR-080: only a failed or warning-bearing run can produce a case.
            // The server refuses otherwise; this keeps the button out of the
            // way rather than inviting the refusal.
            canCreate={runId !== null && EVAL_ELIGIBLE_PHASES.includes(phase)}
            onCreate={() => {
              void act(async () => {
                await createEvalCase(runId ?? "");
                await refreshEvals();
              });
            }}
            onReplay={(evalCaseId, environment) => {
              void act(async () => {
                // The replay's own response carries the result, so it is merged
                // rather than re-read: a second request for a fact already in
                // hand is a request the page's polling does not get to make.
                const latest = await replayEvalCase(evalCaseId, environment);
                // This result is newer than any listing still in flight, and
                // says so — a read that started before the replay must not put
                // the pre-replay row back.
                evalRead.current += 1;
                setEvalCases((held) =>
                  held.map((entry) =>
                    entry.evalCaseId === evalCaseId ? { ...entry, latest } : entry,
                  ),
                );
              });
            }}
          />
          </Stage>
        </div>
      )}

      {pending === null ? null : (
        <ConfirmationDialog
          pending={pending}
          // §14.14: only the tab holding the waiting promise offers controls.
          owned={confirmations.isWaiting(pending.confirmationId)}
          busy={busy}
          onApprove={() => void onDecision("approve_once")}
          onDeny={() => void onDecision("deny")}
        />
      )}
    </main>
  );
}
