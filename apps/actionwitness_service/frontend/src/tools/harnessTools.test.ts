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

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type InstalledDouble, installModelContextDouble } from "../test/modelContextDouble";
import { parseWorkspace } from "../api/workspace";
import { type HarnessToolsetOptions, useHarnessToolset } from "./harnessTools";

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

/** One request the toolset made, as the assertions below need to see it. */
interface RecordedCall {
  readonly url: string;
  readonly body: string | null;
}

/**
 * Stub `fetch` and keep what it was asked for.
 *
 * `respondWith` proves a handler survived a response; this proves it addressed
 * the right endpoint. The `get_outcome_contract` defect was invisible to every
 * existing test precisely because none of them looked at the URL.
 */
function recordFetch(body: unknown = {}, status = 200): RecordedCall[] {
  const calls: RecordedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: typeof init?.body === "string" ? init.body : null });
      return new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

/** The text an agent actually receives from a normalized tool result. */
function textOf(outcome: unknown): string {
  return (outcome as { content?: { text?: string }[] }).content?.[0]?.text ?? "";
}

async function toolsetFor(
  phase: string,
  options: HarnessToolsetOptions = {},
): Promise<{ invoke: (name: string, args?: Record<string, unknown>) => Promise<unknown> }> {
  const { result } = renderHook(() => useHarnessToolset(workspace(phase), noop, options));
  await waitFor(() =>
    expect(result.current.states["list_contract_templates"]?.phase).toBe("registered"),
  );
  return {
    invoke: async (name, args = {}) => {
      await waitFor(() => expect(result.current.states[name]?.phase).toBe("registered"));
      return await installed?.modelContext.invoke(name, args);
    },
  };
}

async function registeredTool(
  name: string,
  phase: string,
  options: HarnessToolsetOptions = {},
): Promise<WebMCP.RegisteredTool | null> {
  const { result, unmount } = renderHook(() => useHarnessToolset(workspace(phase), noop, options));
  await waitFor(() => expect(result.current.states[name]?.phase).toBe("registered"));
  const tools = await (installed?.modelContext.getTools() ?? Promise.resolve([]));
  const found = tools.find((tool) => tool.name === name) ?? null;
  // Unmounted before returning, for the same reason `toolsFor` does it: a
  // second render in the same test would otherwise see the first one's tools.
  unmount();
  await waitFor(() => expect(installed?.modelContext.toolNames).toEqual([]));
  return found;
}

async function toolsFor(phase: string, options: HarnessToolsetOptions = {}): Promise<string[]> {
  const { result, unmount } = renderHook(() => useHarnessToolset(workspace(phase), noop, options));
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

describe("evidence is on the record before a verdict is taken", () => {
  it("awaits the surface flush before posting the verification", async () => {
    // Verification seals the timeline, and the tool-surface witness is debounced
    // and asynchronous. Without this ordering a delta read a moment before
    // `verify_outcome` is posted a moment after it, meets a sealed timeline, and
    // never reaches the verdict it was evidence for.
    const order: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        order.push(`fetch:${url}`);
        return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
      }),
    );
    const beforeVerify = async (): Promise<void> => {
      order.push("flush");
    };

    const { result } = renderHook(() =>
      useHarnessToolset(workspace("running"), noop, { beforeVerify }),
    );
    await waitFor(() => expect(result.current.states["verify_outcome"]?.phase).toBe("registered"));

    await installed?.modelContext.invoke("verify_outcome");

    expect(order[0]).toBe("flush");
    expect(order[1]).toBe("fetch:/api/v1/runs/run_1/verify");
  });

  it("verifies with no flusher supplied", async () => {
    // The option is optional, and a caller that supplies none must not have its
    // verification blocked on a hook that does not exist.
    const { result } = renderHook(() => useHarnessToolset(workspace("running"), noop));
    await waitFor(() => expect(result.current.states["verify_outcome"]?.phase).toBe("registered"));

    const outcome = await installed?.modelContext.invoke("verify_outcome");

    expect((outcome as { isError?: boolean }).isError).toBeFalsy();
  });
});

/**
 * Appendix D, transcribed.
 *
 * D.1 is normative for names, descriptions, required fields and enum values,
 * and until this table existed nothing checked any of it — which is exactly how
 * `arm_outcome_contract` came to publish a `comparison_source_run_id` and
 * neither of D.1's two properties, and how `verify_outcome`,
 * `create_regression_eval` and `run_regression_eval` came to take no arguments
 * at all. The table is deliberately a *copy* rather than an import from the
 * source: a test that read the schema it is checking would agree with any drift.
 *
 * Three departures from D.1 are pinned here on purpose, each because D.1
 * describes a server this build does not have. They are asserted rather than
 * excused, so changing one has to be a decision:
 *
 * 1. D.1's `required` arrays are absent — the arguments are accepted, and the
 *    workspace's own active run or selected case stands in when they are not
 *    supplied. See `it("accepts Appendix D's identifiers without requiring them")`.
 * 2. `arm_outcome_contract`'s `mode` enum publishes only `verification`, and
 *    `create_regression_eval` publishes no `name`. The server refuses the first
 *    and cannot receive the second.
 * 3. `get_run_findings`'s `limit` is 1–10 defaulting to 3, which is what §11.4
 *    and `findings_service.py` say. D.1 says 1–25 defaulting to 10.
 */
interface AppendixDProperty {
  readonly description: string;
  readonly [key: string]: unknown;
}

interface AppendixDSchema {
  readonly type: "object";
  readonly properties: Readonly<Record<string, AppendixDProperty>>;
  readonly additionalProperties: false;
}

interface AppendixDTool {
  readonly name: string;
  /** A phase in which §11.5 publishes this tool, so it can be read back. */
  readonly phase: string;
  readonly options?: HarnessToolsetOptions;
  readonly description: string;
  readonly inputSchema: AppendixDSchema;
}

/** D.1's shared identifier bounds. */
const ID = { type: "string", minLength: 8, maxLength: 80 } as const;

const APPENDIX_D: readonly AppendixDTool[] = [
  {
    name: "list_contract_templates",
    phase: "contract_ready",
    description:
      "List the built-in outcome-contract templates and the flat parameters each template accepts.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "get_outcome_contract",
    phase: "contract_ready",
    // D.1 verbatim except for "publishable", which names a proposal-mode
    // concept this build does not implement.
    description:
      "Return the active outcome contract selected for this workspace so you can learn " +
      "what this site expects of an agent before acting. Reading a contract grants no " +
      "permission and is not evidence of compliance.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
  {
    name: "arm_outcome_contract",
    phase: "contract_ready",
    description:
      "Arm one immutable contract, validate its preconditions, capture initial " +
      "authoritative state, and return a new run identifier.",
    inputSchema: {
      type: "object",
      properties: {
        contract_id: {
          ...ID,
          description: "Immutable contract to arm. Defaults to the one this workspace selected.",
        },
        mode: {
          type: "string",
          // D.1 also lists "proposal"; `run_service` refuses it by name.
          enum: ["verification"],
          default: "verification",
          description: "Verify an existing contract. Only verification runs exist in this build.",
        },
        // Beyond D.1, and kept: §15.3 defines it, the service validates it at
        // arming, and 07-matched-comparison drives a matched pair through it.
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
  },
  {
    name: "verify_outcome",
    phase: "running",
    description:
      "Freeze the active journey, capture final authoritative state, evaluate its outcome " +
      "contract, and return the layered verdict summary.",
    inputSchema: {
      type: "object",
      properties: {
        run_id: {
          ...ID,
          description: "Active outcome run to verify. Defaults to this workspace's active run.",
        },
      },
      additionalProperties: false,
    },
  },
  {
    name: "create_regression_eval",
    phase: "failed",
    description:
      "Create or return the deterministic regression eval for one failed or " +
      "warning-bearing outcome run.",
    inputSchema: {
      type: "object",
      properties: {
        run_id: {
          ...ID,
          description:
            "Terminal source run to generate the eval from. Defaults to this workspace's run.",
        },
        // D.1's optional `name` is absent: POST /runs/{id}/evals accepts no
        // body, so a name would be dropped rather than applied.
      },
      additionalProperties: false,
    },
  },
  {
    name: "run_regression_eval",
    phase: "failed",
    options: { evalCaseId: "eval_selected" },
    description:
      "Replay one built-in eval in an isolated workspace and compare its outcome and " +
      "exact critical classifications with the selected environment expectation.",
    inputSchema: {
      type: "object",
      properties: {
        eval_case_id: {
          ...ID,
          description: "Eval case to run. Defaults to the case this workspace has selected.",
        },
        environment: {
          type: "string",
          enum: ["current", "reproduce_source"],
          default: "current",
          description: "Use current corrected logic or explicitly reproduce the source fault.",
        },
      },
      additionalProperties: false,
    },
  },
  {
    name: "get_run_findings",
    phase: "failed",
    description:
      "Return structured findings for one completed run so you can act on the result: " +
      "overall status, layered results, counts, and each failed check with its path, " +
      "expected value, and actual value. Call this after verify_outcome to learn what " +
      "actually happened before reporting success to your user.",
    inputSchema: {
      type: "object",
      properties: {
        run_id: {
          ...ID,
          description: "Terminal run to read findings for. Defaults to this workspace's run.",
        },
        limit: {
          type: "integer",
          // §11.4 and `findings_service.py`, not D.1's 1–25 default 10.
          minimum: 1,
          maximum: 10,
          default: 3,
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
  },
  {
    name: "reset_workspace",
    phase: "failed",
    description:
      "Return this workspace to a ready state so you can start a fresh attempt. Use this " +
      "when findings tell you to reset and retry. It cancels nonterminal work, keeps " +
      "completed artifacts, and never touches another workspace.",
    inputSchema: { type: "object", properties: {}, additionalProperties: false },
  },
];

describe("the published surface matches Appendix D (D.1)", () => {
  for (const expected of APPENDIX_D) {
    it(`publishes ${expected.name} with D.1's description and input schema`, async () => {
      // Arrange / Act — read the schema back from the browser's own registry
      // rather than from the module, so what is asserted is what an agent
      // discovering this page would actually be handed.
      const tool = await registeredTool(expected.name, expected.phase, expected.options ?? {});

      // Assert
      expect(tool).not.toBeNull();
      expect(tool?.description).toBe(expected.description);
      expect(tool?.inputSchema).toEqual(expected.inputSchema);
    });
  }

  it("registers every tool §11.1 assigns to this hook, and no others", async () => {
    // Arrange — the phases that between them publish the whole set. The failed
    // phase carries a selected case, because §11.1 gates `run_regression_eval`
    // on one existing rather than on the run's phase alone.
    const published = new Set<string>([
      ...(await toolsFor("contract_ready")),
      ...(await toolsFor("running")),
      ...(await toolsFor("failed", { evalCaseId: "eval_selected" })),
    ]);

    // Assert — a tool added here without an Appendix D entry is drift in the
    // other direction, and just as unreviewed.
    expect([...published].sort()).toEqual(APPENDIX_D.map((tool) => tool.name).sort());
  });

  it("accepts Appendix D's identifiers without requiring them", async () => {
    // D.1 marks `run_id` and `eval_case_id` required. They are accepted and
    // optional: the workspace already knows its active run and selected case,
    // the human panel invokes with no arguments, and an agent that follows D.1
    // must not meet a validation error for supplying what D.1 told it to.
    for (const expected of APPENDIX_D) {
      expect(expected.inputSchema).not.toHaveProperty("required");
    }
  });

  it("keeps every published name and description inside §11.4's budgets", async () => {
    // §11.4: tool and parameter names at most 30 characters, tool descriptions
    // at most 500, parameter descriptions at most 150. A schema that drifted
    // past one would be refused by a browser rather than by a reviewer.
    for (const expected of APPENDIX_D) {
      expect(expected.name.length).toBeLessThanOrEqual(30);
      expect(expected.description.length).toBeLessThanOrEqual(500);
      for (const [parameter, schema] of Object.entries(expected.inputSchema.properties)) {
        expect(parameter.length).toBeLessThanOrEqual(30);
        expect(schema.description.length).toBeLessThanOrEqual(150);
      }
    }
  });
});

describe("get_outcome_contract reads the route the service serves", () => {
  const CONTRACT = {
    contract_id: "con_1",
    name: "SAVE20, no checkout",
    schema_version: "1.0",
    content_hash: "sha256:abc",
    source_template_id: "save20_no_checkout",
    is_built_in: false,
    document: { assertions: [{ path: "target.cart.total", operator: "equals" }] },
  };

  it("requests GET /contracts/{id} rather than the absent /published", async () => {
    // The tool asked for `/contracts/{id}/published`, which
    // `routes/contracts.py` deliberately does not implement — so every call
    // 404'd. No existing test looked at the URL, which is why it shipped.
    // Arrange
    const calls = recordFetch(CONTRACT);
    const toolset = await toolsetFor("contract_ready");

    // Act
    const outcome = await toolset.invoke("get_outcome_contract");

    // Assert
    expect(calls.map((call) => call.url)).toEqual(["/api/v1/contracts/con_1"]);
    expect(outcome).not.toMatchObject({ isError: true });
  });

  it("returns the contract document the description promises", async () => {
    // Arrange
    recordFetch(CONTRACT);
    const toolset = await toolsetFor("contract_ready");

    // Act
    const outcome = await toolset.invoke("get_outcome_contract");

    // Assert — the identity an agent needs to check what it was handed, and
    // the document itself, keyed as §15.2 names them.
    expect(JSON.parse(textOf(outcome))).toEqual(CONTRACT);
  });

  it("refuses a contract body that does not match its contract", async () => {
    // A response body is untrusted input even from our own server. A record
    // with no content hash cannot be checked against the run it armed, and
    // handing it over anyway would make an unverifiable document look verified.
    // Arrange
    recordFetch({ contract_id: "con_1", name: "n", schema_version: "1.0", document: {} });
    const toolset = await toolsetFor("contract_ready");

    // Act
    const outcome = await toolset.invoke("get_outcome_contract");

    // Assert
    expect(outcome).toMatchObject({ isError: true });
    expect(textOf(outcome)).toContain("content_hash");
  });

  it("reports a server refusal instead of an empty contract", async () => {
    // Arrange
    recordFetch({ error: { code: "RESOURCE_NOT_FOUND", message: "No such contract.", retryable: false } }, 404);
    const toolset = await toolsetFor("contract_ready");

    // Act
    const outcome = await toolset.invoke("get_outcome_contract");

    // Assert
    expect(outcome).toMatchObject({ isError: true });
    expect(textOf(outcome)).toContain("No such contract.");
  });
});

describe("get_run_findings speaks FR-150's field names", () => {
  const FAILURE = {
    check_id: "cart_total_equals",
    check_type: "assertion",
    status: "failed",
    severity: "critical",
    classification: "false_success_or_state_mismatch",
    path: "target.cart.total",
    expected: "20.00",
    actual: "25.00",
  };
  const PASS = {
    check_id: "cart_line_count",
    check_type: "assertion",
    status: "passed",
    severity: "minor",
    classification: null,
    path: "target.cart.lines",
    expected: "1",
    actual: "1",
  };

  function findingsBody(findings: readonly unknown[], total = findings.length): unknown {
    return {
      run_id: "run_1",
      overall_result: "failed",
      findings,
      returned: findings.length,
      total,
      failed: 1,
      elided: total - findings.length,
      report: "/api/v1/runs/run_1/report",
    };
  }

  it("returns check_id and overall_result, never their camelCase parses", async () => {
    // `parseFindings` narrows the untrusted body into the UI's camelCase view,
    // and returning that object handed the agent `checkId`/`overallResult` —
    // names FR-150 does not use and no document the agent can read explains.
    // Arrange
    recordFetch(findingsBody([FAILURE]));
    const toolset = await toolsetFor("failed");

    // Act
    const text = textOf(await toolset.invoke("get_run_findings"));

    // Assert
    expect(JSON.parse(text)).toMatchObject({ run_id: "run_1", overall_result: "failed" });
    expect(JSON.parse(text)).toMatchObject({ findings: [FAILURE] });
    expect(text).not.toContain("checkId");
    expect(text).not.toContain("checkType");
    expect(text).not.toContain("overallResult");
  });

  it("still refuses an API response that does not match its contract", async () => {
    // The validation is why `parseFindings` is in the path at all; renaming
    // fields on the way out must not become a reason to stop checking them.
    // Arrange
    recordFetch(findingsBody([{ ...FAILURE, check_id: 17 }]));
    const toolset = await toolsetFor("failed");

    // Act
    const outcome = await toolset.invoke("get_run_findings");

    // Assert
    expect(outcome).toMatchObject({ isError: true });
    expect(textOf(outcome)).toContain("check_id");
  });

  it("drops passing checks for include=failures and keeps the run's own totals", async () => {
    // Arrange — the service returns every check, failures first.
    recordFetch(findingsBody([FAILURE, PASS], 9));
    const toolset = await toolsetFor("failed");

    // Act
    const document = JSON.parse(
      textOf(await toolset.invoke("get_run_findings", { include: "failures" })),
    ) as Record<string, unknown>;

    // Assert — the list narrows; the counts still describe the whole run, so an
    // agent that received one finding cannot conclude there was one.
    expect(document["findings"]).toEqual([FAILURE]);
    expect(document).toMatchObject({ returned: 1, total: 9, failed: 1, elided: 8 });
  });

  it("keeps every evaluated check for include=all", async () => {
    // Arrange
    recordFetch(findingsBody([FAILURE, PASS], 9));
    const toolset = await toolsetFor("failed");

    // Act
    const document = JSON.parse(
      textOf(await toolset.invoke("get_run_findings", { include: "all" })),
    ) as Record<string, unknown>;

    // Assert
    expect(document["findings"]).toEqual([FAILURE, PASS]);
    expect(document).toMatchObject({ returned: 2, elided: 7 });
  });

  it("refuses a limit that has no query representation", async () => {
    // Arguments arrive from an agent, so they arrive as `unknown` whatever the
    // published schema says. Stringifying an object would send
    // `?limit=[object Object]` and turn a caller's mistake into a puzzle about
    // the harness.
    // Arrange
    const calls = recordFetch(findingsBody([FAILURE]));
    const toolset = await toolsetFor("failed");

    // Act
    const outcome = await toolset.invoke("get_run_findings", { limit: { most: 3 } });

    // Assert — refused before anything was requested.
    expect(outcome).toMatchObject({ isError: true });
    expect(textOf(outcome)).toContain("limit must be an integer");
    expect(calls).toEqual([]);
  });

  it("refuses an include value it cannot honour rather than defaulting", async () => {
    // Answering a different question and reporting success is the exact failure
    // this product exists to surface; it must not happen in the harness.
    // Arrange
    recordFetch(findingsBody([FAILURE]));
    const toolset = await toolsetFor("failed");

    // Act
    const outcome = await toolset.invoke("get_run_findings", { include: "warnings" });

    // Assert
    expect(outcome).toMatchObject({ isError: true });
    expect(textOf(outcome)).toContain("'failures' or 'all'");
  });
});

describe("Appendix D's arguments work supplied and omitted", () => {
  it("verifies the named run, and the active run when none is named", async () => {
    // Arrange
    const supplied = recordFetch({});
    const namedRun = await toolsetFor("running");

    // Act
    await namedRun.invoke("verify_outcome", { run_id: "run_named" });

    // Assert
    expect(supplied.map((call) => call.url)).toContain("/api/v1/runs/run_named/verify");

    // Arrange / Act — the same tool with nothing supplied.
    const inferred = recordFetch({});
    const activeRun = await toolsetFor("running");
    await activeRun.invoke("verify_outcome");

    // Assert
    expect(inferred.map((call) => call.url)).toContain("/api/v1/runs/run_1/verify");
  });

  it("reads findings for the named run, and the active run when none is named", async () => {
    // Arrange
    const body = {
      run_id: "run_named",
      overall_result: "failed",
      findings: [],
      returned: 0,
      total: 0,
      failed: 0,
      elided: 0,
      report: "/api/v1/runs/run_named/report",
    };
    const supplied = recordFetch(body);
    const named = await toolsetFor("failed");

    // Act
    await named.invoke("get_run_findings", { run_id: "run_named", limit: 5 });

    // Assert — the limit travels verbatim so FastAPI stays the authority on
    // §11.4's bounds.
    expect(supplied.map((call) => call.url)).toContain("/api/v1/runs/run_named/findings?limit=5");

    // Arrange / Act
    const inferred = recordFetch({ ...body, run_id: "run_1" });
    const active = await toolsetFor("failed");
    await active.invoke("get_run_findings");

    // Assert
    expect(inferred.map((call) => call.url)).toContain("/api/v1/runs/run_1/findings");
  });

  it("cuts an eval from the named run, and the active run when none is named", async () => {
    // Arrange
    const supplied = recordFetch({ eval_case_id: "eval_1" });
    const named = await toolsetFor("failed");

    // Act
    await act(async () => {
      await named.invoke("create_regression_eval", { run_id: "run_named" });
    });

    // Assert
    expect(supplied.map((call) => call.url)).toContain("/api/v1/runs/run_named/evals");

    // Arrange / Act
    const inferred = recordFetch({ eval_case_id: "eval_1" });
    const active = await toolsetFor("failed");
    await act(async () => {
      await active.invoke("create_regression_eval");
    });

    // Assert
    expect(inferred.map((call) => call.url)).toContain("/api/v1/runs/run_1/evals");
  });

  it("replays the named case, and the selected case when none is named", async () => {
    // Arrange
    const supplied = recordFetch({});
    const named = await toolsetFor("failed", { evalCaseId: "eval_selected" });

    // Act
    await named.invoke("run_regression_eval", { eval_case_id: "eval_named" });

    // Assert
    expect(supplied.map((call) => call.url)).toContain("/api/v1/evals/eval_named/runs");

    // Arrange / Act
    const inferred = recordFetch({});
    const selected = await toolsetFor("failed", { evalCaseId: "eval_selected" });
    await selected.invoke("run_regression_eval");

    // Assert
    expect(inferred.map((call) => call.url)).toContain("/api/v1/evals/eval_selected/runs");
  });

  it("arms with the selected contract named, and with nothing named", async () => {
    // Arrange
    const supplied = recordFetch({ run_id: "run_2" });
    const named = await toolsetFor("contract_ready");

    // Act
    const outcome = await named.invoke("arm_outcome_contract", {
      contract_id: "con_1",
      mode: "verification",
    });

    // Assert — the id is checked rather than forwarded: `POST /runs` takes no
    // contract identifier, because FR-024 already made one contract active.
    expect(outcome).not.toMatchObject({ isError: true });
    expect(supplied[0]?.url).toBe("/api/v1/runs");
    expect(JSON.parse(supplied[0]?.body ?? "{}")).toEqual({ mode: "verification" });

    // Arrange / Act
    const inferred = recordFetch({ run_id: "run_2" });
    const anonymous = await toolsetFor("contract_ready");
    await anonymous.invoke("arm_outcome_contract");

    // Assert
    expect(JSON.parse(inferred[0]?.body ?? "null")).toEqual({});
  });

  it("refuses to arm a contract other than the one selected", async () => {
    // Arming a different contract and reporting success is exactly the gap
    // between claim and outcome this harness exists to catch.
    // Arrange
    const calls = recordFetch({ run_id: "run_2" });
    const toolset = await toolsetFor("contract_ready");

    // Act
    const outcome = await toolset.invoke("arm_outcome_contract", { contract_id: "con_other" });

    // Assert — refused, and nothing was armed.
    expect(outcome).toMatchObject({ isError: true });
    expect(textOf(outcome)).toContain("con_other");
    expect(calls).toEqual([]);
  });

  it("still binds a matched comparison source (§15.3)", async () => {
    // Beyond D.1 and deliberately kept: 07-matched-comparison drives the pair
    // through this argument, so dropping it to match D.1 exactly would delete a
    // shipped capability with a live caller.
    // Arrange
    const calls = recordFetch({ run_id: "run_2" });
    const toolset = await toolsetFor("contract_ready");

    // Act
    await toolset.invoke("arm_outcome_contract", { comparison_source_run_id: "run_source" });

    // Assert
    expect(JSON.parse(calls[0]?.body ?? "null")).toEqual({ comparison_source_run_id: "run_source" });
  });
});

describe("a generated case is replayable by the agent that generated it", () => {
  it("publishes the replay tool once a case has been created", async () => {
    // AC-22 measures the §11.1 table for *reachability*. `run_regression_eval`
    // was declared `enabled: evalCaseId !== null` while `App` supplied no case
    // id, so the tool could never register: an agent could cut a regression
    // case and then had no way to replay it.
    respondWith({ eval_case_id: "eval_1", created: true });
    const { result } = renderHook(() => useHarnessToolset(workspace("failed"), noop));
    await waitFor(() =>
      expect(result.current.states["create_regression_eval"]?.phase).toBe("registered"),
    );
    expect(result.current.states["run_regression_eval"]?.phase).not.toBe("registered");

    // Wrapped: the handler stores the created id, which re-registers the replay
    // tool — a state update the assertions below depend on having settled.
    await act(async () => {
      await installed?.modelContext.invoke("create_regression_eval");
    });

    await waitFor(() =>
      expect(result.current.states["run_regression_eval"]?.phase).toBe("registered"),
    );
    expect(result.current.evalCaseId).toBe("eval_1");
  });

  it("keeps the replay tool unavailable when the response names no case", async () => {
    // A response body is untrusted input. A missing id must leave the replay
    // unavailable rather than pointed at `"undefined"`.
    respondWith({ created: true });
    const { result } = renderHook(() => useHarnessToolset(workspace("failed"), noop));
    await waitFor(() =>
      expect(result.current.states["create_regression_eval"]?.phase).toBe("registered"),
    );

    await act(async () => {
      await installed?.modelContext.invoke("create_regression_eval");
    });

    expect(result.current.evalCaseId).toBeNull();
    expect(result.current.states["run_regression_eval"]?.phase).not.toBe("registered");
  });

  it("prefers a case the caller selected over the one it created", async () => {
    // The panel and the agent must be looking at one case, and the human
    // selection is the one a person can see.
    respondWith({ eval_case_id: "eval_created", created: true });
    const { result } = renderHook(() =>
      useHarnessToolset(workspace("failed"), noop, { evalCaseId: "eval_selected" }),
    );
    await waitFor(() =>
      expect(result.current.states["run_regression_eval"]?.phase).toBe("registered"),
    );

    expect(result.current.evalCaseId).toBe("eval_selected");
  });
});
