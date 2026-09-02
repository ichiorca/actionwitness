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

import { useCallback, useEffect, useState } from "react";

import { ApiError, request } from "./api/client";
import {
  type FindingsPage,
  type PendingConfirmation,
  parseFindings,
  parseRun,
} from "./api/workspace";
import { type ContractTemplateSummary, listContractTemplates } from "./api/contracts";
import { ConfirmationDialog } from "./components/ConfirmationDialog";
import { CREATE_CONTRACT_TOOL, ContractForm } from "./components/ContractForm";
import { GuidanceBanner } from "./components/GuidanceBanner";
import {
  CapabilityBar,
  ComparisonPanel,
  ConfigPanel,
  type ContractTemplate,
  ContractPanel,
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
  const harnessTools = useHarnessToolset(status, refresh);
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
  useToolSurfaceWitness(runId);
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
      // (no pair exists), never to an error the user has to dismiss.
      void request(`/runs/${runId}/comparison`, {
        parse: (value) => value as Record<string, unknown>,
        signal: controller.signal,
      }).then(
        (doc) => {
          if (live) {
            setComparison({
              comparable: doc["comparable"] === true,
              differingFields: (doc["differing_fields"] as readonly string[] | undefined) ?? [],
              resolved: (doc["resolved_classifications"] as readonly string[] | undefined) ?? [],
              introduced:
                (doc["introduced_classifications"] as readonly string[] | undefined) ?? [],
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

  return (
    <main className="workspace">
      <h1>ActionWitness</h1>

      <CapabilityBar
        capabilities={status?.capabilities ?? []}
        webMcpSupported={isWebMcpSupported()}
        registeredToolCount={reconciliation.count}
      />

      <GuidanceBanner guidance={status?.guidance ?? null} loading={loading} />

      {error === null ? null : <p role="alert">{error}</p>}
      {actionError === null ? null : <p role="alert">{actionError}</p>}

      {status === null ? null : (
        <div className="workspace__panels">
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
          />

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
