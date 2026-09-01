/**
 * 006-T6 — `get_workspace_status` (§11.1, AC-21, AC-22).
 *
 * The property under test is the one AC-21 turns on: the tool result and the
 * banner name the same action code, because both read one server derivation.
 * The way that breaks in practice is a tool that answers from cached page state
 * — fast, and wrong exactly when an agent asks, since it asks when something has
 * changed underneath it. `test_it_rereads_the_server_rather_than_reporting_page_state`
 * is the test that would fail if someone made that optimisation.
 */

import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type InstalledDouble, installModelContextDouble } from "../test/modelContextDouble";
import { GET_WORKSPACE_STATUS, useWorkspaceStatusTool } from "./workspaceStatus";

let installed: InstalledDouble | null = null;

const WORKSPACE = {
  workspace_id: "ws_1",
  selected_target_id: "buggy-store",
  selected_contract_id: "con_1",
  scenario_mode: "pre_fix",
  failure_profile: "discount_reported_but_not_applied",
  active_run: {
    id: "run_1",
    status: "running",
    target_id: "buggy-store",
    contract_id: "con_1",
    completed_at: null,
  },
  guidance: {
    phase: "running",
    active_actor: "agent",
    next_actor: null,
    headline: "The agent is running the journey.",
    instruction: "Perform the stated journey, then verify.",
    reason: "The contract is armed and the run is open.",
    expected_consequence: "The timeline records each action.",
    action_code: "invoke_target_tool",
    recovery_action_code: null,
    waiting_for: null,
    requires_human_input: false,
  },
  next_action: {
    actor: "agent",
    action_code: "invoke_target_tool",
    instruction: "Perform the stated journey, then verify.",
    requires_human_input: false,
  },
  capabilities: { buggy_store: { status: "available", reason: "" } },
};

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
  respondWith(WORKSPACE);
});

afterEach(() => {
  installed?.uninstall();
  installed = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function registered(): Promise<void> {
  const { result } = renderHook(() => useWorkspaceStatusTool());
  await waitFor(() => expect(result.current.phase).toBe("registered"));
}

describe("get_workspace_status", () => {
  it("registers natively and is always available", async () => {
    await registered();

    // §11.1: "Always". A workspace with no contract still has a state worth
    // reporting — it is the answer an agent needs to know what to do first.
    expect(installed?.modelContext.toolNames).toEqual([GET_WORKSPACE_STATUS]);
  });

  it("declares itself read-only", async () => {
    await registered();

    const [tool] = (await installed?.modelContext.getTools()) ?? [];
    expect(tool?.annotations).toMatchObject({ readOnlyHint: true });
  });

  it("takes no arguments, so a caller cannot name another workspace", async () => {
    await registered();

    const [tool] = (await installed?.modelContext.getTools()) ?? [];
    // §20.1 makes the session cookie the only authorization input. A
    // `workspace_id` argument would be a boundary a caller could widen.
    expect(tool?.inputSchema).toMatchObject({ properties: {}, additionalProperties: false });
  });

  it("reports the server's action code, not one it derived", async () => {
    await registered();

    const outcome = await installed?.modelContext.invoke(GET_WORKSPACE_STATUS);
    const text = JSON.parse(
      (outcome as { content: { text: string }[] }).content[0]?.text ?? "{}",
    ) as Record<string, unknown>;

    expect(text["phase"]).toBe("running");
    expect(text["next_action"]).toMatchObject({
      actor: "agent",
      action_code: "invoke_target_tool",
      requires_human_input: false,
    });
  });

  it("rereads the server rather than reporting page state", async () => {
    await registered();
    await installed?.modelContext.invoke(GET_WORKSPACE_STATUS);

    // The workspace moves on — a human approved something, say.
    respondWith({
      ...WORKSPACE,
      guidance: { ...WORKSPACE.guidance, phase: "awaiting_confirmation", action_code: "decide_confirmation" },
      next_action: { ...WORKSPACE.next_action, action_code: "decide_confirmation" },
    });

    const outcome = await installed?.modelContext.invoke(GET_WORKSPACE_STATUS);
    const text = JSON.parse(
      (outcome as { content: { text: string }[] }).content[0]?.text ?? "{}",
    ) as Record<string, unknown>;

    // An agent asks whose turn it is precisely when something has changed
    // underneath it, so a cached answer is wrong exactly when it is needed.
    expect(text["phase"]).toBe("awaiting_confirmation");
  });

  it("reports a refusal as isError rather than throwing at the agent", async () => {
    await registered();
    respondWith(
      { error: { code: "AUDIT_NOT_AUTHORIZED", message: "not your workspace", retryable: false } },
      403,
    );

    const outcome = await installed?.modelContext.invoke(GET_WORKSPACE_STATUS);

    expect(outcome).toMatchObject({ isError: true });
    expect(JSON.stringify(outcome)).toContain("not your workspace");
  });

  it("stays within the tool result budget", async () => {
    await registered();

    const outcome = await installed?.modelContext.invoke(GET_WORKSPACE_STATUS);
    const text = (outcome as { content: { text: string }[] }).content[0]?.text ?? "";

    // §23.3: identifiers and the next action, never evidence. Full detail
    // lives in the UI and the workspace-scoped endpoints.
    expect(text.length).toBeLessThanOrEqual(1_500);
  });
});
