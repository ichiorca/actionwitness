/**
 * Narrowing the benchmark payload (§15.6, FR-092, FR-093).
 *
 * As with `workspace.ts`, this module narrows and nothing more. Every count and
 * every rate is computed server-side from integer counts (FR-092), so a
 * frontend that recalculated one would be a second opinion on a number the
 * artifact publishes — and the two would disagree the first time either
 * changed.
 *
 * **A null rate stays null.** FR-092 makes every rate `null` when there is no
 * population, and `silent_outcome_failure_rate` null when nothing passed at
 * call level. Coercing either to `0` here would turn "this question has no
 * answer" into "we measured and found none", which is the wrong reading and the
 * one a reader is most likely to act on.
 */

import { isRecord, optionalString, requireArray, requireRecord, requireString } from "./client";

/** One FR-092 rate: the integers it came from, and the four-decimal string. */
export interface Rate {
  readonly numerator: number;
  readonly denominator: number;
  /** The presentation string, or `null` over an empty population. Never `0`. */
  readonly value: string | null;
}

export interface MatrixCounts {
  readonly callLevelPassOutcomePass: number;
  readonly callLevelPassOutcomeFail: number;
  readonly callLevelFailOutcomePass: number;
  readonly callLevelFailOutcomeFail: number;
  readonly eligibleTrials: number;
  readonly excludedTrials: number;
  readonly errorTrials: number;
  readonly totalTrials: number;
}

export interface BenchmarkMetrics {
  readonly callLevelPassRate: Rate;
  readonly outcomePassRate: Rate;
  readonly endToEndSuccessRate: Rate;
  readonly silentOutcomeFailureRate: Rate;
  readonly incrementalOutcomeFailureTrials: number;
}

export interface Population {
  readonly label: string;
  readonly counts: MatrixCounts;
  readonly metrics: BenchmarkMetrics;
}

export interface TrialSummary {
  readonly externalTrialId: string;
  readonly scenarioId: string;
  readonly callLevelResult: string;
  readonly outcomeResult: string;
  readonly eligibility: string;
  readonly exclusionReason: string | null;
  /** FR-091: `false` means only an explicit human choice may bind it. */
  readonly addressable: boolean;
}

export interface BenchmarkManifestView {
  readonly sourceKind: string;
  readonly correlationMode: string;
  readonly evaluatorName: string | null;
  readonly evaluatorVersion: string | null;
  readonly modelProvider: string | null;
  readonly modelName: string | null;
  readonly targetBuildCommit: string | null;
  readonly reporterSchema: string | null;
  readonly normalizedAdapterVersion: string | null;
}

export interface BenchmarkView {
  readonly benchmarkId: string;
  readonly status: string;
  readonly sourceKind: string;
  readonly correlationMode: string;
  readonly resultArtifactId: string | null;
  readonly manifest: BenchmarkManifestView;
  readonly counts: MatrixCounts;
  readonly metrics: BenchmarkMetrics;
  readonly byScenario: readonly Population[];
  readonly byFailureProfile: readonly Population[];
  readonly trials: readonly TrialSummary[];
}

function requireNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${field} must be a number`);
  }
  return value;
}

function parseRate(value: unknown, field: string): Rate {
  const record = requireRecord(value, field);
  return {
    numerator: requireNumber(record["numerator"], `${field}.numerator`),
    denominator: requireNumber(record["denominator"], `${field}.denominator`),
    // `optionalString` keeps `null` as `null`. That is the whole point: a rate
    // over an empty population has no value, and inventing "0.0000" would be a
    // measurement claim nobody made.
    value: optionalString(record["value"]),
  };
}

function parseCounts(value: unknown, field: string): MatrixCounts {
  const record = requireRecord(value, field);
  return {
    callLevelPassOutcomePass: requireNumber(
      record["call_level_pass_outcome_pass"],
      `${field}.call_level_pass_outcome_pass`,
    ),
    callLevelPassOutcomeFail: requireNumber(
      record["call_level_pass_outcome_fail"],
      `${field}.call_level_pass_outcome_fail`,
    ),
    callLevelFailOutcomePass: requireNumber(
      record["call_level_fail_outcome_pass"],
      `${field}.call_level_fail_outcome_pass`,
    ),
    callLevelFailOutcomeFail: requireNumber(
      record["call_level_fail_outcome_fail"],
      `${field}.call_level_fail_outcome_fail`,
    ),
    eligibleTrials: requireNumber(record["eligible_trials"], `${field}.eligible_trials`),
    excludedTrials: requireNumber(record["excluded_trials"], `${field}.excluded_trials`),
    errorTrials: requireNumber(record["error_trials"], `${field}.error_trials`),
    totalTrials: requireNumber(record["total_trials"], `${field}.total_trials`),
  };
}

function parseMetrics(value: unknown, field: string): BenchmarkMetrics {
  const record = requireRecord(value, field);
  return {
    callLevelPassRate: parseRate(record["call_level_pass_rate"], `${field}.call_level_pass_rate`),
    outcomePassRate: parseRate(record["outcome_pass_rate"], `${field}.outcome_pass_rate`),
    endToEndSuccessRate: parseRate(
      record["end_to_end_success_rate"],
      `${field}.end_to_end_success_rate`,
    ),
    silentOutcomeFailureRate: parseRate(
      record["silent_outcome_failure_rate"],
      `${field}.silent_outcome_failure_rate`,
    ),
    incrementalOutcomeFailureTrials: requireNumber(
      record["incremental_outcome_failure_trials"],
      `${field}.incremental_outcome_failure_trials`,
    ),
  };
}

function parsePopulation(value: unknown, field: string): Population {
  const record = requireRecord(value, field);
  return {
    label: requireString(record["label"], `${field}.label`),
    counts: parseCounts(record["counts"], `${field}.counts`),
    metrics: parseMetrics(record["metrics"], `${field}.metrics`),
  };
}

function parseTrial(value: unknown, field: string): TrialSummary {
  const record = requireRecord(value, field);
  return {
    externalTrialId: requireString(record["external_trial_id"], `${field}.external_trial_id`),
    scenarioId: requireString(record["scenario_id"], `${field}.scenario_id`),
    callLevelResult: requireString(record["call_level_result"], `${field}.call_level_result`),
    outcomeResult: requireString(record["outcome_result"], `${field}.outcome_result`),
    eligibility: requireString(record["eligibility"], `${field}.eligibility`),
    exclusionReason: optionalString(record["exclusion_reason"]),
    addressable: record["addressable"] !== false,
  };
}

function parseManifest(value: unknown, field: string): BenchmarkManifestView {
  const record = isRecord(value) ? value : {};
  return {
    sourceKind: requireString(record["source_kind"], `${field}.source_kind`),
    correlationMode: requireString(record["correlation_mode"], `${field}.correlation_mode`),
    evaluatorName: optionalString(record["evaluator_name"]),
    evaluatorVersion: optionalString(record["evaluator_version"]),
    modelProvider: optionalString(record["model_provider"]),
    modelName: optionalString(record["model_name"]),
    targetBuildCommit: optionalString(record["target_build_commit"]),
    reporterSchema: optionalString(record["reporter_schema"]),
    normalizedAdapterVersion: optionalString(record["normalized_adapter_version"]),
  };
}

export function parseBenchmark(value: unknown): BenchmarkView {
  const record = requireRecord(value, "benchmark");
  return {
    benchmarkId: requireString(record["benchmark_id"], "benchmark_id"),
    status: requireString(record["status"], "status"),
    sourceKind: requireString(record["source_kind"], "source_kind"),
    correlationMode: requireString(record["correlation_mode"], "correlation_mode"),
    resultArtifactId: optionalString(record["result_artifact_id"]),
    manifest: parseManifest(record["manifest"], "manifest"),
    counts: parseCounts(record["counts"], "counts"),
    metrics: parseMetrics(record["metrics"], "metrics"),
    byScenario: requireArray(record["by_scenario"], "by_scenario").map((entry, index) =>
      parsePopulation(entry, `by_scenario[${String(index)}]`),
    ),
    byFailureProfile: requireArray(record["by_failure_profile"], "by_failure_profile").map(
      (entry, index) => parsePopulation(entry, `by_failure_profile[${String(index)}]`),
    ),
    trials: requireArray(record["trials"], "trials").map((entry, index) =>
      parseTrial(entry, `trials[${String(index)}]`),
    ),
  };
}
