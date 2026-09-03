/**
 * Repeated trials and the correlation they produce, from a person's side.
 *
 * Spec v1.9 §26.5 (repeated trials per intent variant), §9.9 (the dual-layer
 * matrix), FR-092 (a null rate is not a zero), §8.4 (status must survive without
 * colour).
 *
 * Every test here asserts what the section does *after* something happens — a
 * fetch settling, a batch running, a refusal landing — rather than what it
 * renders on arrival. An initial render proves only that JSX compiles.
 *
 * Three properties carry the block:
 *
 * - **the disagreement cell reads as words.** Colour marks the signal row, but
 *   §8.4 forbids colour being the only channel, and this is the one finding the
 *   product exists for. A reader must get it from the sentence alone.
 * - **the ceiling is the server's.** The form refuses a batch above the number
 *   the server reported, and refuses it *without asking* — a request that was
 *   always going to be turned down is not a useful round trip.
 * - **absence is not agreement.** A suite with no trials, and a deployment with
 *   evaluator import switched off, each say what is missing instead of showing
 *   an empty matrix a reader would take for a clean result.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { BenchmarkCorrelation, BenchmarkView } from "../api/benchmark";
import { parseBenchmark, readBenchmarkCorrelation, runRepeatedTrials } from "../api/benchmark";
import { RepeatedTrialsSection } from "./BenchmarkSection";

vi.mock("../api/benchmark", async (importOriginal) => {
  // The parsers stay real: they are the boundary narrowing under test elsewhere,
  // and a fake one here would let a malformed payload look acceptable.
  const actual = await importOriginal<typeof import("../api/benchmark")>();
  return { ...actual, readBenchmarkCorrelation: vi.fn(), runRepeatedTrials: vi.fn() };
});

const readCorrelation = vi.mocked(readBenchmarkCorrelation);
const runTrials = vi.mocked(runRepeatedTrials);

const SCENARIO = "adds a mug";
const VARIANT = "Please add a ceramic mug and use the SAVE20 code.";

function rate(numerator: number, denominator: number, value: string | null) {
  return { numerator, denominator, value };
}

function counts(overrides: Partial<Record<string, number>> = {}) {
  return {
    callLevelPassOutcomePass: 0,
    callLevelPassOutcomeFail: 0,
    callLevelFailOutcomePass: 0,
    callLevelFailOutcomeFail: 0,
    eligibleTrials: 0,
    excludedTrials: 0,
    errorTrials: 0,
    totalTrials: 0,
    ...overrides,
  };
}

function metrics() {
  return {
    callLevelPassRate: rate(0, 0, null),
    outcomePassRate: rate(0, 0, null),
    endToEndSuccessRate: rate(0, 0, null),
    silentOutcomeFailureRate: rate(0, 0, null),
    incrementalOutcomeFailureTrials: 0,
  };
}

/** A correlation view with `passed` of `total` repetitions agreeing. */
function correlation(
  agreed: number,
  disagreed: number,
  overrides: Partial<BenchmarkCorrelation> = {},
): BenchmarkCorrelation {
  const eligible = agreed + disagreed;
  return {
    benchmarkId: "bench_1",
    status: "draft",
    sourceKind: "recorded_fixture",
    correlationMode: "imported_trajectory_replay",
    repetitionCeiling: 10,
    evaluatorImportAvailable: true,
    populations: [
      {
        label: VARIANT,
        trials: eligible,
        counts: counts({
          callLevelPassOutcomePass: agreed,
          callLevelPassOutcomeFail: disagreed,
          eligibleTrials: eligible,
          totalTrials: eligible,
        }),
        metrics: metrics(),
        evaluatorDistribution: [
          { result: "passed", trials: eligible },
          { result: "failed", trials: 0 },
          { result: "error", trials: 0 },
        ],
        observedDistribution: [
          { result: "passed", trials: agreed },
          { result: "failed", trials: disagreed },
        ],
        agreementTrials: agreed,
        agreementRate: rate(agreed, eligible, (agreed / eligible).toFixed(4)),
        overstatedTrials: disagreed,
        overstatedRate: rate(disagreed, eligible, (disagreed / eligible).toFixed(4)),
        understatedTrials: 0,
        understatedRate: rate(0, 0, null),
      },
    ],
    ...overrides,
  };
}

function view(trialIds: readonly string[]): BenchmarkView {
  return parseBenchmark({
    benchmark_id: "bench_1",
    status: "draft",
    source_kind: "recorded_fixture",
    correlation_mode: "imported_trajectory_replay",
    result_artifact_id: null,
    manifest_content_hash: null,
    manifest: {
      source_kind: "recorded_fixture",
      correlation_mode: "imported_trajectory_replay",
      evaluator_name: null,
      evaluator_version: null,
      model_provider: null,
      model_name: null,
      target_build_commit: null,
      reporter_schema: null,
      normalized_adapter_version: null,
      frozen_variants: null,
    },
    counts: {
      call_level_pass_outcome_pass: 0,
      call_level_pass_outcome_fail: 0,
      call_level_fail_outcome_pass: 0,
      call_level_fail_outcome_fail: 0,
      eligible_trials: 0,
      excluded_trials: 0,
      error_trials: 0,
      total_trials: 0,
    },
    metrics: {
      call_level_pass_rate: rate(0, 0, null),
      outcome_pass_rate: rate(0, 0, null),
      end_to_end_success_rate: rate(0, 0, null),
      silent_outcome_failure_rate: rate(0, 0, null),
      incremental_outcome_failure_trials: 0,
    },
    by_scenario: [],
    by_failure_profile: [],
    trials: trialIds.map((id) => ({
      external_trial_id: id,
      scenario_id: SCENARIO,
      call_level_result: "passed",
      outcome_result: "not_reached",
      eligibility: "excluded",
      exclusion_reason: "outcome_not_reached",
      addressable: true,
    })),
  });
}

function renderSection(trialIds: readonly string[] = [`${SCENARIO}#0`]) {
  render(
    <RepeatedTrialsSection selectedId="bench_1" benchmark={view(trialIds)} busy={false} />,
  );
}

beforeEach(() => {
  readCorrelation.mockReset();
  runTrials.mockReset();
});

describe("RepeatedTrialsSection", () => {
  it("states the disagreement cell in words once the correlation arrives", async () => {
    // Arrange — two of five repetitions of one variant disagreed.
    readCorrelation.mockResolvedValue(correlation(3, 2));

    // Act
    renderSection();

    // Assert — the sentence carries the count and the rate, so a reader who
    // cannot see the highlighted row still gets the finding.
    expect(
      await screen.findByText(/2 of them left business state this harness judged wrong/),
    ).toBeTruthy();
    expect(screen.getByText(/silent-failure rate of 0\.4000/)).toBeTruthy();
  });

  it("says nothing disagreed rather than leaving the cell blank", async () => {
    // Arrange
    readCorrelation.mockResolvedValue(correlation(4, 0));

    // Act
    renderSection();

    // Assert
    expect(
      await screen.findByText(
        /Every one of the 4 calls the evaluator scored correct also left business state/,
      ),
    ).toBeTruthy();
  });

  it("runs the chosen trial the requested number of times and re-reads the result", async () => {
    // Arrange — the first read shows a clean population; the batch changes it.
    readCorrelation.mockResolvedValueOnce(correlation(1, 0));
    readCorrelation.mockResolvedValueOnce(correlation(1, 3));
    runTrials.mockResolvedValue({
      trials: 3,
      repetitions: [
        {
          externalTrialId: `${SCENARIO}#0#repetition-1`,
          repetitionIndex: 1,
          outcomeResult: "failed",
          eligibility: "eligible",
          exclusionReason: null,
        },
      ],
    });
    renderSection();
    await screen.findByText(/Every one of the 1 calls/);

    // Act
    fireEvent.change(screen.getByLabelText("Trials"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Run repeated trials" }));

    // Assert — the request names the trial and the count, and the view that
    // follows is the server's, not an optimistic guess at what changed.
    await waitFor(() => {
      expect(runTrials).toHaveBeenCalledWith("bench_1", {
        sourceExternalTrialId: `${SCENARIO}#0`,
        trials: 3,
      });
    });
    expect(await screen.findByText(/3 trials recorded\./)).toBeTruthy();
    expect(await screen.findByText(/3 of them left business state this harness judged wrong/));
  });

  it("refuses a batch above the server's ceiling without sending it", async () => {
    // Arrange
    readCorrelation.mockResolvedValue(correlation(1, 0));
    renderSection();
    await screen.findByText(/Every one of the 1 calls/);

    // Act
    fireEvent.change(screen.getByLabelText("Trials"), { target: { value: "40" } });
    fireEvent.click(screen.getByRole("button", { name: "Run repeated trials" }));

    // Assert — refused in words, and no round trip that was always going to be
    // turned down.
    expect(await screen.findByText("Ask for between 1 and 10 trials.")).toBeTruthy();
    expect(runTrials).not.toHaveBeenCalled();
  });

  it("keeps a partial batch visible when the run refuses partway through", async () => {
    // Arrange — the batch fails, but two repetitions had already been recorded.
    readCorrelation.mockResolvedValueOnce(correlation(1, 0));
    readCorrelation.mockResolvedValueOnce(correlation(0, 2));
    runTrials.mockRejectedValue(new Error("The target went away."));
    renderSection();
    await screen.findByText(/Every one of the 1 calls/);

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Run repeated trials" }));

    // Assert — the refusal is shown *and* the view is re-read, because the
    // repetitions that did run are still in the suite and hiding them would
    // make a partial run look like no run at all.
    expect(await screen.findByText("The target went away.")).toBeTruthy();
    expect(await screen.findByText(/2 of them left business state this harness judged wrong/));
    expect(readCorrelation).toHaveBeenCalledTimes(2);
  });

  it("says a suite with no trials has nothing to repeat, and asks the server nothing", () => {
    // Arrange / Act
    renderSection([]);

    // Assert
    expect(screen.getByText(/This suite holds no trials yet/)).toBeTruthy();
    expect(readCorrelation).not.toHaveBeenCalled();
  });

  it("says why the evaluator axis is empty when import is switched off", async () => {
    // Arrange
    readCorrelation.mockResolvedValue(
      correlation(1, 0, { evaluatorImportAvailable: false }),
    );

    // Act
    renderSection();

    // Assert — a configuration fact, stated as one. An empty axis with no
    // explanation would read as "the evaluator found nothing".
    expect(
      await screen.findByText(/Evaluator import is switched off in this deployment/),
    ).toBeTruthy();
  });

  it("reports a correlation that could not be read instead of showing an empty matrix", async () => {
    // Arrange
    readCorrelation.mockRejectedValue(new Error("The harness could not be reached."));

    // Act
    renderSection();

    // Assert
    expect(await screen.findByText("The harness could not be reached.")).toBeTruthy();
    expect(screen.queryByRole("table")).toBeNull();
  });
});
