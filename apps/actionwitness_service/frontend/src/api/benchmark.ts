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

import {
  isRecord,
  optionalString,
  request,
  requireArray,
  requireRecord,
  requireString,
} from "./client";

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

/** One approved variant, as the manifest carries it (FR-100). */
export interface FrozenVariant {
  readonly kind: string;
  readonly text: string;
}

/**
 * The sealed variant set, or `null` when this suite never had one.
 *
 * The distinction is load-bearing and is the server's to make: `null` says "no
 * variants were ever generated for this suite", while a set with no variants
 * says "a human reviewed some and approved none". Coercing the first into the
 * second here would invent a review that never happened.
 */
export interface FrozenVariantsView {
  readonly canonicalIntent: string;
  readonly variants: readonly FrozenVariant[];
  readonly reviewer: string;
  readonly approvedAt: string;
  /** FR-100 forbids an agent approving its own material; the record says who. */
  readonly actor: string;
  readonly note: string;
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
  readonly frozenVariants: FrozenVariantsView | null;
}

export interface BenchmarkView {
  readonly benchmarkId: string;
  readonly status: string;
  readonly sourceKind: string;
  readonly correlationMode: string;
  readonly resultArtifactId: string | null;
  /**
   * The identity FR-100 seals variants into, or `null` from a server that does
   * not send one. Never recomputed here: the hash is the server's statement
   * about a document, and a second opinion computed in the browser would
   * disagree the first time either side changed its canonicalization.
   */
  readonly manifestContentHash: string | null;
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

function parseFrozenVariant(value: unknown, field: string): FrozenVariant {
  const record = requireRecord(value, field);
  return {
    kind: requireString(record["kind"], `${field}.kind`),
    text: requireString(record["text"], `${field}.text`),
  };
}

/**
 * The frozen set, or `null` — and `null` is a real answer, not a parse failure.
 *
 * A malformed set is *not* silently treated as absent: "no variants were ever
 * frozen" and "the frozen set did not arrive in the expected shape" are
 * different facts, and rendering the second as the first would offer a person a
 * freeze control for a suite that has already been sealed.
 */
function parseFrozenVariants(value: unknown, field: string): FrozenVariantsView | null {
  if (value === null || value === undefined) {
    return null;
  }
  const record = requireRecord(value, field);
  const approval = requireRecord(record["approval"], `${field}.approval`);
  return {
    canonicalIntent: requireString(record["canonical_intent"], `${field}.canonical_intent`),
    variants: requireArray(record["variants"], `${field}.variants`).map((entry, index) =>
      parseFrozenVariant(entry, `${field}.variants[${String(index)}]`),
    ),
    reviewer: requireString(approval["reviewer"], `${field}.approval.reviewer`),
    approvedAt: requireString(approval["approved_at"], `${field}.approval.approved_at`),
    actor: requireString(approval["actor"], `${field}.approval.actor`),
    note: optionalString(approval["note"]) ?? "",
  };
}

function parseManifest(value: unknown, field: string): BenchmarkManifestView {
  const record = isRecord(value) ? value : {};
  return {
    frozenVariants: parseFrozenVariants(record["frozen_variants"], `${field}.frozen_variants`),
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
    manifestContentHash: optionalString(record["manifest_content_hash"]),
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

/** One row of the suite listing — enough to identify and choose, no more. */
export interface BenchmarkSummary {
  readonly benchmarkId: string;
  readonly status: string;
  readonly sourceKind: string;
  readonly correlationMode: string;
  readonly resultArtifactId: string | null;
  readonly createdAt: string;
}

function parseSummary(value: unknown, field: string): BenchmarkSummary {
  const record = requireRecord(value, field);
  return {
    benchmarkId: requireString(record["benchmark_id"], `${field}.benchmark_id`),
    status: requireString(record["status"], `${field}.status`),
    sourceKind: requireString(record["source_kind"], `${field}.source_kind`),
    correlationMode: requireString(record["correlation_mode"], `${field}.correlation_mode`),
    resultArtifactId: optionalString(record["result_artifact_id"]),
    createdAt: requireString(record["created_at"], `${field}.created_at`),
  };
}

export async function listBenchmarks(signal?: AbortSignal): Promise<readonly BenchmarkSummary[]> {
  return await request("/benchmarks", {
    parse: (value) =>
      requireArray(requireRecord(value, "benchmarks")["benchmarks"], "benchmarks").map(
        (entry, index) => parseSummary(entry, `benchmarks[${String(index)}]`),
      ),
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function readBenchmark(
  benchmarkId: string,
  signal?: AbortSignal,
): Promise<BenchmarkView> {
  return await request(`/benchmarks/${benchmarkId}`, {
    parse: parseBenchmark,
    ...(signal === undefined ? {} : { signal }),
  });
}

/**
 * Create a suite to import into.
 *
 * `source_kind` is the operator's declaration about where the trials came
 * from, and AC-16 forbids ever representing a recorded fixture as a live
 * execution — so it is asked for rather than defaulted silently.
 */
export async function createBenchmark(
  sourceKind: string,
  correlationMode: string,
  signal?: AbortSignal,
): Promise<string> {
  return await request("/benchmarks", {
    method: "POST",
    body: { source_kind: sourceKind, correlation_mode: correlationMode },
    parse: (value) =>
      requireString(requireRecord(value, "benchmark")["benchmark_id"], "benchmark_id"),
    ...(signal === undefined ? {} : { signal }),
  });
}

/**
 * Import an evaluator report, sending the operator's chosen bytes unchanged.
 *
 * `rawBody` rather than a parsed object: the route reads the raw body so
 * FR-117's size cap precedes the JSON parser, and re-serializing here would
 * measure a different document than the one the operator selected.
 */
export async function importEvaluatorReport(
  benchmarkId: string,
  report: string,
  signal?: AbortSignal,
): Promise<number> {
  return await request(`/benchmarks/${benchmarkId}/imports`, {
    method: "POST",
    rawBody: report,
    parse: (value) => {
      const record = requireRecord(value, "import");
      const count = record["trial_count"];
      return typeof count === "number" ? count : 0;
    },
    ...(signal === undefined ? {} : { signal }),
  });
}

/** One variant on its way to review: the text, and what kind it is. */
export interface VariantDraft {
  readonly kind: string;
  readonly text: string;
}

/**
 * A reviewer's decision about one screened set (FR-100).
 *
 * The whole set travels, not only the approved part: an approval is a statement
 * about specific texts and the server binds it to a fingerprint of all of them,
 * so a request carrying only the survivors would describe a review nobody did.
 *
 * There is no `actor` field, deliberately. An agent cannot approve its own
 * material, and the server does not accept a claim about who approved beyond
 * the reviewer's name — so this client has nothing to assert on anyone's behalf.
 */
export interface VariantApprovalRequest {
  readonly canonicalIntent: string;
  readonly variants: readonly VariantDraft[];
  readonly approvedIndices: readonly number[];
  readonly reviewer: string;
  readonly note?: string;
}

/** What the server says it sealed, and into which manifest. */
export interface FrozenVariantsReceipt {
  readonly benchmarkId: string;
  readonly frozenVariantsContentHash: string;
  readonly manifestContentHash: string;
  readonly variantCount: number;
  readonly reviewer: string;
}

function requireCount(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value < 0) {
    throw new Error(`${field} must be a count`);
  }
  return value;
}

export function parseFrozenVariantsReceipt(value: unknown): FrozenVariantsReceipt {
  const record = requireRecord(value, "frozen_variants");
  return {
    benchmarkId: requireString(record["benchmark_id"], "benchmark_id"),
    frozenVariantsContentHash: requireString(
      record["frozen_variants_content_hash"],
      "frozen_variants_content_hash",
    ),
    manifestContentHash: requireString(record["manifest_content_hash"], "manifest_content_hash"),
    variantCount: requireCount(record["variant_count"], "variant_count"),
    reviewer: requireString(record["reviewer"], "reviewer"),
  };
}

/**
 * What the live model drafted, and which model drafted it.
 *
 * `approved` is deliberately part of the shape rather than assumed. Generation
 * approves nothing — FR-100 requires a named person to do that — and a client
 * that treated a successful draft as a decision would be recording consent
 * nobody gave. The field is read back rather than hard-coded here so a server
 * that ever started claiming otherwise would be visible instead of ignored.
 */
export interface DraftedVariants {
  readonly canonicalIntent: string;
  readonly variants: readonly VariantDraft[];
  readonly modelProvider: string;
  readonly modelName: string;
  readonly approved: boolean;
}

function parseVariantDraft(value: unknown, field: string): VariantDraft {
  const record = requireRecord(value, field);
  return {
    kind: requireString(record["kind"], `${field}.kind`),
    text: requireString(record["text"], `${field}.text`),
  };
}

export function parseDraftedVariants(value: unknown): DraftedVariants {
  const record = requireRecord(value, "intent_variants");
  return {
    canonicalIntent: requireString(record["canonical_intent"], "canonical_intent"),
    variants: requireArray(record["variants"], "variants").map((entry, index) =>
      parseVariantDraft(entry, `variants[${String(index)}]`),
    ),
    modelProvider: requireString(record["model_provider"], "model_provider"),
    modelName: requireString(record["model_name"], "model_name"),
    approved: record["approved"] === true,
  };
}

/**
 * Ask the configured live model for candidate variants (FR-100's generate step).
 *
 * Nothing is sealed by this call and nothing is approved by it: the response is
 * a draft a person edits, ticks, and then submits through
 * `freezeBenchmarkVariants`, which remains the only way a variant reaches a
 * manifest. Deployments without a live backend refuse it by name, which is the
 * default deployment and not an error — the hand-written path is unaffected.
 *
 * There is no model, provider, or endpoint parameter. Which backend the harness
 * talks to is server-controlled configuration; a caller who could name one could
 * point the harness at an arbitrary origin.
 */
export async function generateIntentVariants(
  benchmarkId: string,
  canonicalIntent: string,
  count: number,
  signal?: AbortSignal,
): Promise<DraftedVariants> {
  return await request(`/benchmarks/${benchmarkId}/intent-variants`, {
    method: "POST",
    body: { canonical_intent: canonicalIntent, count },
    parse: parseDraftedVariants,
    ...(signal === undefined ? {} : { signal }),
  });
}

/**
 * Seal the approved variants into the suite's content-hashed manifest (FR-100).
 *
 * Once per suite. The server refuses a second freeze rather than overwriting,
 * because overwriting is the "generation rerun between repetitions" the
 * requirement forbids — so this function never retries a refusal as though it
 * were a transient failure.
 */
export async function freezeBenchmarkVariants(
  benchmarkId: string,
  approval: VariantApprovalRequest,
  signal?: AbortSignal,
): Promise<FrozenVariantsReceipt> {
  return await request(`/benchmarks/${benchmarkId}/frozen-variants`, {
    method: "POST",
    body: {
      canonical_intent: approval.canonicalIntent,
      variants: approval.variants.map((variant) => ({ kind: variant.kind, text: variant.text })),
      approved_indices: approval.approvedIndices,
      reviewer: approval.reviewer,
      note: approval.note ?? "",
    },
    parse: parseFrozenVariantsReceipt,
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function replayBenchmark(benchmarkId: string, signal?: AbortSignal): Promise<void> {
  await request(`/benchmarks/${benchmarkId}/replay`, {
    method: "POST",
    parse: () => undefined,
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function finalizeBenchmark(benchmarkId: string, signal?: AbortSignal): Promise<void> {
  await request(`/benchmarks/${benchmarkId}/finalize`, {
    method: "POST",
    parse: () => undefined,
    ...(signal === undefined ? {} : { signal }),
  });
}

/* -- repeated trials and the correlation they produce (§26.5, §9.9) -------- */

/** One entry of a closed vocabulary's distribution: the result, and how often. */
export interface ResultCount {
  readonly result: string;
  readonly trials: number;
}

/**
 * One variant's two layers, over its repetitions.
 *
 * The evaluator's distribution and the observed distribution are separate
 * fields under separate vocabularies, and nothing here blends them into a
 * single accuracy — the whole point of the view is that the two can disagree.
 * As everywhere else in this module, every count and rate is the server's; a
 * rate recomputed in the browser would be a second opinion on a published
 * number.
 */
export interface CorrelatedPopulation {
  readonly label: string;
  readonly trials: number;
  readonly counts: MatrixCounts;
  readonly metrics: BenchmarkMetrics;
  readonly evaluatorDistribution: readonly ResultCount[];
  readonly observedDistribution: readonly ResultCount[];
  readonly agreementTrials: number;
  readonly agreementRate: Rate;
  /** The evaluator passed the call; the observed state disagreed. */
  readonly overstatedTrials: number;
  readonly overstatedRate: Rate;
  /** The evaluator failed the call; the observed state passed anyway. */
  readonly understatedTrials: number;
  readonly understatedRate: Rate;
}

export interface BenchmarkCorrelation {
  readonly benchmarkId: string;
  readonly status: string;
  readonly sourceKind: string;
  readonly correlationMode: string;
  /**
   * How many repetitions one batch may run, as the server states it.
   *
   * Read rather than mirrored: the ceiling is enforced where the repetitions
   * are written, and a copy in this bundle would start offering an action the
   * server refuses the first time the two drifted.
   */
  readonly repetitionCeiling: number;
  /**
   * Whether an evaluator report can reach this deployment at all.
   *
   * The left axis of the matrix is an *imported* evaluator verdict, so with the
   * import module off there is nothing to correlate against and there never
   * will be. Rendering an empty axis without saying so would read as "the
   * evaluator found nothing" — a measurement claim — rather than as a
   * configuration fact.
   */
  readonly evaluatorImportAvailable: boolean;
  /** Empty means no trials have run — not that the trials that ran agreed. */
  readonly populations: readonly CorrelatedPopulation[];
}

function parseResultCounts(value: unknown, field: string): readonly ResultCount[] {
  return requireArray(value, field).map((entry, index) => {
    const record = requireRecord(entry, `${field}[${String(index)}]`);
    return {
      result: requireString(record["result"], `${field}[${String(index)}].result`),
      trials: requireNumber(record["trials"], `${field}[${String(index)}].trials`),
    };
  });
}

function parseCorrelatedPopulation(value: unknown, field: string): CorrelatedPopulation {
  const record = requireRecord(value, field);
  return {
    label: requireString(record["label"], `${field}.label`),
    trials: requireNumber(record["trials"], `${field}.trials`),
    counts: parseCounts(record["counts"], `${field}.counts`),
    metrics: parseMetrics(record["metrics"], `${field}.metrics`),
    evaluatorDistribution: parseResultCounts(
      record["evaluator_distribution"],
      `${field}.evaluator_distribution`,
    ),
    observedDistribution: parseResultCounts(
      record["observed_distribution"],
      `${field}.observed_distribution`,
    ),
    agreementTrials: requireNumber(record["agreement_trials"], `${field}.agreement_trials`),
    agreementRate: parseRate(record["agreement_rate"], `${field}.agreement_rate`),
    overstatedTrials: requireNumber(record["overstated_trials"], `${field}.overstated_trials`),
    overstatedRate: parseRate(record["overstated_rate"], `${field}.overstated_rate`),
    understatedTrials: requireNumber(record["understated_trials"], `${field}.understated_trials`),
    understatedRate: parseRate(record["understated_rate"], `${field}.understated_rate`),
  };
}

export function parseCorrelation(value: unknown): BenchmarkCorrelation {
  const record = requireRecord(value, "correlation");
  return {
    benchmarkId: requireString(record["benchmark_id"], "benchmark_id"),
    status: requireString(record["status"], "status"),
    sourceKind: requireString(record["source_kind"], "source_kind"),
    correlationMode: requireString(record["correlation_mode"], "correlation_mode"),
    repetitionCeiling: requireNumber(record["repetition_ceiling"], "repetition_ceiling"),
    // Absent is read as unavailable rather than as available: a client that
    // assumed the capability was there would offer an import the server refuses.
    evaluatorImportAvailable: record["evaluator_import_available"] === true,
    populations: requireArray(record["populations"], "populations").map((entry, index) =>
      parseCorrelatedPopulation(entry, `populations[${String(index)}]`),
    ),
  };
}

export async function readBenchmarkCorrelation(
  benchmarkId: string,
  signal?: AbortSignal,
): Promise<BenchmarkCorrelation> {
  return await request(`/benchmarks/${benchmarkId}/correlation`, {
    parse: parseCorrelation,
    ...(signal === undefined ? {} : { signal }),
  });
}

/** What one repetition concluded. There is no call-level field, deliberately:
 *  a repetition re-runs the observation, never the evaluator's verdict. */
export interface RepeatedTrialReceipt {
  readonly externalTrialId: string;
  readonly repetitionIndex: number;
  readonly outcomeResult: string;
  readonly eligibility: string;
  readonly exclusionReason: string | null;
}

export interface RepeatedTrialsReceipt {
  readonly trials: number;
  readonly repetitions: readonly RepeatedTrialReceipt[];
}

export function parseRepeatedTrials(value: unknown): RepeatedTrialsReceipt {
  const record = requireRecord(value, "repeated_trials");
  return {
    trials: requireNumber(record["trials"], "trials"),
    repetitions: requireArray(record["repetitions"], "repetitions").map((entry, index) => {
      const repetition = requireRecord(entry, `repetitions[${String(index)}]`);
      return {
        externalTrialId: requireString(
          repetition["external_trial_id"],
          `repetitions[${String(index)}].external_trial_id`,
        ),
        repetitionIndex: requireNumber(
          repetition["repetition_index"],
          `repetitions[${String(index)}].repetition_index`,
        ),
        outcomeResult: requireString(
          repetition["outcome_result"],
          `repetitions[${String(index)}].outcome_result`,
        ),
        eligibility: requireString(
          repetition["eligibility"],
          `repetitions[${String(index)}].eligibility`,
        ),
        exclusionReason: optionalString(repetition["exclusion_reason"]),
      };
    }),
  };
}

/**
 * Run one variant again, N times (§26.5).
 *
 * `variantIndex` is sent only when the caller has one: FR-100 freezes the set in
 * a definite order, and naming a position the suite never froze is refused
 * rather than guessed at. A suite with no frozen set omits it, and the
 * correlation view then groups the repetitions by their scenario.
 *
 * The count is not clamped here. The ceiling is the server's, and silently
 * sending fewer trials than the operator asked for would leave them describing a
 * rate over a population twice the size of the one that ran.
 */
export async function runRepeatedTrials(
  benchmarkId: string,
  request_: {
    readonly sourceExternalTrialId: string;
    readonly trials: number;
    readonly variantIndex?: number;
  },
  signal?: AbortSignal,
): Promise<RepeatedTrialsReceipt> {
  return await request(`/benchmarks/${benchmarkId}/repeated-trials`, {
    method: "POST",
    body: {
      source_external_trial_id: request_.sourceExternalTrialId,
      trials: request_.trials,
      ...(request_.variantIndex === undefined ? {} : { variant_index: request_.variantIndex }),
    },
    parse: parseRepeatedTrials,
    ...(signal === undefined ? {} : { signal }),
  });
}
