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
import {
  type BenchmarkSummary,
  type BenchmarkView,
  type VariantApprovalRequest,
  createBenchmark,
  finalizeBenchmark,
  freezeBenchmarkVariants,
  importEvaluatorReport,
  listBenchmarks,
  readBenchmark,
  replayBenchmark,
} from "./api/benchmark";
import {
  type AuditOutcome,
  type AuditPack,
  type LiveAudit,
  assertAuthorization,
  cancelAudit,
  listAuditPacks,
  readAuditReport,
  readCurrentAudit,
  submitAuditEvidence,
} from "./api/audit";
import { AuditSection } from "./components/AuditSection";
import { BenchmarkSection } from "./components/BenchmarkSection";
import { ShopifyPairingSection } from "./components/ShopifyPairingPanel";
import { ConfirmationDialog } from "./components/ConfirmationDialog";
import { CREATE_CONTRACT_TOOL, ContractForm } from "./components/ContractForm";
import { GuidanceBanner, goToAction } from "./components/GuidanceBanner";
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
  webMcpHostLabel,
  useToolReconciliation,
} from "./webmcp/adapter";
import { useToolSurfaceWitness } from "./webmcp/surface";

const TERMINAL = ["passed", "passed_with_warnings", "failed", "error", "cancelled"];

/** FR-080's eligible sources for a regression case. */
const EVAL_ELIGIBLE_PHASES = ["failed", "passed_with_warnings"];

/** The lifecycle groups the panels are laid out in, in reading order. */
type StageId = "setup" | "contract" | "run" | "verdict" | "regression" | "benchmark";

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

/** The left rail's workflow entries, in journey order. */
const WORKFLOW_NAV = [
  { id: "contract", step: 1, title: "Contract" },
  { id: "run", step: 2, title: "Run" },
  { id: "verdict", step: 3, title: "Verdict" },
  { id: "regression", step: 4, title: "Regression" },
  // §9.9's dual-layer view. Last in the journey because it reads across runs
  // rather than advancing one: a benchmark is what you look at after several
  // verdicts exist, and no workspace phase maps to it.
  { id: "benchmark", step: 5, title: "Benchmark" },
] as const;

/**
 * Shortcut targets that live on the administration view. A walk to one of
 * these must bring that view forward first — focusing a control on a hidden
 * view moves nothing a person can see.
 */
const ADMINISTRATION_TARGETS: ReadonlySet<string> = new Set([
  "stage-administration",
  "action-reset-workspace",
]);

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
    // `id` + `tabIndex={-1}`: the left rail's jump target — programmatically
    // focusable, never in the tab order.
    <section
      className="stage"
      id={`stage-${id}`}
      tabIndex={-1}
      data-stage={id}
      data-active={active ? "true" : undefined}
    >
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

/**
 * A pasted transcript's `reports` map, narrowed without inspecting the reports
 * themselves.
 *
 * Each report's *shape* is the server's business — it is the tool's own claim,
 * and this page is a courier for it. What has to be true here is only that the
 * container is an object of objects, so a paste of `"reports": 7` is named as a
 * bad transcript rather than sent on to fail somewhere less legible.
 */
function isRecordOf(value: unknown): Record<string, Record<string, unknown>> {
  const outer = requireRecord(value, "transcript.reports");
  const narrowed: Record<string, Record<string, unknown>> = {};
  for (const [name, entry] of Object.entries(outer)) {
    narrowed[name] = requireRecord(entry, `transcript.reports.${name}`);
  }
  return narrowed;
}

/**
 * An observation payload, or `null`.
 *
 * `null` is meaningful and is not an error: §12.17 says an origin with no
 * independent channel is reported `observation_unavailable`, so a transcript
 * that carries no cart read is a valid transcript about an unobservable
 * storefront. Coercing it to `{}` would turn "we could not look" into "we
 * looked and found an empty cart".
 */
function asDocument(value: unknown): Record<string, unknown> | null {
  return value === null || value === undefined ? null : requireRecord(value, "transcript.observed");
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
  // The benchmark view is read on demand rather than polled: a suite changes
  // only when this page changes it, so a timer would spend requests re-reading
  // a matrix nobody moved.
  const [benchmarks, setBenchmarks] = useState<readonly BenchmarkSummary[]>([]);
  const [benchmarkId, setBenchmarkId] = useState<string | null>(null);
  const [benchmark, setBenchmark] = useState<BenchmarkView | null>(null);
  // The selection, readable from a callback that must not re-derive itself when
  // it changes — see `refreshBenchmarks`.
  const heldBenchmarkId = useRef<string | null>(null);
  heldBenchmarkId.current = benchmarkId;
  const [auditPacks, setAuditPacks] = useState<readonly AuditPack[]>([]);
  const [audit, setAudit] = useState<LiveAudit | null>(null);
  const [auditOutcome, setAuditOutcome] = useState<AuditOutcome | null>(null);
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
  /**
   * Re-read the suite listing, and the selected suite's matrix with it.
   *
   * One callback for both because they are one question — "what is there, and
   * what does the chosen one say" — and splitting them let the listing show a
   * suite whose matrix had not arrived yet. Selection falls back to the newest
   * suite so that creating one lands the operator on it rather than on an empty
   * panel they then have to choose from.
   */
  const refreshBenchmarks = useCallback(async (preferId?: string, signal?: AbortSignal) => {
    const rows = await listBenchmarks(signal);
    setBenchmarks(rows);
    // Read from the ref, not from state: a callback that re-derived itself
    // whenever the selection changed would rebuild the mount effect below on
    // every selection, and re-running that effect would immediately undo the
    // selection that caused it. Same reason `refreshEvals` reads `heldCases`.
    const held = heldBenchmarkId.current;
    const chosen =
      preferId ??
      (held !== null && rows.some((row) => row.benchmarkId === held)
        ? held
        : (rows[0]?.benchmarkId ?? null));
    setBenchmarkId(chosen);
    if (chosen === null) {
      setBenchmark(null);
      return;
    }
    setBenchmark(await readBenchmark(chosen, signal));
  }, []);

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

  // Once, at mount. Suites outlive runs — a benchmark reads across several — so
  // keying this on the run would re-read it four times a journey for a list
  // that had not changed. A workspace with no suites is not an error, so a
  // failure here leaves the empty state rather than an error banner.
  useEffect(() => {
    const controller = new AbortController();
    refreshBenchmarks(undefined, controller.signal).catch(() => undefined);
    return () => {
      controller.abort();
    };
  }, [refreshBenchmarks]);

  /**
   * Read the audit's state: the catalogue, the live audit, and any report.
   *
   * All three tolerate refusal. The module ships off, so on most deployments
   * every one of these is a 403 — which is a fact about the deployment the
   * section renders, not an error the page should shout about. The report is
   * read separately because it exists only after an audit completes, and a 404
   * there is the ordinary state of a workspace that has not run one.
   */
  // Looked up once. The module's own reported state is what decides whether the
  // audit surface is offered at all — §21.1's "visibly disabled" needs the UI to
  // know which module it is reporting on.
  const auditModule = status?.modules.find((module) => module.name === "external_audit") ?? null;

  const refreshAudit = useCallback(async (signal?: AbortSignal) => {
    try {
      setAuditPacks(await listAuditPacks(signal));
      setAudit(await readCurrentAudit(signal));
    } catch {
      setAuditPacks([]);
      setAudit(null);
    }
    try {
      setAuditOutcome(await readAuditReport(signal));
    } catch {
      // No completed audit is the ordinary state of a workspace that has not
      // run one; a 404 here is an answer, not a failure.
      setAuditOutcome(null);
    }
  }, []);

  // Only when the server says the module is on. With it off the routes are not
  // mounted at all, so asking anyway would 404 on every page load of every
  // deployment that ships the default — a dead request whose only effect is
  // noise in the console the release checklist tells an operator to read.
  const auditEnabled = auditModule?.status === "enabled";
  useEffect(() => {
    if (!auditEnabled) {
      return;
    }
    const controller = new AbortController();
    void refreshAudit(controller.signal);
    return () => {
      controller.abort();
    };
  }, [auditEnabled, refreshAudit]);

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

  // Which of the two views the main area shows. Both stay mounted — the
  // WebMCP surface (including the declarative form's tool) must not change
  // shape because a person looked at the other view — so the inactive one is
  // `hidden`, never unmounted.
  const [view, setView] = useState<"workflow" | "audit" | "administration">("workflow");

  /** Bring the owning view forward, then walk to the control. */
  const goTo = useCallback((targetId: string) => {
    setView(ADMINISTRATION_TARGETS.has(targetId) ? "administration" : "workflow");
    // After the view re-renders — a control on a hidden view can be focused
    // but takes the reader nowhere they can see.
    requestAnimationFrame(() => {
      goToAction(targetId);
    });
  }, []);

  return (
    <div className="app">
      <nav className="sidebar" aria-label="Workspace navigation">
        <h1 className="sidebar__brand">ActionWitness</h1>
        {/* What this page is, for somebody who arrived without being told.
            Deliberately a description of the mechanism rather than a claim about
            outcomes: §8 requires product copy to state that this complements
            call-level evaluators and to claim no unverified protection, so the
            sentence says what the harness *does* — compare two sources — and
            promises nothing about what that prevents. */}
        <p className="sidebar__tagline">
          An independent witness for <strong>WebMCP</strong> agents. A WebMCP tool call reports
          its own result; this compares that report against business state observed
          independently, and judges the run on the difference.
        </p>
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

        <p className="sidebar__heading">Workflow</p>
        <ul className="sidebar__list">
          {WORKFLOW_NAV.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                className="sidebar__item"
                aria-current={
                  view === "workflow" && activeStage === item.id ? "step" : undefined
                }
                onClick={() => {
                  goTo(`stage-${item.id}`);
                }}
              >
                <span className="sidebar__step" aria-hidden="true">
                  {item.step}
                </span>
                {item.title}
                {/* The phase's home, said in a word — the rail must carry it
                    even when the reader is on the other view (§8.4). */}
                {activeStage === item.id ? <span className="sidebar__now">now</span> : null}
              </button>
            </li>
          ))}
        </ul>

        {/* A journey of its own, not a step in the run: auditing a surface
            somebody else built shares the classifier and nothing else. */}
        <p className="sidebar__heading">Audit</p>
        <ul className="sidebar__list">
          <li>
            <button
              type="button"
              className="sidebar__item"
              aria-current={view === "audit" ? "page" : undefined}
              onClick={() => {
                setView("audit");
              }}
            >
              External surface
            </button>
          </li>
        </ul>

        <p className="sidebar__heading">Administration</p>
        <ul className="sidebar__list">
          <li>
            <button
              type="button"
              className="sidebar__item"
              aria-current={view === "administration" ? "page" : undefined}
              onClick={() => {
                goTo("stage-administration");
              }}
            >
              Setup &amp; tools
            </button>
          </li>
        </ul>

        <div className="sidebar__foot">
          <CapabilityBar
            capabilities={status?.capabilities ?? []}
            webMcpSupported={isWebMcpSupported()}
            webMcpHost={webMcpHostLabel()}
            registeredToolCount={reconciliation.count}
          />
        </div>
      </nav>

      <main className="workspace">
      <GuidanceBanner
        guidance={status?.guidance ?? null}
        loading={loading}
        actionTargetId={
          status?.guidance.actionCode == null
            ? null
            : (ACTION_TARGET_IDS[status.guidance.actionCode] ?? null)
        }
        onGo={goTo}
      />

      {error === null ? null : <p role="alert">{error}</p>}
      {actionError === null ? null : <p role="alert">{actionError}</p>}

      {status === null ? null : (
        <div className="workspace__panels" hidden={view !== "workflow"}>
          <Stage id="contract" step={1} title="Contract" activeStage={activeStage}>
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

          <Stage id="run" step={2} title="Run" activeStage={activeStage}>
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

          {/* §12.12's external target. It sits in the Run stage because that is
              what it is — a run whose acting happens in another tab, on a store
              this project does not control. The section owns its own pairing
              state and cadence; nothing else on this page reads a pairing. */}
          <ShopifyPairingSection
            moduleStatus={
              status.modules.find((module) => module.name === "shopify")?.status ?? "disabled"
            }
            moduleReason={
              status.modules.find((module) => module.name === "shopify")?.reason ?? ""
            }
            contractId={status.selectedContractId}
          />
          </Stage>

          <Stage id="verdict" step={3} title="Verdict" activeStage={activeStage}>
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

          <Stage id="regression" step={4} title="Regression" activeStage={activeStage}>
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

          <Stage id="benchmark" step={5} title="Benchmark" activeStage={activeStage}>
          {/* §9.9's dual layer, made reachable. The matrix was written and
              tested long before this: what was missing was a suite listing, a
              way to create one, and a control to import a report — so the panel
              existed and no person could arrive at it. */}
          <BenchmarkSection
            benchmarks={benchmarks}
            selectedId={benchmarkId}
            benchmark={benchmark}
            busy={busy}
            onSelect={(chosen) => {
              void act(async () => {
                await refreshBenchmarks(chosen);
              });
            }}
            onCreate={(sourceKind, correlationMode) => {
              void act(async () => {
                const created = await createBenchmark(sourceKind, correlationMode);
                // Land on the suite just made, rather than leaving the operator
                // to find it in a list that now has one more row.
                await refreshBenchmarks(created);
              });
            }}
            onImport={(report) => {
              void act(async () => {
                await importEvaluatorReport(benchmarkId ?? "", report);
                await refreshBenchmarks(benchmarkId ?? undefined);
              });
            }}
            liveEvaluatorStatus={
              status?.modules.find((module) => module.name === "live_evaluator")?.status ??
              "unknown"
            }
            liveEvaluatorReason={
              status?.modules.find((module) => module.name === "live_evaluator")?.reason ?? ""
            }
            onFreezeVariants={(approval: VariantApprovalRequest) => {
              void act(async () => {
                // Re-read rather than merge the receipt: FR-100's seal is a
                // change to the manifest, and the server's copy of it is the
                // one a person should be looking at.
                await freezeBenchmarkVariants(benchmarkId ?? "", approval);
                await refreshBenchmarks(benchmarkId ?? undefined);
              });
            }}
            onReplay={() => {
              void act(async () => {
                await replayBenchmark(benchmarkId ?? "");
                await refreshBenchmarks(benchmarkId ?? undefined);
              });
            }}
            onFinalize={() => {
              void act(async () => {
                await finalizeBenchmark(benchmarkId ?? "");
                await refreshBenchmarks(benchmarkId ?? undefined);
              });
            }}
            trialHref={(externalTrialId) =>
              `/api/v1/benchmarks/${benchmarkId ?? ""}/trials/${externalTrialId}`
            }
            reportHref={`/api/v1/benchmarks/${benchmarkId ?? ""}/report`}
          />
          </Stage>
        </div>
      )}

      {/* The audit view. Its own journey: §12.17 audits a surface somebody else
          built, so it shares the classifier with the run path and none of its
          state. Hidden, never unmounted — same rule as the others, because the
          tool surface must not change because a person looked elsewhere. */}
      {status === null ? null : (
        <div className="workspace__panels" hidden={view !== "audit"}>
          <section className="stage" id="stage-audit" tabIndex={-1} data-stage="audit">
            <h2 className="stage__title">
              <span className="stage__step" aria-hidden="true">
                ★
              </span>
              External surface audit
            </h2>
            <div className="stage__panels">
              <AuditSection
                moduleStatus={auditModule?.status ?? "disabled"}
                moduleReason={auditModule?.reason ?? ""}
                packs={auditPacks}
                audit={audit}
                outcome={auditOutcome}
                busy={busy}
                onAuthorize={(origin, assertedBy) => {
                  void act(async () => {
                    // `true` is sent because the operator ticked the box the
                    // button is gated on; the server refuses a submission
                    // without it, and this client never asserts on their behalf.
                    await assertAuthorization(origin, assertedBy, true);
                    await refreshAudit();
                  });
                }}
                onSubmit={(packId, transcript) => {
                  void act(async () => {
                    // Parsed here so a malformed paste is named as such, rather
                    // than reaching the server as an unreadable body.
                    let parsed: unknown;
                    try {
                      parsed = JSON.parse(transcript);
                    } catch {
                      throw new Error("That transcript is not valid JSON.");
                    }
                    const record = requireRecord(parsed, "transcript");
                    await submitAuditEvidence({
                      packId,
                      enumerated: stringList(record["enumerated"]),
                      reports: isRecordOf(record["reports"]),
                      observedBefore: asDocument(record["observed_before"]),
                      observedAfter: asDocument(record["observed_after"]),
                    });
                    await refreshAudit();
                  });
                }}
                onCancel={() => {
                  void act(async () => {
                    await cancelAudit();
                    await refreshAudit();
                  });
                }}
              />
            </div>
          </section>
        </div>
      )}

      {/* The administration view: configuration and the registration surface,
          out of the workflow's way but one click from the rail. Hidden, never
          unmounted — the tool surface must not change because a person looked
          elsewhere. */}
      {status === null ? null : (
        <div className="workspace__panels" hidden={view !== "administration"}>
          <section
            className="stage"
            id="stage-administration"
            tabIndex={-1}
            data-stage="administration"
          >
            <h2 className="stage__title">Administration</h2>
            <div className="stage__panels">
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
            </div>
          </section>
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
    </div>
  );
}
