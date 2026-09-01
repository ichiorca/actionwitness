/**
 * 008-T10 — the dual-layer benchmark panel (§9.9, FR-092, FR-095, AC-16).
 *
 * Three properties carry this panel, and each is a claim about honesty rather
 * than about layout.
 *
 * **A null rate is words, not a zero.** FR-092 makes every rate null over an
 * empty population. `0.0000` would read as "we measured and found none", which
 * is the reading somebody acts on, so the absence is spelled out.
 *
 * **The source kind is never dressed up as a live run.** AC-16 requires the
 * application to "never represent either as a live execution", and a recorded
 * fixture is the one somebody would otherwise repeat in a talk as a model
 * result.
 *
 * **The forbidden copy stays forbidden.** FR-095 names two phrases that must
 * not appear in generated product copy. A test is the only thing that keeps a
 * later edit from reintroducing them.
 */

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { parseBenchmark } from "../api/benchmark";
import { BenchmarkPanel } from "./panels";

function rate(numerator: number, denominator: number, value: string | null) {
  return { numerator, denominator, value };
}

function counts(overrides: Record<string, number> = {}) {
  return {
    call_level_pass_outcome_pass: 2,
    call_level_pass_outcome_fail: 3,
    call_level_fail_outcome_pass: 1,
    call_level_fail_outcome_fail: 4,
    eligible_trials: 10,
    excluded_trials: 2,
    error_trials: 1,
    total_trials: 12,
    ...overrides,
  };
}

function metrics(overrides: Record<string, unknown> = {}) {
  return {
    call_level_pass_rate: rate(5, 10, "0.5000"),
    outcome_pass_rate: rate(3, 10, "0.3000"),
    end_to_end_success_rate: rate(2, 10, "0.2000"),
    silent_outcome_failure_rate: rate(3, 5, "0.6000"),
    incremental_outcome_failure_trials: 3,
    ...overrides,
  };
}

function payload(overrides: Record<string, unknown> = {}) {
  return {
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
    counts: counts(),
    metrics: metrics(),
    by_scenario: [
      {
        label: "adds a mug",
        counts: counts(),
        metrics: metrics(),
      },
    ],
    by_failure_profile: [
      {
        label: "discount_reported_but_not_applied",
        counts: counts(),
        metrics: metrics(),
      },
    ],
    trials: [
      {
        external_trial_id: "adds a mug#0",
        scenario_id: "adds a mug",
        call_level_result: "passed",
        outcome_result: "failed",
        eligibility: "eligible",
        exclusion_reason: null,
        addressable: true,
      },
      {
        external_trial_id: "#1",
        scenario_id: "adds a mug",
        call_level_result: "error",
        outcome_result: "not_reached",
        eligibility: "excluded",
        exclusion_reason: "evaluator_error",
        addressable: false,
      },
    ],
    ...overrides,
  };
}

function renderPanel(overrides: Record<string, unknown> = {}) {
  const onReplay = vi.fn();
  const onFinalize = vi.fn();
  render(
    <BenchmarkPanel
      benchmark={parseBenchmark(payload(overrides))}
      busy={false}
      onReplay={onReplay}
      onFinalize={onFinalize}
      trialHref={(id) => `/api/v1/benchmarks/bench_1/trials/${encodeURIComponent(id)}`}
      reportHref="/api/v1/benchmarks/bench_1/report"
    />,
  );
  return { onReplay, onFinalize };
}

describe("BenchmarkPanel", () => {
  it("names the source kind and says a recorded fixture was not a live run", () => {
    renderPanel();

    expect(screen.getByText("recorded_fixture")).toBeTruthy();
    expect(screen.getByText(/No model was called in this run/)).toBeTruthy();
  });

  it("shows the correlation mode, so two populations are never confused", () => {
    renderPanel();

    expect(screen.getByText("imported_trajectory_replay")).toBeTruthy();
  });

  it("renders the four matrix cells with their readings in words", () => {
    renderPanel();

    const matrix = screen.getByRole("table");
    expect(within(matrix).getByText(/Silent outcome defect/)).toBeTruthy();
    expect(within(matrix).getByText(/Verified end to end/)).toBeTruthy();
    expect(within(matrix).getByText(/End-to-end failure/)).toBeTruthy();
  });

  it("shows coverage beside the rates", () => {
    renderPanel();

    // A rate over ten eligible trials and one over two hundred read
    // identically without the denominator beside them.
    expect(screen.getByText("Eligible")).toBeTruthy();
    expect(screen.getAllByText("10").length).toBeGreaterThan(0);
    expect(screen.getByText(/of which errors: 1/)).toBeTruthy();
  });

  it("prints every rate to four decimals with its integers", () => {
    renderPanel();

    expect(screen.getAllByText("0.5000").length).toBeGreaterThan(0);
    expect(screen.getAllByText("(5/10)").length).toBeGreaterThan(0);
  });

  it("renders an absent rate as words rather than as a zero", () => {
    renderPanel({
      counts: counts({
        call_level_pass_outcome_pass: 0,
        call_level_pass_outcome_fail: 0,
        call_level_fail_outcome_pass: 0,
        call_level_fail_outcome_fail: 0,
        eligible_trials: 0,
        total_trials: 2,
      }),
      metrics: metrics({
        call_level_pass_rate: rate(0, 0, null),
        outcome_pass_rate: rate(0, 0, null),
        end_to_end_success_rate: rate(0, 0, null),
        silent_outcome_failure_rate: rate(0, 0, null),
        incremental_outcome_failure_trials: 0,
      }),
      by_scenario: [],
      by_failure_profile: [],
    });

    expect(screen.getAllByText("no eligible trials").length).toBeGreaterThan(0);
    expect(screen.queryByText("0.0000")).toBeNull();
  });

  it("says so when no trial had usable evidence on both layers", () => {
    renderPanel({
      counts: counts({
        call_level_pass_outcome_pass: 0,
        call_level_pass_outcome_fail: 0,
        call_level_fail_outcome_pass: 0,
        call_level_fail_outcome_fail: 0,
        eligible_trials: 0,
        total_trials: 2,
      }),
      metrics: metrics({
        call_level_pass_rate: rate(0, 0, null),
        outcome_pass_rate: rate(0, 0, null),
        end_to_end_success_rate: rate(0, 0, null),
        silent_outcome_failure_rate: rate(0, 0, null),
        incremental_outcome_failure_trials: 0,
      }),
      by_scenario: [],
      by_failure_profile: [],
    });

    expect(screen.getByRole("status").textContent).toContain("insufficient sample");
  });

  it("keeps scenario and failure-profile populations separate", () => {
    renderPanel();

    expect(screen.getByText("By scenario")).toBeTruthy();
    expect(screen.getByText("By failure profile")).toBeTruthy();
    expect(screen.getByText("discount_reported_but_not_applied")).toBeTruthy();
  });

  it("links every trial to its own redacted evidence", () => {
    renderPanel();

    const link = screen.getByRole("link", { name: "adds a mug#0" });
    expect(link.getAttribute("href")).toBe(
      "/api/v1/benchmarks/bench_1/trials/adds%20a%20mug%230",
    );
  });

  it("says why an excluded trial was excluded", () => {
    renderPanel();

    // Coverage without a reason is a number nobody can act on.
    expect(screen.getByText(/evaluator_error/)).toBeTruthy();
  });

  it("marks a trial that needs an explicit binding choice", () => {
    renderPanel();

    expect(screen.getByText(/needs an explicit binding choice/)).toBeTruthy();
  });

  it("reports absent reproducibility metadata as not recorded", () => {
    renderPanel();

    // FR-093: missing metadata is null, never inferred. "not recorded" is
    // itself information; a blank would look like a rendering fault.
    expect(screen.getAllByText("not recorded").length).toBeGreaterThan(0);
    expect(screen.getByText("webmcp-evals/0.0.4")).toBeTruthy();
  });

  it("never uses the comparative copy FR-095 forbids", () => {
    renderPanel();

    const text = document.body.textContent ?? "";
    expect(text.toLowerCase()).not.toContain("accuracy comparison");
    expect(text.toLowerCase()).not.toContain("beat the probabilistic");
    // The panel says what the layers measure, not which one won.
    expect(text).toContain("two different questions");
  });

  it("explains itself when no benchmark exists yet", () => {
    render(
      <BenchmarkPanel
        benchmark={null}
        busy={false}
        onReplay={vi.fn()}
        onFinalize={vi.fn()}
        trialHref={() => ""}
        reportHref=""
      />,
    );

    // FR-096: the module stays available and instructs rather than vanishing.
    expect(screen.getByText(/Import a supported evaluator report/)).toBeTruthy();
  });

  it("offers the report only once one has been finalized", () => {
    renderPanel({ result_artifact_id: null });

    expect(screen.queryByRole("link", { name: /Download the benchmark report/ })).toBeNull();
  });
});
