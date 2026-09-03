/**
 * The dual-layer matrix is reachable from the running product (§9.9, AC-16).
 *
 * `benchmarkPanel.test.tsx` renders `BenchmarkPanel` directly with a hand-built
 * payload, and passed for months while the panel was **imported by nothing**:
 * it was added by a commit called "feat: add the dual-layer BenchmarkPanel" and
 * never referenced in `App.tsx`. A component test cannot catch that, because
 * mounting the component is the thing it does for you.
 *
 * So this test refuses to import the panel. It renders the real `<App />`
 * against a mocked fetch and asserts a person can see the matrix — which fails
 * the moment the panel is unmounted again, the listing route disappears, or the
 * fetch layer stops being wired.
 */

import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import App from "./App";

const WORKSPACE_BODY = {
  workspace_id: "ws_1",
  selected_target_id: "buggy-store",
  selected_contract_id: null,
  scenario_mode: "pre_fix",
  failure_profile: null,
  active_run: null,
  guidance: {
    phase: "no_contract",
    active_actor: "human_operator",
    next_actor: null,
    headline: "Select an outcome contract.",
    instruction: "Choose one.",
    reason: "Nothing is armed.",
    expected_consequence: "Nothing happens automatically.",
    action_code: "select_contract",
    recovery_action_code: null,
    waiting_for: null,
    requires_human_input: true,
  },
  next_action: {
    actor: "human_operator",
    action_code: "select_contract",
    instruction: "Choose one.",
    requires_human_input: true,
  },
  capabilities: {},
};

function rate(numerator: number, denominator: number, value: string | null) {
  return { numerator, denominator, value };
}

const COUNTS = {
  call_level_pass_outcome_pass: 2,
  call_level_pass_outcome_fail: 3,
  call_level_fail_outcome_pass: 1,
  call_level_fail_outcome_fail: 4,
  eligible_trials: 10,
  excluded_trials: 2,
  error_trials: 1,
  total_trials: 12,
};

const METRICS = {
  call_level_pass_rate: rate(5, 10, "0.5000"),
  outcome_pass_rate: rate(3, 10, "0.3000"),
  end_to_end_success_rate: rate(2, 10, "0.2000"),
  silent_outcome_failure_rate: rate(3, 5, "0.6000"),
  incremental_outcome_failure_trials: 3,
};

const BENCHMARK_BODY = {
  benchmark_id: "bench_1",
  status: "completed",
  source_kind: "recorded_fixture",
  correlation_mode: "imported_trajectory_replay",
  result_artifact_id: "art_1",
  manifest: {
    source_kind: "recorded_fixture",
    correlation_mode: "imported_trajectory_replay",
    evaluator_name: "webmcp-evals",
    evaluator_version: "0.0.4",
    model_provider: null,
    model_name: null,
    target_build_commit: "abc1234",
    reporter_schema: "webmcp-evals/0.0.4",
    normalized_adapter_version: "1",
  },
  counts: COUNTS,
  metrics: METRICS,
  by_scenario: [],
  by_failure_profile: [],
  trials: [],
};

const LISTING_BODY = {
  benchmarks: [
    {
      benchmark_id: "bench_1",
      status: "completed",
      source_kind: "recorded_fixture",
      correlation_mode: "imported_trajectory_replay",
      result_artifact_id: "art_1",
      created_at: "2026-01-01T00:00:00Z",
    },
  ],
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
}

function installFetch(listing: unknown = LISTING_BODY): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = urlOf(input);
      if (url.includes("/contracts/templates")) {
        return jsonResponse({ templates: [] });
      }
      // The suite read is more specific than the listing and must be checked
      // first, or the listing body would answer both.
      if (url.includes("/benchmarks/bench_1")) {
        return jsonResponse(BENCHMARK_BODY);
      }
      if (url.includes("/benchmarks")) {
        return jsonResponse(listing);
      }
      if (url.includes("/eval")) {
        return jsonResponse({ cases: [] });
      }
      if (url.includes("/workspace")) {
        return jsonResponse(WORKSPACE_BODY);
      }
      throw new Error(`unmocked fetch in test: ${url}`);
    }),
  );
}

describe("the benchmark matrix is reachable from the workspace", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the dual-layer matrix without importing the panel here", async () => {
    // Arrange / Act
    installFetch();
    render(<App />);

    // Assert — the four quadrant readings a person is meant to see. Asserting
    // on the matrix's own copy rather than a test id, because a panel that
    // rendered an empty shell would satisfy a test id and tell nobody anything.
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Benchmark suites" })).toBeDefined();
    });
    await waitFor(() => {
      // `silent_outcome_failure_rate` is the number this whole product exists
      // to report: a call the evaluator scored as passing whose outcome failed.
      expect(screen.getAllByText("0.6000").length).toBeGreaterThan(0);
    });
  });

  it("offers the rail entry that leads there", async () => {
    // Arrange / Act
    installFetch();
    render(<App />);

    // Assert — the panel being mounted is not enough if nothing navigates to
    // it; the operator found the old gap by looking for a screen and not
    // finding one.
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "Benchmark" })).toBeDefined();
    });
  });

  it("shows the empty state rather than failing when no suite exists", async () => {
    // Arrange / Act — a fresh workspace has no suites, which is ordinary.
    installFetch({ benchmarks: [] });
    render(<App />);

    // Assert
    await waitFor(() => {
      expect(screen.getByRole("region", { name: "Benchmark suites" })).toBeDefined();
    });
    expect(screen.getAllByText(/no suites yet/i).length).toBeGreaterThan(0);
  });
});
