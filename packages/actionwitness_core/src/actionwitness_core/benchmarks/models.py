"""Benchmark records: manifests, normalized trials, the matrix, and metrics.

Spec v1.9 §9.9 (the diagnostic matrix), §17.1 (`benchmark_suites`,
`benchmark_trials`), FR-092 (matrix and metrics), FR-093 (the reproducibility
manifest), FR-094 (the derived artifact).

**Rates carry their own arithmetic.** FR-092: "rates shall use unrounded integer
counts as inputs and display four decimal places". A `Rate` therefore keeps the
numerator and denominator it came from, an exact `Decimal`, and the presentation
string separately. A float would make the fourth decimal place a rounding
accident, and a bare string would leave a reader unable to check the claim
against the counts.

**A null rate is a real value.** All rate fields are `null` when
`eligible_trials` is zero, and `silent_outcome_failure_rate` is also `null` when
there are no call-level passes. `0.0000` would say "no silent defects were
found"; `null` says "this question has no answer over this population", and the
two are different findings.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from actionwitness_core.benchmarks.enums import (
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    OutcomeTrialResult,
    SourceKind,
    TrialEligibility,
)
from actionwitness_core.kernel import ContractError, CoreErrorCode, CoreModel, JsonValue
from actionwitness_core.security.canonical import document_content_hash

__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "RATE_DECIMAL_PLACES",
    "BenchmarkManifest",
    "BenchmarkMetrics",
    "BenchmarkReport",
    "MatrixCounts",
    "NormalizedTrial",
    "Population",
    "Rate",
    "TrialBinding",
]

type Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
type ContentHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
type SchemaVersion = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+$")]

#: FR-089: every artifact carries its schema version.
BENCHMARK_SCHEMA_VERSION = "1.0"
MANIFEST_SCHEMA_VERSION = "1.0"

#: FR-092: "display four decimal places".
RATE_DECIMAL_PLACES = 4

_QUANTUM = Decimal(1).scaleb(-RATE_DECIMAL_PLACES)


class Rate(CoreModel):
    """One FR-092 rate, with the integers it was computed from.

    Constructed through `of` rather than by hand so the invariant that `value`
    actually equals `numerator / denominator` cannot drift — a rate whose
    displayed number disagrees with its own counts is worse than no rate.
    """

    numerator: Annotated[int, Field(ge=0)]
    denominator: Annotated[int, Field(ge=0)]
    #: `None` when the denominator is zero. Never `0`, which would be a claim.
    value: Decimal | None = None
    #: The four-decimal presentation string, or `None` alongside a null value.
    display: str | None = None

    @classmethod
    def of(cls, numerator: int, denominator: int) -> Rate:
        """A rate from two counts, or a null rate when there is no population."""
        if denominator == 0:
            return cls(numerator=numerator, denominator=0)
        exact = (Decimal(numerator) / Decimal(denominator)).quantize(_QUANTUM)
        return cls(
            numerator=numerator,
            denominator=denominator,
            value=exact,
            display=f"{exact:.{RATE_DECIMAL_PLACES}f}",
        )

    @model_validator(mode="after")
    def _value_and_display_agree(self) -> Rate:
        if (self.value is None) != (self.display is None):
            raise ContractError(
                "a rate must carry both a value and a display string, or neither",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        if self.denominator == 0 and self.value is not None:
            raise ContractError(
                "a rate over an empty population must be null, never a number",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            # Serialized as the presentation string, so a JSON reader never sees
            # a float that lost the fourth decimal place.
            "value": self.display,
        }


class MatrixCounts(CoreModel):
    """§9.9's two-by-two, plus the coverage counts around it.

    The four cells are separate fields rather than a nested mapping because
    `call_level_pass_outcome_fail` is the product's headline signal and deserves
    a name a reader can search for.
    """

    call_level_pass_outcome_pass: Annotated[int, Field(ge=0)] = 0
    call_level_pass_outcome_fail: Annotated[int, Field(ge=0)] = 0
    call_level_fail_outcome_pass: Annotated[int, Field(ge=0)] = 0
    call_level_fail_outcome_fail: Annotated[int, Field(ge=0)] = 0
    excluded_trials: Annotated[int, Field(ge=0)] = 0
    #: FR-092: "a disclosed subset of `excluded_trials`, not a third additive
    #: population". Adding it to the total would double-count every errored trial.
    error_trials: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _errors_are_a_subset_of_exclusions(self) -> MatrixCounts:
        if self.error_trials > self.excluded_trials:
            raise ContractError(
                f"error_trials ({self.error_trials}) cannot exceed excluded_trials "
                f"({self.excluded_trials}); FR-092 makes errors a subset, not a "
                "third population",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    @property
    def eligible_trials(self) -> int:
        """FR-092: "the four matrix cells shall sum to `eligible_trials`"."""
        return (
            self.call_level_pass_outcome_pass
            + self.call_level_pass_outcome_fail
            + self.call_level_fail_outcome_pass
            + self.call_level_fail_outcome_fail
        )

    @property
    def total_trials(self) -> int:
        """FR-092: `total_trials = eligible_trials + excluded_trials`."""
        return self.eligible_trials + self.excluded_trials

    @property
    def call_level_passes(self) -> int:
        return self.call_level_pass_outcome_pass + self.call_level_pass_outcome_fail

    @property
    def outcome_passes(self) -> int:
        return self.call_level_pass_outcome_pass + self.call_level_fail_outcome_pass

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "call_level_pass_outcome_pass": self.call_level_pass_outcome_pass,
            "call_level_pass_outcome_fail": self.call_level_pass_outcome_fail,
            "call_level_fail_outcome_pass": self.call_level_fail_outcome_pass,
            "call_level_fail_outcome_fail": self.call_level_fail_outcome_fail,
            "eligible_trials": self.eligible_trials,
            "excluded_trials": self.excluded_trials,
            "error_trials": self.error_trials,
            "total_trials": self.total_trials,
        }


class BenchmarkMetrics(CoreModel):
    """FR-092's five metrics. Four rates and one count."""

    call_level_pass_rate: Rate
    outcome_pass_rate: Rate
    end_to_end_success_rate: Rate
    silent_outcome_failure_rate: Rate
    #: FR-092: "counts trials, not unique root causes". A count, not a rate,
    #: because the answer to "how much did the outcome layer add?" is a number
    #: of trials somebody can go and read.
    incremental_outcome_failure_trials: Annotated[int, Field(ge=0)] = 0

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "call_level_pass_rate": self.call_level_pass_rate.canonical_document(),
            "outcome_pass_rate": self.outcome_pass_rate.canonical_document(),
            "end_to_end_success_rate": self.end_to_end_success_rate.canonical_document(),
            "silent_outcome_failure_rate": (self.silent_outcome_failure_rate.canonical_document()),
            "incremental_outcome_failure_trials": self.incremental_outcome_failure_trials,
        }


class Population(CoreModel):
    """One population's counts and metrics, under the label that defines it.

    §9.9 and FR-093 forbid pooling across correlation modes, source kinds,
    scenario modes, and failure profiles. Making a population carry its own
    label is what stops two of them being added together by a caller who did not
    read the requirement: there is no unlabelled total to reach for.
    """

    label: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    counts: MatrixCounts
    metrics: BenchmarkMetrics

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "label": self.label,
            "counts": self.counts.canonical_document(),
            "metrics": self.metrics.canonical_document(),
        }


class TrialBinding(CoreModel):
    """FR-091's explicit one-to-one binding.

    Exactly one of `outcome_run_id` / `evaluation_run_id` is set, matching the
    declared mode. There is deliberately no field recording *how* the binding
    was chosen, because there is only one permitted way: a person named it.
    """

    external_trial_id: Identifier
    correlation_mode: CorrelationMode
    outcome_run_id: Identifier | None = None
    evaluation_run_id: Identifier | None = None

    @model_validator(mode="after")
    def _exactly_one_reference_for_the_declared_mode(self) -> TrialBinding:
        if self.correlation_mode is CorrelationMode.EXECUTED_BROWSER:
            if self.outcome_run_id is None:
                raise ContractError(
                    "an executed_browser binding names the exact outcome run it was "
                    "produced by; without one there is nothing bound",
                    code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
                )
            if self.evaluation_run_id is not None:
                raise ContractError(
                    "an executed_browser binding cannot also name an evaluation run; "
                    "FR-091 gives each trial exactly one mode",
                    code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
                )
            return self
        if self.outcome_run_id is not None:
            raise ContractError(
                "an imported_trajectory_replay binding cannot name an outcome run; "
                "FR-091 gives each trial exactly one mode",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self


class NormalizedTrial(CoreModel):
    """One imported trial after normalization (§24.7 step 3, §17.1).

    Only allowlisted call-level fields survive normalization. Everything the
    importer does not understand is preserved as `null` in `metadata` rather
    than dropped: FR-093 says "missing unsupported metadata shall be `null`,
    never inferred", and a dropped key and an unsupported key look identical to
    a later reader.
    """

    external_trial_id: Identifier
    scenario_id: Identifier
    correlation_mode: CorrelationMode
    call_level_result: CallLevelResult
    outcome_result: OutcomeTrialResult = OutcomeTrialResult.NOT_REACHED
    eligibility: TrialEligibility = TrialEligibility.EXCLUDED
    exclusion_reason: ExclusionReason | None = None
    contract_content_hash: ContentHash | None = None
    scenario_mode: str | None = None
    failure_profile: str | None = None
    outcome_run_id: Identifier | None = None
    evaluation_run_id: Identifier | None = None
    #: The replayable calls, when the mode needs them. Arguments only — never
    #: code, a URL, or a shell string (FR-086 applies to imported trajectories
    #: exactly as it does to generated cases).
    trajectory: tuple[Mapping[str, JsonValue], ...] = ()
    #: Unsupported upstream fields, explicitly `null`.
    metadata: Mapping[str, JsonValue] = Field(default_factory=dict)
    #: Whether this trial carries a stable address of its own (ADR-0005).
    #:
    #: The pinned reporter's `test.name` and `runIndex` are both *optional*, so
    #: a report may contain trials that nothing can name. Such a trial keeps a
    #: positional id so it can be stored and shown, but `addressable` is
    #: `False` and FR-091 then permits binding it only by explicit operator
    #: selection. Recording this as a field rather than re-deriving it at
    #: binding time is what stops the id's *shape* from being mistaken for
    #: evidence that it identifies anything.
    addressable: bool = True

    @model_validator(mode="after")
    def _an_excluded_trial_says_why(self) -> NormalizedTrial:
        if self.eligibility is TrialEligibility.EXCLUDED and self.exclusion_reason is None:
            raise ContractError(
                f"trial {self.external_trial_id} is excluded with no reason; coverage "
                "without a reason is a number nobody can act on",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        if self.eligibility is TrialEligibility.ELIGIBLE and self.exclusion_reason is not None:
            raise ContractError(
                f"trial {self.external_trial_id} is eligible but carries an exclusion "
                f"reason ({self.exclusion_reason.value})",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "external_trial_id": self.external_trial_id,
            "scenario_id": self.scenario_id,
            "correlation_mode": self.correlation_mode.value,
            "call_level_result": self.call_level_result.value,
            "outcome_result": self.outcome_result.value,
            "eligibility": self.eligibility.value,
            "exclusion_reason": (
                None if self.exclusion_reason is None else self.exclusion_reason.value
            ),
            "contract_content_hash": self.contract_content_hash,
            "scenario_mode": self.scenario_mode,
            "failure_profile": self.failure_profile,
            "outcome_run_id": self.outcome_run_id,
            "evaluation_run_id": self.evaluation_run_id,
            "trajectory": [dict(step) for step in self.trajectory],
            "metadata": dict(self.metadata),
            "addressable": self.addressable,
        }


class BenchmarkManifest(CoreModel):
    """FR-093's reproducibility manifest.

    Every field is recorded rather than derived at read time. A manifest whose
    model name came from today's configuration would describe the wrong run the
    moment the configuration changed, and the artifact is supposed to outlive
    the environment that produced it.
    """

    schema_version: SchemaVersion = MANIFEST_SCHEMA_VERSION
    source_kind: SourceKind
    correlation_mode: CorrelationMode
    benchmark_id: Identifier
    scenario_ids: tuple[Identifier, ...] = ()
    target_fixture: str | None = None
    target_build_commit: str | None = None
    evaluator_name: str | None = None
    evaluator_package: str | None = None
    evaluator_version: str | None = None
    evaluator_command_mode: str | None = None
    model_provider: str | None = None
    model_name: str | None = None
    #: Whatever parameters the evaluator exposed. Absent ones stay absent rather
    #: than being filled with defaults this harness invented.
    model_parameters: Mapping[str, JsonValue] = Field(default_factory=dict)
    run_count: Annotated[int, Field(ge=0)] = 0
    reporter_schema: str | None = None
    normalized_adapter_version: str | None = None
    #: FR-094: the immutable sources this derived artifact references.
    source_artifact_hashes: tuple[ContentHash, ...] = ()

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "source_kind": self.source_kind.value,
            "correlation_mode": self.correlation_mode.value,
            "benchmark_id": self.benchmark_id,
            "scenario_ids": list(self.scenario_ids),
            "target_fixture": self.target_fixture,
            "target_build_commit": self.target_build_commit,
            "evaluator_name": self.evaluator_name,
            "evaluator_package": self.evaluator_package,
            "evaluator_version": self.evaluator_version,
            "evaluator_command_mode": self.evaluator_command_mode,
            "model_provider": self.model_provider,
            "model_name": self.model_name,
            "model_parameters": dict(self.model_parameters),
            "run_count": self.run_count,
            "reporter_schema": self.reporter_schema,
            "normalized_adapter_version": self.normalized_adapter_version,
            "source_artifact_hashes": list(self.source_artifact_hashes),
        }

    def content_hash(self) -> str:
        return document_content_hash(self.canonical_document())


class BenchmarkReport(CoreModel):
    """FR-094's derived artifact: the matrix, metrics, coverage, and manifest.

    It *references* its sources by hash and never contains them. §7's non-goal
    is explicit — "rewriting an immutable source outcome report to embed
    external evaluator data" — so recalculating after a code change creates a
    new report beside the old sources rather than touching them.
    """

    schema_version: SchemaVersion = BENCHMARK_SCHEMA_VERSION
    benchmark_id: Identifier
    manifest: BenchmarkManifest
    counts: MatrixCounts
    metrics: BenchmarkMetrics
    by_scenario: tuple[Population, ...] = ()
    by_failure_profile: tuple[Population, ...] = ()
    #: Every trial, so a reader can check the counts rather than trust them.
    trials: tuple[NormalizedTrial, ...] = ()

    @model_validator(mode="after")
    def _one_source_kind_and_one_mode_throughout(self) -> BenchmarkReport:
        """FR-093: "every suite contains exactly one source kind"; §9.9: the two
        correlation modes "shall never be aggregated into one rate"."""
        modes = {trial.correlation_mode for trial in self.trials}
        if len(modes) > 1:
            raise ContractError(
                f"a benchmark report covers one correlation mode; found "
                f"{sorted(mode.value for mode in modes)}",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        if modes and self.manifest.correlation_mode not in modes:
            raise ContractError(
                f"the manifest declares {self.manifest.correlation_mode.value} but the "
                f"trials are {sorted(mode.value for mode in modes)}",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "benchmark_id": self.benchmark_id,
            "manifest": self.manifest.canonical_document(),
            "counts": self.counts.canonical_document(),
            "metrics": self.metrics.canonical_document(),
            "by_scenario": [group.canonical_document() for group in self.by_scenario],
            "by_failure_profile": [group.canonical_document() for group in self.by_failure_profile],
            "trials": [trial.canonical_document() for trial in self.trials],
        }

    def content_hash(self) -> str:
        """Computed over everything else, last (FR-089)."""
        return document_content_hash(self.canonical_document())

    def as_stored_document(self) -> dict[str, JsonValue]:
        return {**self.canonical_document(), "content_hash": self.content_hash()}
