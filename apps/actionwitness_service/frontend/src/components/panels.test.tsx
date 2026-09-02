/**
 * 006-T9/T11 — the panels and the confirmation dialog (§8.4, §14, AC-06, AC-09, AC-21).
 *
 * Three properties are worth more than the rest.
 *
 * **The banner does not decide anything.** Every field comes from the server's
 * `GuidanceState`, including the case where there is no safe action. A panel
 * that filled that gap with a sensible default would be inventing the one thing
 * FR-120 says the frontend must not.
 *
 * **Nothing is preselected in the dialog.** §14.4 reads like an accessibility
 * detail and is a safety property: a focused default *Approve* is a consent
 * flow that a stray Enter key completes.
 *
 * **Status never depends on colour.** A failed run has to read as failed to
 * somebody who cannot see the red, so the words are asserted rather than the
 * class names.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Finding } from "../api/workspace";
import type { ToolGroupReconciliation, ToolReconciliation } from "../webmcp/adapter";
import { parseGuidance, parseWorkspace } from "../api/workspace";
import { ConfirmationDialog } from "./ConfirmationDialog";
import { GuidanceBanner } from "./GuidanceBanner";
import {
  CapabilityBar,
  ComparisonPanel,
  ConfigPanel,
  ToolRegistrationPanel,
  EvalPanel,
  RunTimeline,
  ToolSurfacePanel,
  UndeclaredChangesPanel,
  FindingsPanel,
} from "./panels";

const GUIDANCE = {
  phase: "awaiting_confirmation",
  active_actor: "human_approver",
  next_actor: "agent",
  headline: "A protected action is waiting on you.",
  instruction: "Approve once or deny the pending checkout.",
  reason: "The contract protects this tool with a confirmation policy.",
  expected_consequence: "Approving performs the checkout exactly once.",
  action_code: "decide_confirmation",
  recovery_action_code: "reset_workspace",
  waiting_for: "a human decision",
  requires_human_input: true,
};

function workspace(overrides: Record<string, unknown> = {}) {
  return parseWorkspace({
    workspace_id: "ws_1",
    selected_target_id: "buggy-store",
    selected_contract_id: "con_1",
    scenario_mode: "pre_fix",
    failure_profile: "discount_reported_but_not_applied",
    active_run: null,
    guidance: GUIDANCE,
    next_action: {
      actor: "human_approver",
      action_code: "decide_confirmation",
      instruction: "Approve once or deny the pending checkout.",
      requires_human_input: true,
    },
    capabilities: {},
    ...overrides,
  });
}

describe("GuidanceBanner (AC-21)", () => {
  it("names one actor, one action, the reason, and the consequence", () => {
    render(<GuidanceBanner guidance={parseGuidance(GUIDANCE)} loading={false} />);

    expect(screen.getByText(/A protected action is waiting on you/)).toBeDefined();
    expect(screen.getByText(/You \(approver\)/)).toBeDefined();
    expect(screen.getByText(/Approve once or deny/)).toBeDefined();
    expect(screen.getByText(/protects this tool/)).toBeDefined();
    expect(screen.getByText(/exactly once/)).toBeDefined();
    expect(screen.getByText(/a human decision/)).toBeDefined();
  });

  it("shows the recovery instruction when no safe action exists", () => {
    // §15.1: "if no safe action is possible, the primary action is omitted and
    // the recovery instruction explains why." A blank space here would read as
    // a loading state, which is the one thing it must not.
    render(
      <GuidanceBanner
        guidance={parseGuidance({ ...GUIDANCE, action_code: null })}
        loading={false}
      />,
    );

    expect(screen.getByText(/No safe action right now/)).toBeDefined();
  });

  it("publishes the action code every other surface must match", () => {
    render(<GuidanceBanner guidance={parseGuidance(GUIDANCE)} loading={false} />);

    // AC-21: the banner, the tool result, the enabled controls, and the
    // history all name this one code.
    expect(screen.getByTestId("banner-action-code").textContent).toBe("decide_confirmation");
  });

  it("announces changes politely rather than silently", () => {
    const { container } = render(
      <GuidanceBanner guidance={parseGuidance(GUIDANCE)} loading={false} />,
    );

    // Control moving between a person and an agent is exactly the change a
    // screen-reader user must not have to go looking for.
    expect(container.querySelector('[aria-live="polite"]')).not.toBeNull();
  });
});

describe("CapabilityBar (AC-09)", () => {
  it("reports a browser without WebMCP as a fact, not a failure", () => {
    render(<CapabilityBar capabilities={[]} webMcpSupported={false} registeredToolCount={0} />);

    expect(screen.getByText(/not available/)).toBeDefined();
    // The workspace below is fully usable, and the copy has to say so or a
    // person will reasonably assume it is not.
    expect(screen.getByText(/can still be done by hand/)).toBeDefined();
  });

  it("lists an unavailable target with the reason", () => {
    render(
      <CapabilityBar
        capabilities={[{ name: "buggy_store", status: "unavailable", reason: "not configured" }]}
        webMcpSupported
        registeredToolCount={3}
      />,
    );

    // A bar that listed only what worked would make a misconfiguration look
    // like a feature nobody built.
    expect(screen.getByText(/unavailable/)).toBeDefined();
    expect(screen.getByText(/not configured/)).toBeDefined();
  });
});

describe("ConfigPanel (FR-012)", () => {
  it("freezes configuration during a run and says why", () => {
    render(
      <ConfigPanel
        status={workspace({
          active_run: {
            id: "run_1",
            status: "running",
            target_id: "buggy-store",
            contract_id: "con_1",
            completed_at: null,
          },
        })}
        busy={false}
        onScenarioMode={vi.fn()}
        onFailureProfile={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    // Told why, rather than left to guess at a dead control.
    expect(screen.getByText(/Frozen while run run_1 is in progress/)).toBeDefined();
    expect(screen.getByRole("radio", { name: "pre_fix" }).closest("fieldset")?.disabled).toBe(true);
  });

  it("allows configuration when no run is in flight", () => {
    const onScenarioMode = vi.fn();
    render(
      <ConfigPanel
        status={workspace()}
        busy={false}
        onScenarioMode={onScenarioMode}
        onFailureProfile={vi.fn()}
        onReset={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("radio", { name: "post_fix" }));

    expect(onScenarioMode).toHaveBeenCalledWith("post_fix");
  });
});

describe("FindingsPanel", () => {
  it("states the result in words, not only in colour", () => {
    render(
      <FindingsPanel
        findings={[]}
        total={0}
        elided={0}
        reportPath={null}
        overallResult="failed"
      />,
    );

    // A failed run has to read as failed to someone who cannot see the red.
    expect(screen.getByText("failed")).toBeDefined();
  });

  it("says how many findings it left out", () => {
    render(
      <FindingsPanel
        findings={[
          {
            checkId: "c1",
            checkType: "assertion",
            status: "failed",
            severity: "critical",
            classification: "false_success_or_state_mismatch",
            path: "target.cart.total",
            paths: [],
            appliedExemptions: [],
            surfaceDeltas: [],
            identityMismatches: [],
            expected: "20.00",
            actual: "25.00",
          },
        ]}
        total={9}
        elided={8}
        reportPath="/api/v1/runs/run_1/report"
        overallResult="failed"
      />,
    );

    // A shortened list that did not say so reads as the whole list.
    expect(screen.getByText(/Showing 1 of 9/)).toBeDefined();
    expect(screen.getByText(/8 more/)).toBeDefined();
  });
});

describe("ComparisonPanel (FR-019)", () => {
  it("says a mismatched rerun is still a valid run", () => {
    render(
      <ComparisonPanel
        comparable={false}
        differingFields={["contract_content_hash"]}
        resolved={[]}
        introduced={[]}
      />,
    );

    // Otherwise somebody "fixes" the mismatch by weakening what they meant to
    // test, which is the opposite of the point.
    expect(screen.getByText(/still a valid run with its own verdict/)).toBeDefined();
    expect(screen.getByText(/contract_content_hash/)).toBeDefined();
  });

  it("names the resolved classification on a matched pair", () => {
    render(
      <ComparisonPanel
        comparable
        differingFields={[]}
        resolved={["false_success_or_state_mismatch"]}
        introduced={[]}
      />,
    );

    expect(screen.getByText(/false_success_or_state_mismatch/)).toBeDefined();
  });
});

describe("ConfirmationDialog (§14.4, AC-06)", () => {
  const pending = {
    confirmationId: "cnf_1",
    toolName: "proceed_to_checkout",
    expiresAt: "2026-01-01T00:01:00+00:00",
    consequence: { action: "proceed_to_checkout", affects: { cart: { total: "20.00" } } },
  };

  function renderDialog(overrides: Partial<Parameters<typeof ConfirmationDialog>[0]> = {}) {
    return render(
      <ConfirmationDialog
        pending={pending}
        owned
        busy={false}
        onApprove={vi.fn()}
        onDeny={vi.fn()}
        {...overrides}
      />,
    );
  }

  it("preselects neither choice", () => {
    renderDialog();

    // §14.4. A focused default Approve is a consent flow a stray Enter
    // completes — so focus lands on the dialog, not on a button.
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
    expect(document.activeElement).not.toBe(screen.getByRole("button", { name: /approve/i }));
  });

  it("states the action, the expiry, and what it affects in text", () => {
    renderDialog();

    expect(screen.getByText("proceed_to_checkout")).toBeDefined();
    // A countdown ring nobody can read is an expiry that will surprise them.
    expect(screen.getByText(/2026-01-01T00:01:00/)).toBeDefined();
    expect(screen.getByText(/"total": "20.00"/)).toBeDefined();
  });

  it("says plainly that nothing has changed yet", () => {
    renderDialog();

    // The single most important sentence in the dialog: it tells a person what
    // they are actually deciding.
    expect(screen.getByText(/Nothing has changed yet/)).toBeDefined();
  });

  it("is a modal dialog with an accessible name", () => {
    renderDialog();

    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-labelledby")).toBe("confirmation-title");
  });

  it("restores focus when it closes", () => {
    const opener = document.createElement("button");
    document.body.appendChild(opener);
    opener.focus();

    const { unmount } = renderDialog();
    expect(document.activeElement).not.toBe(opener);

    unmount();

    // Otherwise the caret jumps to the top of the document and a keyboard
    // user has to find their place again.
    expect(document.activeElement).toBe(opener);
    opener.remove();
  });

  it("offers no decision controls in a tab that is not waiting", () => {
    renderDialog({ owned: false });

    // §14.14: the same confirmation, but this tab holds no pending promise, so
    // a decision made here would resolve nothing.
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.getByText(/pending in another tab/)).toBeDefined();
  });

  it("passes the decision up rather than deciding anything itself", () => {
    const onApprove = vi.fn();
    const onDeny = vi.fn();
    renderDialog({ onApprove, onDeny });

    fireEvent.click(screen.getByRole("button", { name: /deny/i }));

    expect(onDeny).toHaveBeenCalledTimes(1);
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("disables both choices while the decision is recording", () => {
    renderDialog({ busy: true });

    // Two clicks would be two decisions on one request; the server refuses the
    // second, but a person should not be invited to make it.
    expect(screen.getByRole("button", { name: /approve/i }).getAttribute("disabled")).not.toBeNull();
    expect(screen.getByRole("button", { name: /deny/i }).getAttribute("disabled")).not.toBeNull();
  });

  it("traps Tab at the end, wrapping focus back to the first control", () => {
    // The dialog is the consent gate for an irreversible action (AGENTS.md:
    // confirmation tests must cover keyboard operation). A trap that only
    // caught forward Tab at one end and not the other would let a keyboard
    // user Tab straight out of the modal.
    renderDialog();

    const approve = screen.getByRole("button", { name: /approve once/i });
    const deny = screen.getByRole("button", { name: /deny/i });

    deny.focus();
    expect(document.activeElement).toBe(deny);

    // Dispatched on `document`, matching where the component itself
    // listens (`document.addEventListener("keydown", ...)`).
    fireEvent.keyDown(document, { key: "Tab" });

    expect(document.activeElement).toBe(approve);
  });

  it("traps Shift+Tab at the start, wrapping focus back to the last control", () => {
    renderDialog();

    const approve = screen.getByRole("button", { name: /approve once/i });
    const deny = screen.getByRole("button", { name: /deny/i });

    approve.focus();
    expect(document.activeElement).toBe(approve);

    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });

    expect(document.activeElement).toBe(deny);
  });
});

describe("EvalPanel (§24.3)", () => {
  const summary = {
    evalCaseId: "eval_1",
    name: "one-mug-save20",
    contentHash: "sha256:abc",
    latestStatus: "passed",
    latestOutcome: "failed",
    latestEnvironment: "reproduce_source",
  };

  // The clipboard stub must not leak between tests: the "no clipboard" case
  // below depends on jsdom's real absence of one, whatever the order.
  afterEach(() => {
    Reflect.deleteProperty(window.navigator, "clipboard");
  });

  it("never merges eval status with business outcome", () => {
    render(
      <EvalPanel
        cases={[summary]}
        busy={false}
        canCreate
        onCreate={vi.fn()}
        onReplay={vi.fn()}
      />,
    );

    // A reproduced failure is a *passing* eval whose target *failed*. One
    // merged number here would report the product's best evidence as a broken
    // build, which is the misreading §24.3 warns about.
    expect(screen.getByText("passed")).toBeDefined();
    expect(screen.getByText("failed")).toBeDefined();
    expect(screen.getByText(/Eval:/)).toBeDefined();
    expect(screen.getByText(/Target outcome:/)).toBeDefined();
  });

  it("says in words that reproducing a failure is a pass", () => {
    render(
      <EvalPanel cases={[]} busy={false} canCreate onCreate={vi.fn()} onReplay={vi.fn()} />,
    );

    // The one sentence that stops somebody filing the reproduction as a bug.
    expect(screen.getByText(/reproducing it is a/)).toBeDefined();
  });

  it("names the environment a replay ran against", () => {
    render(
      <EvalPanel
        cases={[summary]}
        busy={false}
        canCreate
        onCreate={vi.fn()}
        onReplay={vi.fn()}
      />,
    );

    // §24.4: a passing eval must not hide which environment produced it.
    expect(screen.getByText(/reproduce_source/)).toBeDefined();
  });

  it("offers both profiles and passes the chosen one up", () => {
    const onReplay = vi.fn();
    render(
      <EvalPanel cases={[summary]} busy={false} canCreate onCreate={vi.fn()} onReplay={onReplay} />,
    );

    fireEvent.click(screen.getByRole("button", { name: /replay against source/i }));

    expect(onReplay).toHaveBeenCalledWith("eval_1", "reproduce_source");
  });

  it("keeps the full content hash in the DOM and copies the whole value", async () => {
    // The row is identified by its hash — the e2e suite filters on the full
    // string, and a person selects it — so shortening must stay CSS-only.
    // The copy button is how the complete value travels.
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    render(
      <EvalPanel cases={[summary]} busy={false} canCreate onCreate={vi.fn()} onReplay={vi.fn()} />,
    );

    expect(screen.getByText("sha256:abc")).toBeDefined();

    const copy = screen.getByRole("button", { name: /copy content hash/i });
    fireEvent.click(copy);

    await waitFor(() => {
      expect(copy.textContent).toBe("Copied");
    });
    expect(writeText).toHaveBeenCalledWith("sha256:abc");
  });

  it("says the copy failed instead of pretending, when the browser has no clipboard", async () => {
    // jsdom (like any non-secure context) exposes no `navigator.clipboard`;
    // the button must say so rather than flash "Copied" over a no-op.
    render(
      <EvalPanel cases={[summary]} busy={false} canCreate onCreate={vi.fn()} onReplay={vi.fn()} />,
    );

    const copy = screen.getByRole("button", { name: /copy content hash/i });
    fireEvent.click(copy);

    await waitFor(() => {
      expect(copy.textContent).toBe("Copy failed");
    });
  });

  it("withholds creation when the run cannot produce a case", () => {
    render(
      <EvalPanel
        cases={[]}
        busy={false}
        canCreate={false}
        onCreate={vi.fn()}
        onReplay={vi.fn()}
      />,
    );

    // FR-080: a passing run has no failure to reproduce, so offering the
    // control would invite an action the server refuses.
    expect(
      screen.getByRole("button", { name: /create a regression eval/i }).getAttribute("disabled"),
    ).not.toBeNull();
  });
});


/**
 * 013-T6 — the "changed outside contract" panel.
 *
 * The panel is a projection of one server finding, and the tests below are
 * about the three states that are easy to render as each other: a clean run, a
 * run nobody checked, and a run whose waiver made it clean.
 */
describe("UndeclaredChangesPanel", () => {
  const finding = (over: Partial<Finding> = {}): Finding => ({
    checkId: "no_undeclared_changes",
    checkType: "policy",
    status: "failed",
    severity: "critical",
    classification: "undeclared_state_change",
    path: null,
    paths: ["target.preferences.delivery_note"],
    appliedExemptions: [],
    surfaceDeltas: [],
    identityMismatches: [],
    expected: null,
    actual: null,
    ...over,
  });

  it("lists every path the server said was undeclared", () => {
    render(<UndeclaredChangesPanel findings={[finding({ paths: ["target.a", "target.b"] })]} />);

    expect(screen.getByText("target.a")).toBeDefined();
    expect(screen.getByText("target.b")).toBeDefined();
  });

  it("states the outcome in words rather than by colour alone", () => {
    render(<UndeclaredChangesPanel findings={[finding()]} />);

    expect(screen.getByText("failed")).toBeDefined();
  });

  it("renders nothing when the contract carried no such policy", () => {
    // An empty "changed outside contract" heading would read as a clean result
    // for a check that never ran.
    const { container } = render(
      <UndeclaredChangesPanel findings={[finding({ checkId: "idempotent_by_request_id" })]} />,
    );

    expect(container.querySelector("section")).toBeNull();
  });

  it("distinguishes not-evaluated from nothing-changed", () => {
    // §16.1: an unevaluated policy must never read as a satisfied one, and this
    // is the surface where blurring the two would be easiest.
    render(
      <UndeclaredChangesPanel
        findings={[finding({ status: "not_evaluated", classification: null, paths: [] })]}
      />,
    );

    expect(screen.getByText(/Not evaluated/)).toBeDefined();
  });

  it("says nothing changed when the policy passed with no paths", () => {
    render(
      <UndeclaredChangesPanel
        findings={[finding({ status: "passed", classification: null, paths: [] })]}
      />,
    );

    expect(screen.getByText(/Nothing changed outside/)).toBeDefined();
  });

  it("shows an applied waiver even on a passing run", () => {
    // §9.5: "each exemption is recorded so the waiver is visible". A waiver is
    // most worth seeing precisely when it is the reason the run went green.
    render(
      <UndeclaredChangesPanel
        findings={[
          finding({
            status: "passed",
            classification: null,
            paths: [],
            appliedExemptions: ["target.cart.updated_at"],
          }),
        ]}
      />,
    );

    expect(screen.getByText(/target.cart.updated_at/)).toBeDefined();
  });
});

/**
 * 014-T6 — FR-169's side-by-side tool-definition diff.
 *
 * "The `stable_tool_surface` policy shall fail a run on any undeclared delta of
 * a configured kind... with a side-by-side diff of the tool definition before
 * and after as evidence."
 *
 * The diff is the evidence. A reader told only that a schema changed cannot see
 * what it changed to, and this feature's whole claim is that a person can look
 * at the two definitions and recognise the second as an impersonation.
 */
describe("ToolSurfacePanel", () => {
  const surfaceFinding = (over: Partial<Finding> = {}): Finding => ({
    checkId: "stable_tool_surface",
    checkType: "policy",
    status: "failed",
    severity: "critical",
    classification: "tool_surface_mutation",
    path: null,
    paths: [],
    appliedExemptions: [],
    surfaceDeltas: [
      {
        toolName: "apply_discount",
        kind: "description_change",
        before: '{"description": "Apply a discount code to the cart."}',
        after: '{"description": "[injected unsafe demo behaviour]"}',
      },
    ],
    identityMismatches: [],
    expected: null,
    actual: null,
    ...over,
  });

  it("shows both definitions side by side", () => {
    render(<ToolSurfacePanel findings={[surfaceFinding()]} />);

    expect(screen.getByText(/Apply a discount code to the cart/)).toBeDefined();
    expect(screen.getByText(/injected unsafe demo behaviour/)).toBeDefined();
  });

  it("names the delta kind and the tool it concerns", () => {
    render(<ToolSurfacePanel findings={[surfaceFinding()]} />);

    expect(screen.getByText("description_change")).toBeDefined();
    expect(screen.getByText(/apply_discount/)).toBeDefined();
  });

  it("reports an identity mismatch that produced no delta at all", () => {
    // FR-169 fails the policy on a mismatch "even if no toolchange event was
    // observed". A page that swapped a definition without announcing it is the
    // interesting case, and a panel showing an empty delta list would read as
    // a quiet surface.
    render(
      <ToolSurfacePanel
        findings={[surfaceFinding({ surfaceDeltas: [], identityMismatches: ["apply_discount"] })]}
      />,
    );

    expect(screen.getByText(/did not match the armed baseline/)).toBeDefined();
  });

  it("distinguishes an unrecorded baseline from a quiet surface", () => {
    render(
      <ToolSurfacePanel
        findings={[
          surfaceFinding({
            status: "observation_unavailable",
            classification: "observation_unavailable",
            surfaceDeltas: [],
          }),
        ]}
      />,
    );

    expect(screen.getByText(/no surface baseline was recorded/)).toBeDefined();
  });

  it("renders nothing when the contract carried no surface policy", () => {
    const { container } = render(
      <ToolSurfacePanel findings={[surfaceFinding({ checkId: "idempotent_by_request_id" })]} />,
    );

    expect(container.querySelector("section")).toBeNull();
  });
});

/**
 * FR-003's registration status (012-T6).
 *
 * The panel reports a reconciliation and decides nothing. The property worth
 * guarding is the last one: an undeclared tool is *named*, and the copy points
 * at `stable_tool_surface` for the verdict. A panel that called an extra tool
 * acceptable would be a second, softer opinion about exactly what that policy
 * exists to judge.
 */
describe("ToolRegistrationPanel", () => {
  const group = (
    declared: string[],
    present: string[],
    missing: string[] = [],
  ): ToolGroupReconciliation => ({ declared, claimed: present, present, missing });

  const reconciliation = (
    overrides: Partial<ToolReconciliation> = {},
  ): ToolReconciliation => ({
    supported: true,
    count: 2,
    harness: group(["verify_outcome"], ["verify_outcome"]),
    target: group(["update_cart"], ["update_cart"]),
    unexpected: [],
    ...overrides,
  });

  it("reports the number the browser says is available", () => {
    // Arrange / Act
    render(<ToolRegistrationPanel reconciliation={reconciliation()} />);

    // Assert
    expect(screen.getByText(/Reported by the browser/)).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
  });

  it("answers for harness and target tools separately", () => {
    // Arrange / Act — FR-003 asks about both, and they fail for different
    // reasons: a missing harness tool is a defect in this app, a missing target
    // tool is usually the workspace phase doing its job.
    render(<ToolRegistrationPanel reconciliation={reconciliation()} />);

    // Assert
    expect(screen.getByText("Harness tools:")).toBeTruthy();
    expect(screen.getByText("Target tools:")).toBeTruthy();
  });

  it("names a tool this app claimed that the browser does not report", () => {
    // Arrange — the disagreement FR-003 exists to surface. Mount state alone
    // would call this registered.
    const view = reconciliation({
      harness: group(["verify_outcome", "arm_outcome_contract"], [], ["verify_outcome"]),
    });

    // Act
    render(<ToolRegistrationPanel reconciliation={view} />);

    // Assert
    expect(screen.getByText(/claimed but not reported: verify_outcome/)).toBeTruthy();
  });

  it("names an undeclared tool and leaves the verdict to the policy", () => {
    // Arrange
    const view = reconciliation({ unexpected: ["proceed_to_checkout_v2"] });

    // Act
    render(<ToolRegistrationPanel reconciliation={view} />);

    // Assert — both halves matter. Naming it without pointing at the policy
    // invites the reader to treat this panel as the judgement; pointing at the
    // policy without naming it leaves them nothing to look at.
    expect(screen.getByText(/proceed_to_checkout_v2/)).toBeTruthy();
    expect(screen.getByText(/stable_tool_surface/)).toBeTruthy();
  });

  it("says nothing about undeclared tools when there are none", () => {
    // Arrange / Act — a standing warning that is always present is one nobody
    // reads when it finally matters.
    render(<ToolRegistrationPanel reconciliation={reconciliation()} />);

    // Assert
    expect(screen.queryByText(/Not declared by this page/)).toBeNull();
  });

  it("reports an absent WebMCP as a fact about the browser", () => {
    // Arrange
    const view = reconciliation({
      supported: false,
      count: 0,
      harness: group([], []),
      target: group([], []),
    });

    // Act
    render(<ToolRegistrationPanel reconciliation={view} />);

    // Assert — AC-09: the workspace still works, and the copy has to say so or
    // a person will reasonably assume it does not.
    expect(screen.getByText(/can be done by hand/)).toBeTruthy();
  });
});

describe("RunTimeline", () => {
  const EVENT = {
    id: "evt_1",
    sequenceNumber: 1,
    eventType: "run_armed",
    actor: "human",
    toolName: null,
    status: null,
    reportedStatus: null,
    createdAt: "2026-06-01T12:00:00Z",
  };

  it("says a run is being watched while it is live", () => {
    render(<RunTimeline events={[EVENT]} runStatus="armed" polling error={null} />);

    expect(screen.getByText(/watching for new activity/)).toBeDefined();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says out loud that the timeline could not be read", () => {
    // `useRunTimeline` has always computed this and nothing rendered it, so a
    // dropped connection looked exactly like a quiet run: the events froze, the
    // banner still said the page was watching, and there was no way to tell.
    render(
      <RunTimeline
        events={[EVENT]}
        runStatus="armed"
        polling
        error="The timeline could not be read."
      />,
    );

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("The timeline could not be read.");
    // A statement about the connection, not about the run: the hook keeps
    // retrying and keeps what it already has.
    expect(alert.textContent).toContain("keeps retrying");
  });

  it("keeps the events it already had while the connection is down", () => {
    // A failed poll must not rewind the record in front of the reader, which is
    // the reason the message says the list may be behind rather than gone.
    render(
      <RunTimeline
        events={[EVENT]}
        runStatus="armed"
        polling
        error="The timeline could not be read."
      />,
    );

    expect(screen.getByText("run_armed")).toBeDefined();
  });

  it("labels the tool's own claim as a claim", () => {
    // §23.1 keeps the self-report and the observation distinguishable; a
    // timeline showing one status would hide the disagreement a run is judged on.
    render(
      <RunTimeline
        events={[{ ...EVENT, id: "evt_2", eventType: "tool_invocation_completed", actor: "agent", toolName: "apply_discount", reportedStatus: "success" }]}
        runStatus="running"
        polling
        error={null}
      />,
    );

    expect(screen.getByText(/reported: success/)).toBeDefined();
  });
});
