/**
 * The Tier 1 workspace panels (§8.4, §11.5, §14, AC-09, AC-20, AC-21).
 *
 * Presentational by design: each takes server state and callbacks, holds no
 * authoritative state of its own, and disables its controls from the phase the
 * server reported. A panel that computed its own enablement would eventually
 * offer an action the server refuses — and would do it in the states nobody
 * tests, which are the interesting ones.
 *
 * **Everything here works without WebMCP.** AC-09 requires the full human UI to
 * remain usable in a browser with no `document.modelContext`, so no control is
 * gated on registration, and the capability bar reports WebMCP's absence as a
 * fact about the browser rather than as an error the user must fix.
 *
 * Status is never carried by colour alone (§8.4): every state that matters is
 * spelled out in text, because a run that failed must read as failed to someone
 * who cannot see the red.
 */

import { useState } from "react";

import type { BenchmarkView, Rate } from "../api/benchmark";
import type { CapabilityReport, Finding, TimelineEvent, WorkspaceStatus } from "../api/workspace";
import type { ToolGroupReconciliation, ToolReconciliation } from "../webmcp/adapter";

export interface CapabilityBarProps {
  readonly capabilities: readonly CapabilityReport[];
  readonly webMcpSupported: boolean;
  readonly registeredToolCount: number;
}

export function CapabilityBar({
  capabilities,
  webMcpSupported,
  registeredToolCount,
}: CapabilityBarProps): React.ReactElement {
  return (
    <section className="capabilities" aria-label="Capabilities">
      <p>
        <span className="capabilities__label">Browser agent support:</span>{" "}
        {webMcpSupported ? (
          <>
            <strong>available</strong> — {registeredToolCount} tool
            {registeredToolCount === 1 ? "" : "s"} registered
          </>
        ) : (
          // AC-09: a statement about the browser, not an error. The workspace
          // below it is fully usable either way, and the copy has to say so or
          // a person will reasonably assume it is not.
          <>
            <strong>not available</strong> — this browser has no WebMCP. Every step below can
            still be done by hand.
          </>
        )}
      </p>
      <ul className="capabilities__targets">
        {capabilities.map((capability) => (
          <li key={capability.name}>
            <span className="capabilities__label">{capability.name}:</span>{" "}
            <strong>{capability.status}</strong>
            {capability.reason === "" ? null : ` — ${capability.reason}`}
          </li>
        ))}
      </ul>
    </section>
  );
}

export interface ToolRegistrationPanelProps {
  readonly reconciliation: ToolReconciliation;
}

/**
 * FR-003's registration status, reconciled against the browser (012-T6).
 *
 * FR-003: "show whether harness and selected-target tools are registered and
 * the number currently available. It shall not infer success solely from React
 * component mount state."
 *
 * Two groups rather than one number, because they fail for different reasons: a
 * missing harness tool is a defect in this app, while a missing target tool is
 * usually the workspace phase doing its job (§11.5). A single count cannot say
 * which happened.
 *
 * **This panel judges nothing.** An unexpected tool is named here and nothing
 * more — whether a surface is acceptable is `stable_tool_surface`'s answer,
 * evaluated by the server from recorded evidence. A view that called an extra
 * tool "fine" would be a second, softer opinion about the exact thing the
 * policy exists to decide, and the person reading it would have no way to know
 * which opinion counted. Every name is rendered as text: they come from a tool
 * registry any script on the origin can write to.
 */
export function ToolRegistrationPanel({
  reconciliation,
}: ToolRegistrationPanelProps): React.ReactElement {
  if (!reconciliation.supported) {
    // AC-09: a fact about the browser, not a fault. Said plainly, because the
    // whole workspace below works either way.
    return (
      <section className="panel" aria-label="Tool registration">
        <h3>Tool registration</h3>
        <p>This browser has no WebMCP, so no tools are registered. Every step can be done by hand.</p>
      </section>
    );
  }

  return (
    <section className="panel" aria-label="Tool registration">
      <h3>Tool registration</h3>
      <p>
        <span className="panel__label">Reported by the browser:</span>{" "}
        <strong>{reconciliation.count}</strong> tool{reconciliation.count === 1 ? "" : "s"}
      </p>
      <ul>
        <ToolGroupLine label="Harness tools" group={reconciliation.harness} />
        <ToolGroupLine label="Target tools" group={reconciliation.target} />
      </ul>
      {reconciliation.unexpected.length === 0 ? null : (
        <p>
          <span className="panel__label">Not declared by this page:</span>{" "}
          {reconciliation.unexpected.join(", ")} — the run&rsquo;s{" "}
          <code>stable_tool_surface</code> finding decides whether that matters.
        </p>
      )}
    </section>
  );
}

function ToolGroupLine({
  label,
  group,
}: {
  readonly label: string;
  readonly group: ToolGroupReconciliation;
}): React.ReactElement {
  return (
    <li>
      <span className="panel__label">{label}:</span>{" "}
      <strong>
        {group.present.length} of {group.declared.length} registered
      </strong>
      {group.missing.length === 0 ? null : (
        // The disagreement FR-003 exists to surface: this app believes it
        // registered these and the browser does not list them.
        <> — claimed but not reported: {group.missing.join(", ")}</>
      )}
    </li>
  );
}

export interface ConfigPanelProps {
  readonly status: WorkspaceStatus;
  readonly busy: boolean;
  readonly onScenarioMode: (mode: string) => void;
  readonly onFailureProfile: (profile: string) => void;
  readonly onReset: () => void;
}

const SCENARIO_MODES = ["pre_fix", "post_fix"];

export function ConfigPanel({
  status,
  busy,
  onScenarioMode,
  onFailureProfile,
  onReset,
}: ConfigPanelProps): React.ReactElement {
  // FR-012: configuration is immutable for the life of a run. Frozen here as
  // well as refused by the server, so a person is not invited to make a change
  // that will be rejected — and told *why* it is frozen rather than left to
  // guess at a disabled control (§14).
  const frozen = status.activeRun !== null && !isTerminal(status.activeRun.status);

  return (
    <section className="panel" aria-label="Configuration">
      <h3>Configuration</h3>
      {frozen ? (
        <p className="panel__note">
          Frozen while run {status.activeRun?.runId} is in progress, so its evidence keeps the
          configuration it was armed with.
        </p>
      ) : null}
      <fieldset disabled={frozen || busy}>
        <legend>Scenario mode</legend>
        {SCENARIO_MODES.map((mode) => (
          <label key={mode}>
            <input
              type="radio"
              name="scenario-mode"
              value={mode}
              checked={status.scenarioMode === mode}
              onChange={() => {
                onScenarioMode(mode);
              }}
            />
            {mode}
          </label>
        ))}
      </fieldset>
      <fieldset disabled={frozen || busy}>
        <legend>Failure profile</legend>
        <label>
          Profile
          <input
            type="text"
            name="failure-profile"
            defaultValue={status.failureProfile ?? ""}
            onBlur={(event) => {
              onFailureProfile(event.target.value);
            }}
          />
        </label>
      </fieldset>
      {/* `id`: the banner's "Go to this step" target for `reset_workspace`. */}
      <button type="button" id="action-reset-workspace" onClick={onReset} disabled={busy}>
        Reset workspace
      </button>
    </section>
  );
}

export interface ContractTemplate {
  readonly contractId: string;
  readonly sourceTemplateId: string;
  readonly title: string;
}

export interface ContractPanelProps {
  readonly templates: readonly ContractTemplate[];
  readonly selectedContractId: string | null;
  readonly busy: boolean;
  readonly canSelect: boolean;
  readonly onSelect: (contractId: string) => void;
}

export function ContractPanel({
  templates,
  selectedContractId,
  busy,
  canSelect,
  onSelect,
}: ContractPanelProps): React.ReactElement {
  return (
    // `id` + `tabIndex={-1}`: the banner's `select_contract` shortcut lands
    // here — programmatically focusable, never in the tab order.
    <section className="panel" aria-label="Contract" id="panel-contract" tabIndex={-1}>
      <h3>Outcome contract</h3>
      {canSelect ? null : (
        <p className="panel__note">
          A run is in progress, so the contract cannot be changed — FR-012 keeps a run&apos;s
          evidence bound to the contract it was armed against.
        </p>
      )}
      <ul>
        {templates.map((template) => (
          <li key={template.contractId}>
            <button
              type="button"
              disabled={busy || !canSelect}
              aria-pressed={selectedContractId === template.contractId}
              onClick={() => {
                onSelect(template.contractId);
              }}
            >
              {template.title}
            </button>
            {selectedContractId === template.contractId ? <strong> — selected</strong> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

export interface TargetPanelProps {
  readonly status: WorkspaceStatus;
  readonly busy: boolean;
  readonly canArm: boolean;
  readonly canVerify: boolean;
  readonly onArm: () => void;
  readonly onVerify: () => void;
}

export function TargetPanel({
  status,
  busy,
  canArm,
  canVerify,
  onArm,
  onVerify,
}: TargetPanelProps): React.ReactElement {
  return (
    <section className="panel" aria-label="Target">
      <h3>Target</h3>
      <p>
        <span className="panel__label">Target:</span> {status.selectedTargetId ?? "none selected"}
      </p>
      <p>
        <span className="panel__label">Run:</span>{" "}
        {status.activeRun === null ? "none" : `${status.activeRun.runId} (${status.activeRun.status})`}
      </p>
      {/* `id`s: the banner's shortcut targets for `arm_run` / `verify_outcome`. */}
      <button type="button" id="action-arm-run" onClick={onArm} disabled={busy || !canArm}>
        Arm the contract
      </button>
      <button
        type="button"
        id="action-verify-outcome"
        onClick={onVerify}
        disabled={busy || !canVerify}
      >
        Verify the outcome
      </button>
    </section>
  );
}

export interface RunTimelineProps {
  readonly events: readonly TimelineEvent[];
  readonly runStatus: string | null;
  readonly polling: boolean;
  /** Why the last poll did not land, or `null` when it did. */
  readonly error: string | null;
}

export function RunTimeline({
  events,
  runStatus,
  polling,
  error,
}: RunTimelineProps): React.ReactElement {
  return (
    <section className="panel" aria-label="Agent activity">
      <h3>Agent activity</h3>
      <p aria-live="polite">
        {runStatus === null
          ? "No run yet."
          : `Run is ${runStatus}${polling ? " — watching for new activity." : "."}`}
      </p>
      {/* A dropped connection has to be visible. `useRunTimeline` keeps trying
          and keeps what it already has, so this is a statement about the
          connection rather than about the run — and the events below stay on
          screen, which is why it says the list may be behind rather than that
          something failed. An `alert`, because a reader who has looked away
          needs to be told the page stopped keeping up. */}
      {error === null ? null : (
        <p role="alert" className="panel__error">
          {error} The activity below may be behind; this page keeps retrying.
        </p>
      )}
      <ol className="timeline">
        {events.map((event) => (
          <li key={event.id} data-event-type={event.eventType}>
            <span className="timeline__sequence">#{event.sequenceNumber}</span>{" "}
            <span className="timeline__actor">{event.actor}</span>{" "}
            <span className="timeline__type">{event.eventType}</span>
            {event.toolName === null ? null : <span className="timeline__tool"> {event.toolName}</span>}
            {event.reportedStatus === null ? null : (
              // The tool's own claim, labelled as such. §23.1 keeps the
              // self-report and the observation distinguishable, and a
              // timeline that showed only one would hide the disagreement.
              <span className="timeline__reported"> reported: {event.reportedStatus}</span>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

export interface FindingsPanelProps {
  readonly findings: readonly Finding[];
  readonly total: number;
  readonly elided: number;
  readonly reportPath: string | null;
  readonly overallResult: string | null;
}

export function FindingsPanel({
  findings,
  total,
  elided,
  reportPath,
  overallResult,
}: FindingsPanelProps): React.ReactElement {
  return (
    // `id` + `tabIndex={-1}`: the banner's `review_findings` shortcut target.
    <section className="panel" aria-label="Findings" id="panel-findings" tabIndex={-1}>
      <h3>Findings</h3>
      {overallResult === null ? (
        <p>No verdict yet.</p>
      ) : (
        // Spelled out, never signalled by colour alone: a failed run has to
        // read as failed to someone who cannot see the red. `data-status`
        // echoes the word so colour can follow it as a second channel.
        <p>
          <span className="panel__label">Result:</span>{" "}
          <strong data-status={overallResult}>{overallResult}</strong>
        </p>
      )}
      <ul>
        {findings.map((finding) => (
          <li key={finding.checkId}>
            <strong data-status={finding.status}>{finding.status}</strong> — {finding.checkId}
            {finding.classification === null ? null : ` (${finding.classification})`}
            {finding.path === null ? null : <div className="finding__path">at {finding.path}</div>}
            <div className="finding__values">
              expected {JSON.stringify(finding.expected)}, observed {JSON.stringify(finding.actual)}
            </div>
          </li>
        ))}
      </ul>
      {elided > 0 ? (
        // The untruncated total, for the same reason the tool result carries
        // it: a shortened list that did not say so reads as the whole list.
        <p>
          Showing {findings.length} of {total}. {elided} more{" "}
          {reportPath === null ? "in the full report." : <a href={reportPath}>in the full report.</a>}
        </p>
      ) : null}
    </section>
  );
}

export interface UndeclaredChangesPanelProps {
  readonly findings: readonly Finding[];
}

/**
 * §9.10's partition, rendered from the server's own finding (013-T6).
 *
 * Server-driven like every other panel here: the paths, the waivers and the
 * verdict are all read off the finding the engine produced. Nothing is computed
 * in the browser, because a second implementation of the partition would
 * eventually disagree with the one that decided the run — and the UI is not
 * where that argument should be settled.
 *
 * The panel renders only when the policy actually produced a finding. A run that
 * never carried `no_undeclared_changes` shows nothing rather than an empty
 * "changed outside contract" heading, which would read as a clean result for a
 * check that never ran.
 */
export function UndeclaredChangesPanel({
  findings,
}: UndeclaredChangesPanelProps): React.ReactElement | null {
  const finding = findings.find((entry) => entry.checkId === "no_undeclared_changes");
  if (finding === undefined) {
    return null;
  }

  // `not_evaluated` is a distinct outcome from "nothing changed" and has to look
  // like one: §16.1 exists so an unevaluated policy never reads as a satisfied
  // one, and this is the surface where that would be easiest to blur.
  if (finding.status === "not_evaluated") {
    return (
      <section className="panel" aria-label="Changed outside contract">
        <h3>Changed outside contract</h3>
        <p>Not evaluated — no full-state comparison was available for this run.</p>
      </section>
    );
  }

  return (
    <section className="panel" aria-label="Changed outside contract">
      <h3>Changed outside contract</h3>
      {finding.paths.length === 0 ? (
        <p>Nothing changed outside what this contract declared.</p>
      ) : (
        <>
          {/* Stated in words, never by colour alone. */}
          <p>
            <span className="panel__label">Result:</span>{" "}
            <strong data-status={finding.status}>{finding.status}</strong> —{" "}
            {finding.paths.length} path{finding.paths.length === 1 ? "" : "s"} changed that no
            assertion, precondition, or executed tool&rsquo;s declared effect covered.
          </p>
          <ul>
            {finding.paths.map((path) => (
              <li key={path} className="finding__path">
                {path}
              </li>
            ))}
          </ul>
        </>
      )}
      {finding.appliedExemptions.length > 0 ? (
        // §9.5: "each exemption is recorded in the report so the waiver is
        // visible". A waiver nobody can see is a waiver nobody reviews, so it is
        // shown even on a passing run — especially on a passing run.
        <p>
          Waived by <code>allow_paths</code>: {finding.appliedExemptions.join(", ")}
        </p>
      ) : null}
    </section>
  );
}

export interface ToolSurfacePanelProps {
  readonly findings: readonly Finding[];
}

/**
 * FR-169's side-by-side tool-definition diff (014-T6).
 *
 * "The `stable_tool_surface` policy shall fail a run on any undeclared delta of
 * a configured kind, classified `tool_surface_mutation`, **with a side-by-side
 * diff of the tool definition before and after as evidence**."
 *
 * The diff is the point. A reader told only that a schema changed cannot see
 * what it changed to, and the whole claim of this feature is that a person can
 * look at the two definitions and recognise the second as an impersonation.
 * Rendered as text, never as markup: these strings come from a tool registry any
 * script on the origin can write to.
 */
export function ToolSurfacePanel({ findings }: ToolSurfacePanelProps): React.ReactElement | null {
  const finding = findings.find((entry) => entry.checkId === "stable_tool_surface");
  if (finding === undefined) {
    return null;
  }

  if (finding.status === "observation_unavailable") {
    // §16.1's outcome, said in words. Distinct from "the surface was quiet",
    // and the difference is the whole reason the status exists.
    return (
      <section className="panel" aria-label="Tool surface">
        <h3>Tool surface</h3>
        <p>Not evaluated — no surface baseline was recorded for this run.</p>
      </section>
    );
  }

  const deltas = finding.surfaceDeltas;
  return (
    <section className="panel" aria-label="Tool surface">
      <h3>Tool surface</h3>
      <p>
        <span className="panel__label">Result:</span>{" "}
        <strong data-status={finding.status}>{finding.status}</strong>
        {finding.identityMismatches.length === 0 ? null : (
          <>
            {" "}
            — a tool&rsquo;s definition at invocation time did not match the armed baseline:{" "}
            {finding.identityMismatches.join(", ")}
          </>
        )}
      </p>
      {deltas.length === 0 ? (
        <p>No undeclared change to the target tool surface.</p>
      ) : (
        <ul>
          {deltas.map((delta) => (
            <li key={`${delta.toolName}:${delta.kind}`}>
              <strong>{delta.kind}</strong> — {delta.toolName}
              <dl className="tool-diff">
                <dt>armed</dt>
                <dd>{delta.before ?? "(not registered)"}</dd>
                <dt>observed</dt>
                <dd>{delta.after ?? "(no longer registered)"}</dd>
              </dl>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export interface ComparisonPanelProps {
  readonly comparable: boolean | null;
  readonly differingFields: readonly string[];
  readonly resolved: readonly string[];
  readonly introduced: readonly string[];
}

export function ComparisonPanel({
  comparable,
  differingFields,
  resolved,
  introduced,
}: ComparisonPanelProps): React.ReactElement {
  return (
    <section className="panel" aria-label="Comparison">
      <h3>Matched comparison</h3>
      {comparable === null ? (
        <p>This run was not armed against a comparison source.</p>
      ) : comparable ? (
        <>
          <p>These two runs differ only in scenario mode, so the comparison is valid.</p>
          <p>
            <span className="panel__label">Resolved:</span>{" "}
            {resolved.length === 0 ? "nothing" : resolved.join(", ")}
          </p>
          <p>
            <span className="panel__label">Introduced:</span>{" "}
            {introduced.length === 0 ? "nothing" : introduced.join(", ")}
          </p>
        </>
      ) : (
        // FR-019: a mismatched rerun stays a perfectly good run. Saying so
        // matters — otherwise someone will "fix" the mismatch by weakening
        // what they meant to test.
        <>
          <p>
            These runs cannot be compared. This is still a valid run with its own verdict; it
            simply is not the other one&apos;s counterpart.
          </p>
          <p>
            <span className="panel__label">Differs in:</span> {differingFields.join(", ")}
          </p>
        </>
      )}
    </section>
  );
}

const TERMINAL = ["passed", "passed_with_warnings", "failed", "error", "cancelled"];

function isTerminal(status: string): boolean {
  return TERMINAL.includes(status);
}

/**
 * Copy one content hash to the clipboard, saying what happened in words.
 *
 * The hash itself stays fully in the DOM (the row is identified by it, in
 * tests and by people); the visual shortening is CSS ellipsis only, and this
 * button is how the whole value travels. The outcome label is deterministic
 * state, not a timer: it flips on the copy's own promise and stays until the
 * next attempt, so nothing here depends on wall-clock time.
 */
function CopyHashButton({ hash }: { readonly hash: string }): React.ReactElement {
  const [outcome, setOutcome] = useState<"idle" | "copied" | "failed">("idle");

  const copy = async (): Promise<void> => {
    // `navigator.clipboard` is absent off secure contexts; that is a fact
    // about the browser and gets said as one, never swallowed.
    if (typeof navigator === "undefined" || navigator.clipboard === undefined) {
      setOutcome("failed");
      return;
    }
    try {
      await navigator.clipboard.writeText(hash);
      setOutcome("copied");
    } catch {
      setOutcome("failed");
    }
  };

  return (
    <button
      type="button"
      className="eval__copy"
      aria-label={`Copy content hash ${hash}`}
      onClick={() => void copy()}
    >
      {outcome === "idle" ? "Copy" : outcome === "copied" ? "Copied" : "Copy failed"}
    </button>
  );
}

export interface EvalCaseSummary {
  readonly evalCaseId: string;
  readonly name: string;
  readonly contentHash: string;
  readonly latestStatus: string | null;
  readonly latestOutcome: string | null;
  readonly latestEnvironment: string | null;
}

export interface EvalPanelProps {
  readonly cases: readonly EvalCaseSummary[];
  readonly busy: boolean;
  readonly canCreate: boolean;
  readonly onCreate: () => void;
  readonly onReplay: (evalCaseId: string, environment: string) => void;
}

/**
 * Regression eval cases and their last replay (§24, 007-T11).
 *
 * The panel's one hard rule: **it never merges eval status with business
 * outcome.** A reproduced failure is a *passing* eval whose target *failed*
 * (§24.3), and a UI that showed one number would report the product's best
 * evidence as a broken build. So both are rendered, each labelled, and the
 * pairing is spelled out in words rather than left to a colour.
 */
export function EvalPanel({
  cases,
  busy,
  canCreate,
  onCreate,
  onReplay,
}: EvalPanelProps): React.ReactElement {
  return (
    // `id` + `tabIndex={-1}`: the banner's `run_regression_eval` shortcut target.
    <section
      className="panel"
      aria-label="Regression evals"
      id="panel-regression-evals"
      tabIndex={-1}
    >
      <h3>Regression evals</h3>
      <p className="panel__note">
        A case turns this failure into a file CI can replay. Replaying against{" "}
        <strong>current</strong> should pass; replaying against{" "}
        <strong>reproduce source</strong> should reproduce the original failure — and
        reproducing it is a <em>passing</em> eval.
      </p>
      <button type="button" onClick={onCreate} disabled={busy || !canCreate}>
        Create a regression eval from this run
      </button>
      <ul>
        {cases.map((entry) => (
          <li key={entry.evalCaseId}>
            <strong>{entry.name}</strong>
            <div className="eval__hash">
              {/* The full hash, always in the DOM: shortening is CSS ellipsis
                  only, so the identity a row is found by never changes. */}
              <code title={entry.contentHash}>{entry.contentHash}</code>
              <CopyHashButton hash={entry.contentHash} />
            </div>
            {entry.latestStatus === null ? (
              <div>Not replayed yet.</div>
            ) : (
              <div>
                {/* Two labelled facts, never one merged verdict. The
                    `data-status` echoes the server's word so colour can
                    follow it; the word itself stays the signal (§8.4). */}
                <span className="panel__label">Eval:</span>{" "}
                <strong data-status={entry.latestStatus}>{entry.latestStatus}</strong>{" "}
                <span className="panel__label">Target outcome:</span>{" "}
                <strong data-status={entry.latestOutcome ?? undefined}>
                  {entry.latestOutcome ?? "not evaluated"}
                </strong>{" "}
                <span className="panel__label">Environment:</span> {entry.latestEnvironment}
              </div>
            )}
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                onReplay(entry.evalCaseId, "current");
              }}
            >
              Replay against current
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                onReplay(entry.evalCaseId, "reproduce_source");
              }}
            >
              Replay against source
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

export interface BenchmarkPanelProps {
  readonly benchmark: BenchmarkView | null;
  readonly busy: boolean;
  readonly onReplay: () => void;
  readonly onFinalize: () => void;
  /** Where the redacted per-trial evidence lives (FR-095's evidence links). */
  readonly trialHref: (externalTrialId: string) => string;
  readonly reportHref: string;
}

/** A rate, or the reason there is none (FR-092).
 *
 *  A null rate is rendered as words, never as `0.0000` and never as a blank
 *  cell. "No eligible trials" is a finding about coverage; a zero would be a
 *  measurement claim, and an empty cell would look like a rendering bug. */
function RateValue({ rate }: { readonly rate: Rate }): React.ReactElement {
  if (rate.value === null) {
    return <span className="benchmark__rate benchmark__rate--absent">no eligible trials</span>;
  }
  return (
    <span className="benchmark__rate">
      {rate.value}{" "}
      <span className="panel__label">
        ({rate.numerator}/{rate.denominator})
      </span>
    </span>
  );
}

/**
 * The dual-layer benchmark (§9.9, FR-095, AC-16, 008-T10).
 *
 * **The two layers are never merged into one score.** §9.9 says the benchmark
 * "measures correlation and incremental defect detection, not which evaluator
 * is universally better", and FR-095 forbids the phrases "deterministic
 * evaluator beat the probabilistic evaluator" and "accuracy comparison" in
 * product copy. So the matrix is four labelled cells with their interpretations
 * in words, and the copy says what the numbers mean rather than which layer won.
 *
 * **The source kind is prominent and literal.** AC-16 requires the application
 * to accept `external_import` or `recorded_fixture` and "never represent either
 * as a live execution". A recorded fixture says so at the top of the panel,
 * because that is the claim somebody would otherwise repeat in a talk.
 *
 * **Coverage is shown beside every rate.** A rate over two eligible trials and
 * a rate over two hundred read identically without it.
 */
export function BenchmarkPanel({
  benchmark,
  busy,
  onReplay,
  onFinalize,
  trialHref,
  reportHref,
}: BenchmarkPanelProps): React.ReactElement {
  if (benchmark === null) {
    return (
      <section className="panel" aria-label="Dual-layer benchmark">
        <h3>Dual-layer benchmark</h3>
        {/* FR-096: the module stays available and explains itself rather than
            disappearing when no report has been supplied. */}
        <p className="panel__note">
          No benchmark yet. Import a supported evaluator report to compare what a
          model <em>called</em> with what the business state actually <em>became</em>.
        </p>
      </section>
    );
  }

  const { counts, metrics, manifest } = benchmark;
  const insufficient = counts.eligibleTrials === 0;

  return (
    <section className="panel" aria-label="Dual-layer benchmark">
      <h3>Dual-layer benchmark</h3>

      {/* AC-16: source kind and correlation mode, prominent and never implied. */}
      <p className="benchmark__source">
        <span className="panel__label">Source:</span> <strong>{benchmark.sourceKind}</strong>{" "}
        {benchmark.sourceKind === "recorded_fixture" ? (
          <span className="benchmark__caveat">
            &mdash; a checked-in report. No model was called in this run.
          </span>
        ) : null}
        <br />
        <span className="panel__label">Correlation mode:</span>{" "}
        <strong>{benchmark.correlationMode}</strong>{" "}
        <span className="panel__label">Status:</span> <strong>{benchmark.status}</strong>
      </p>

      <p className="panel__note">
        Two layers answer two different questions over the same trials: whether the
        model made the required calls, and whether the resulting business state was
        valid. They are reported side by side because a trial can pass one and fail
        the other &mdash; and that disagreement is the point.
      </p>

      {insufficient ? (
        <p className="benchmark__insufficient" role="status">
          Benchmark insufficient sample &mdash; no trial had usable evidence on both
          layers, so no rate can be calculated. Coverage is below.
        </p>
      ) : null}

      <table className="benchmark__matrix">
        <caption>Trials by call-level result and business outcome</caption>
        <thead>
          <tr>
            <th scope="col">Call level</th>
            <th scope="col">Outcome</th>
            <th scope="col">Trials</th>
            <th scope="col">Reading</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">pass</th>
            <td>pass</td>
            <td>{counts.callLevelPassOutcomePass}</td>
            <td>Verified end to end.</td>
          </tr>
          <tr className="benchmark__row--signal">
            <th scope="row">pass</th>
            <td>fail</td>
            <td>{counts.callLevelPassOutcomeFail}</td>
            <td>Silent outcome defect &mdash; the calls looked right and the state did not.</td>
          </tr>
          <tr>
            <th scope="row">fail</th>
            <td>pass</td>
            <td>{counts.callLevelFailOutcomePass}</td>
            <td>Alternate path or an evaluator expectation mismatch; review.</td>
          </tr>
          <tr>
            <th scope="row">fail</th>
            <td>fail</td>
            <td>{counts.callLevelFailOutcomeFail}</td>
            <td>End-to-end failure.</td>
          </tr>
        </tbody>
      </table>

      <dl className="benchmark__coverage">
        <dt>Eligible</dt>
        <dd>{counts.eligibleTrials}</dd>
        <dt>Excluded</dt>
        <dd>
          {counts.excludedTrials}{" "}
          <span className="panel__label">(of which errors: {counts.errorTrials})</span>
        </dd>
        <dt>Total</dt>
        <dd>{counts.totalTrials}</dd>
      </dl>

      <dl className="benchmark__metrics">
        <dt>Call-level pass rate</dt>
        <dd>
          <RateValue rate={metrics.callLevelPassRate} />
        </dd>
        <dt>Outcome pass rate</dt>
        <dd>
          <RateValue rate={metrics.outcomePassRate} />
        </dd>
        <dt>End-to-end success rate</dt>
        <dd>
          <RateValue rate={metrics.endToEndSuccessRate} />
        </dd>
        <dt>Silent outcome failure rate</dt>
        <dd>
          <RateValue rate={metrics.silentOutcomeFailureRate} />
        </dd>
        <dt>Incremental outcome-failure trials</dt>
        <dd>{metrics.incrementalOutcomeFailureTrials}</dd>
      </dl>

      <h4>By scenario</h4>
      <ul className="benchmark__breakdown">
        {benchmark.byScenario.map((group) => (
          <li key={group.label}>
            <strong>{group.label}</strong>{" "}
            <span className="panel__label">eligible {group.counts.eligibleTrials}:</span>{" "}
            <RateValue rate={group.metrics.endToEndSuccessRate} />
          </li>
        ))}
      </ul>

      {benchmark.byFailureProfile.length > 0 ? (
        <>
          <h4>By failure profile</h4>
          <ul className="benchmark__breakdown">
            {benchmark.byFailureProfile.map((group) => (
              <li key={group.label}>
                <strong>{group.label}</strong>{" "}
                <span className="panel__label">eligible {group.counts.eligibleTrials}:</span>{" "}
                <RateValue rate={group.metrics.silentOutcomeFailureRate} />
              </li>
            ))}
          </ul>
        </>
      ) : null}

      {/* FR-093 reproducibility metadata. Absent fields say "not recorded"
          rather than being hidden: "we do not know" is itself information. */}
      <h4>Reproducibility</h4>
      <dl className="benchmark__manifest">
        <dt>Evaluator</dt>
        <dd>{manifest.evaluatorName ?? "not recorded"}</dd>
        <dt>Reporter schema</dt>
        <dd>{manifest.reporterSchema ?? "not recorded"}</dd>
        <dt>Normalizer</dt>
        <dd>{manifest.normalizedAdapterVersion ?? "not recorded"}</dd>
        <dt>Model</dt>
        <dd>
          {manifest.modelName ?? "not recorded"}
          {manifest.modelProvider === null ? "" : ` (${manifest.modelProvider})`}
        </dd>
        <dt>Target build</dt>
        <dd>{manifest.targetBuildCommit ?? "not recorded"}</dd>
      </dl>

      <h4>Trials</h4>
      <ul className="benchmark__trials">
        {benchmark.trials.map((trial) => (
          <li key={trial.externalTrialId}>
            {/* FR-095 evidence links: every number above is reachable back to
                the redacted trial it came from. */}
            <a href={trialHref(trial.externalTrialId)}>{trial.externalTrialId}</a>{" "}
            <span className="panel__label">call:</span> {trial.callLevelResult}{" "}
            <span className="panel__label">outcome:</span> {trial.outcomeResult}{" "}
            {trial.eligibility === "excluded" ? (
              <span className="benchmark__excluded">
                excluded &mdash; {trial.exclusionReason ?? "no reason recorded"}
              </span>
            ) : null}
            {trial.addressable ? null : (
              <span className="benchmark__unaddressable"> needs an explicit binding choice</span>
            )}
          </li>
        ))}
      </ul>

      <button type="button" onClick={onReplay} disabled={busy}>
        Replay imported trajectories
      </button>
      <button type="button" onClick={onFinalize} disabled={busy}>
        Finalize this benchmark
      </button>
      {benchmark.resultArtifactId === null ? null : (
        <a href={reportHref}>Download the benchmark report</a>
      )}
    </section>
  );
}
