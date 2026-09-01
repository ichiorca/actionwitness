/**
 * 006-T7 — the hook-registered harness tools (§11.1, §11.5, AC-22).
 *
 * The behaviour under test is §11.5's: **the visible tool set changes with the
 * workspace phase, and the phase comes from the server.** Two tests carry that:
 * one walks the phases and asserts which tools exist in each, and
 * `test_enablement_follows_the_servers_phase_not_a_local_guess` changes only the
 * server's answer and expects the registered set to follow.
 *
 * The third property is that enablement is a *hint*. A tool that is enabled and
 * then refused by the server is correct — the server is the authority, and a UI
 * that treated its own flag as the rule would have to be right about races it
 * cannot see. `test_an_enabled_tool_still_reports_a_server_refusal` pins that.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type InstalledDouble, installModelContextDouble } from "../test/modelContextDouble";
import { parseWorkspace } from "../api/workspace";
import { useHarnessToolset } from "./harnessTools";

let installed: InstalledDouble | null = null;

function workspace(phase: string, overrides: Record<string, unknown> = {}) {
  return parseWorkspace({
    workspace_id: "ws_1",
    selected_target_id: "buggy-store",
    selected_contract_id: phase === "no_contract" ? null : "con_1",
    scenario_mode: "pre_fix",
    failure_profile: null,
    active_run:
      phase === "no_contract" || phase === "contract_ready"
        ? null
        : { id: "run_1", status: phase, target_id: "buggy-store", contract_id: "con_1", completed_at: null },
    guidance: {
      phase,
      active_actor: "agent",
      next_actor: null,
      headline: "h",
      instruction: "i",
      reason: "r",
      expected_consequence: "e",
      action_code: "invoke_target_tool",
      recovery_action_code: null,
      waiting_for: null,
      requires_human_input: false,
    },
    next_action: {
      actor: "agent",
      action_code: "invoke_target_tool",
      instruction: "i",
      requires_human_input: false,
    },
    capabilities: {},
    ...overrides,
  });
}

function respondWith(body: unknown, status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(body), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
}

beforeEach(() => {
  installed = installModelContextDouble();
  respondWith({});
});

afterEach(() => {
  installed?.uninstall();
  installed = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const noop = async (): Promise<void> => undefined;

async function toolsFor(phase: string): Promise<string[]> {
  const { result, unmount } = renderHook(() => useHarnessToolset(workspace(phase), noop));
  await waitFor(() =>
    expect(result.current.states["list_contract_templates"]?.phase).toBe("registered"),
  );
  const names = [...(installed?.modelContext.toolNames ?? [])].sort();
  // Unmounted before returning: two `toolsFor` calls in one test would
  // otherwise leave both sets registered, and the second assertion would see
  // the first phase's tools.
  unmount();
  await waitFor(() => expect(installed?.modelContext.toolNames).toEqual([]));
  return names;
}

describe("state-dependent registration (§11.5)", () => {
  it("always publishes the template list, even with nothing selected", async () => {
    // An agent arriving at an empty workspace has to be able to find out what
    // contracts exist, or it cannot take the first step at all.
    expect(await toolsFor("no_contract")).toContain("list_contract_templates");
  });

  it("publishes arming only once a contract is selected and no run is active", async () => {
    expect(await toolsFor("contract_ready")).toContain("arm_outcome_contract");
  });

  it("withdraws arming while a run is in flight", async () => {
    // Offering it would invite an agent to arm a second run, which FR-039
    // refuses — an action a tool offers and the server rejects reads as a bug.
    expect(await toolsFor("running")).not.toContain("arm_outcome_contract");
  });

  it("publishes verification only while a run is running", async () => {
    expect(await toolsFor("running")).toContain("verify_outcome");
    expect(await toolsFor("contract_ready")).not.toContain("verify_outcome");
  });

  it("withdraws verification while a human is deciding", async () => {
    // §11.5 gives `AwaitingConfirmation` status only. Verifying over an
    // unresolved confirmation is exactly what FR-039 blocks.
    expect(await toolsFor("awaiting_confirmation")).not.toContain("verify_outcome");
  });

  it("publishes findings once a run reaches any terminal state", async () => {
    for (const phase of ["passed", "failed", "error", "cancelled"]) {
      expect(await toolsFor(phase)).toContain("get_run_findings");
    }
  });

  it("does not publish findings for a run still in flight", async () => {
    expect(await toolsFor("running")).not.toContain("get_run_findings");
  });

  it("publishes reset once a run is terminal, so an agent can retry", async () => {
    // FR-152 instructs an agent to reset and retry; an instruction with no tool
    // to obey it is a defect rather than guidance.
    expect(await toolsFor("failed")).toContain("reset_workspace");
  });

  it("withdraws reset while a run is in flight", async () => {
    expect(await toolsFor("running")).not.toContain("reset_workspace");
  });
});

describe("the server decides", () => {
  it("follows the server's phase rather than a local guess", async () => {
    const { result, rerender } = renderHook(
      ({ phase }: { phase: string }) => useHarnessToolset(workspace(phase), noop),
      { initialProps: { phase: "contract_ready" } },
    );
    await waitFor(() =>
      expect(result.current.states["arm_outcome_contract"]?.phase).toBe("registered"),
    );
    expect(installed?.modelContext.toolNames).toContain("arm_outcome_contract");

    // Only the server's answer changes.
    rerender({ phase: "running" });

    await waitFor(() =>
      expect(installed?.modelContext.toolNames).not.toContain("arm_outcome_contract"),
    );
    expect(installed?.modelContext.toolNames).toContain("verify_outcome");
  });

  it("still reports a server refusal from an enabled tool", async () => {
    const { result } = renderHook(() => useHarnessToolset(workspace("running"), noop));
    await waitFor(() => expect(result.current.states["verify_outcome"]?.phase).toBe("registered"));

    // An in-flight invocation is not visible in the phase, so the tool is
    // enabled and the server refuses. That is correct: enablement is a hint,
    // and a UI that treated its own flag as the rule would have to be right
    // about races it cannot see.
    respondWith(
      { error: { code: "PRECONDITION_FAILED", message: "an invocation is still in flight", retryable: false } },
      409,
    );

    const outcome = await installed?.modelContext.invoke("verify_outcome");

    expect(outcome).toMatchObject({ isError: true });
    expect(JSON.stringify(outcome)).toContain("still in flight");
  });

  it("registers nothing at all without a workspace", async () => {
    // Before the first load there is no authoritative state, and guessing one
    // would publish tools against a workspace nobody has read yet.
    const { result } = renderHook(() => useHarnessToolset(null, noop));

    await waitFor(() =>
      expect(result.current.states["list_contract_templates"]?.phase).toBe("registered"),
    );
    // Only the always-available reader; nothing that acts.
    expect(installed?.modelContext.toolNames).toEqual(["list_contract_templates"]);
  });
});

describe("budgets", () => {
  it("gives findings the larger budget §11.4 grants it", async () => {
    respondWith({
      run_id: "run_1",
      overall_result: "failed",
      findings: [
        {
          check_id: "c1",
          check_type: "assertion",
          status: "failed",
          severity: "critical",
          classification: "false_success_or_state_mismatch",
          path: "target.cart.total",
          expected: "x".repeat(400),
          actual: "y".repeat(400),
        },
      ],
      returned: 1,
      total: 9,
      failed: 1,
      elided: 8,
      report: "/api/v1/runs/run_1/report",
    });

    const { result } = renderHook(() => useHarnessToolset(workspace("failed"), noop));
    await waitFor(() => expect(result.current.states["get_run_findings"]?.phase).toBe("registered"));

    const outcome = await installed?.modelContext.invoke("get_run_findings");
    const text = (outcome as { content: { text: string }[] }).content[0]?.text ?? "";

    expect(text.length).toBeGreaterThan(800);
    expect(text.length).toBeLessThanOrEqual(4_000);
    // The untruncated total travels with the bounded list, or an agent reading
    // one finding would conclude there was one.
    expect(text).toContain('"total":9');
  });
});
