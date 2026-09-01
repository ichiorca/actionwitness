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

import type { CapabilityReport, Finding, TimelineEvent, WorkspaceStatus } from "../api/workspace";

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
      <button type="button" onClick={onReset} disabled={busy}>
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
    <section className="panel" aria-label="Contract">
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
      <button type="button" onClick={onArm} disabled={busy || !canArm}>
        Arm the contract
      </button>
      <button type="button" onClick={onVerify} disabled={busy || !canVerify}>
        Verify the outcome
      </button>
    </section>
  );
}

export interface RunTimelineProps {
  readonly events: readonly TimelineEvent[];
  readonly runStatus: string | null;
  readonly polling: boolean;
}

export function RunTimeline({ events, runStatus, polling }: RunTimelineProps): React.ReactElement {
  return (
    <section className="panel" aria-label="Agent activity">
      <h3>Agent activity</h3>
      <p aria-live="polite">
        {runStatus === null
          ? "No run yet."
          : `Run is ${runStatus}${polling ? " — watching for new activity." : "."}`}
      </p>
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
    <section className="panel" aria-label="Findings">
      <h3>Findings</h3>
      {overallResult === null ? (
        <p>No verdict yet.</p>
      ) : (
        // Spelled out, never signalled by colour alone: a failed run has to
        // read as failed to someone who cannot see the red.
        <p>
          <span className="panel__label">Result:</span> <strong>{overallResult}</strong>
        </p>
      )}
      <ul>
        {findings.map((finding) => (
          <li key={finding.checkId}>
            <strong>{finding.status}</strong> — {finding.checkId}
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
