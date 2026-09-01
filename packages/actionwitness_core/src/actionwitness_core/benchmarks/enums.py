"""Closed benchmark vocabulary: source kinds, correlation modes, suite status,
call-level results, eligibility, and exclusion reasons.

Spec v1.9 §9.9 (the dual-layer matrix), §16.4 (the suite state machine), §17.1
(`benchmark_suites`, `benchmark_trials`), FR-091 (explicit correlation), FR-092
(matrix normalization), FR-093 (the reproducibility manifest).

Three of these carry distinctions the milestone lives on.

**`SourceKind` is never inferred.** FR-093: "every suite contains exactly one
source kind; incompatible source kinds are never pooled", and AC-16 requires the
application to "never represent either as a live execution". A recorded fixture
and a live model run answer different questions, and a benchmark that pooled
them would report a rate over two populations that were never comparable.

**`CorrelationMode` populations are separate, always.** §9.9: the two modes
"shall never be aggregated into one rate". They are separate because the
evidence underneath them is different in kind — one binds to a browser
execution that actually happened, the other replays a recorded trajectory.

**`CallLevelResult.ERROR` is not a failure.** FR-092: "evaluator or adapter
`error` is excluded". An evaluator that crashed tells you nothing about whether
the model picked the right tool, and counting it as a failure would make a flaky
harness look like a bad model. Exclusion is the honest reading, and the same
reasoning is why `error_trials` is "a disclosed subset of `excluded_trials`, not
a third additive population".
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from actionwitness_core.reports.enums import LayerResult

__all__ = [
    "ENUM_REGISTRATIONS",
    "EXCLUDED_CALL_LEVEL_RESULTS",
    "EXCLUDED_OUTCOME_RESULTS",
    "OUTCOME_PASS_RESULTS",
    "TERMINAL_BENCHMARK_STATUSES",
    "BenchmarkStatus",
    "CallLevelResult",
    "CorrelationMode",
    "ExclusionReason",
    "OutcomeTrialResult",
    "SourceKind",
    "TrialEligibility",
    "VariantKind",
    "outcome_from_layer_result",
]


class SourceKind(StrEnum):
    """FR-093's three source kinds. Exactly one per suite, never pooled."""

    EXTERNAL_IMPORT = "external_import"
    LIVE_MODEL_RUN = "live_model_run"
    RECORDED_FIXTURE = "recorded_fixture"


SOURCE_KIND_DESCRIPTIONS: Mapping[SourceKind, str] = {
    SourceKind.EXTERNAL_IMPORT: (
        "A report produced elsewhere by a supported evaluator and imported here. "
        "The evidence is real but this execution did not produce it."
    ),
    SourceKind.LIVE_MODEL_RUN: (
        "A configured model executed during this benchmark. Tier 3 only, and never "
        "claimed for a replayed or checked-in report."
    ),
    SourceKind.RECORDED_FIXTURE: (
        "A redacted report committed to the repository so CI and the offline "
        "fallback reproduce the benchmark without a credential. Always labeled as "
        "such; never presented as evidence that a live call occurred."
    ),
}


class CorrelationMode(StrEnum):
    """FR-091's two modes. Exactly one per trial, and populations stay separate."""

    EXECUTED_BROWSER = "executed_browser"
    IMPORTED_TRAJECTORY_REPLAY = "imported_trajectory_replay"


CORRELATION_MODE_DESCRIPTIONS: Mapping[CorrelationMode, str] = {
    CorrelationMode.EXECUTED_BROWSER: (
        "The imported trial is bound one-to-one to the exact completed outcome run "
        "that browser execution produced."
    ),
    CorrelationMode.IMPORTED_TRAJECTORY_REPLAY: (
        "The imported, redacted, allowlisted tool trajectory is replayed in a fresh "
        "isolated eval workspace through the same registered target adapter."
    ),
}


class BenchmarkStatus(StrEnum):
    """§16.4's suite lifecycle."""

    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


BENCHMARK_STATUS_DESCRIPTIONS: Mapping[BenchmarkStatus, str] = {
    BenchmarkStatus.DRAFT: "Manifest created; imports or bindings incomplete.",
    BenchmarkStatus.READY: (
        "All intended imports and bindings validate. Bindings become immutable here."
    ),
    BenchmarkStatus.RUNNING: "Imported trajectories are replaying.",
    BenchmarkStatus.COMPLETED: (
        "The immutable benchmark artifact is finalized. Terminal, and the suite "
        "cannot be recalculated in place — a change requires a new suite."
    ),
    BenchmarkStatus.CANCELLED: "Workspace reset or user cancelled nonterminal work.",
    BenchmarkStatus.ERROR: (
        "Import, replay, or finalization could not complete safely. Terminal, and "
        "never a partial result: finalization commits everything or nothing."
    ),
}

#: §16.4's terminal states.
TERMINAL_BENCHMARK_STATUSES: frozenset[BenchmarkStatus] = frozenset(
    {BenchmarkStatus.COMPLETED, BenchmarkStatus.CANCELLED, BenchmarkStatus.ERROR}
)


class CallLevelResult(StrEnum):
    """§17.1's normalized call-level result for one imported trial.

    Deliberately three values, not two. Folding `ERROR` into `FAILED` would put
    a crashed evaluator into the failure count and make the model look worse
    than the evidence supports.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


CALL_LEVEL_RESULT_DESCRIPTIONS: Mapping[CallLevelResult, str] = {
    CallLevelResult.PASSED: (
        "Every required selection, argument, and order constraint in the trial "
        "passed (FR-092). A partial pass is a fail, not a pass."
    ),
    CallLevelResult.FAILED: "At least one required constraint failed.",
    CallLevelResult.ERROR: (
        "The evaluator or adapter errored, so the trial says nothing about call-level "
        "reliability. Excluded from the matrix rather than counted as a failure."
    ),
}

#: FR-092: results that keep a trial out of the two-by-two denominator.
EXCLUDED_CALL_LEVEL_RESULTS: frozenset[CallLevelResult] = frozenset({CallLevelResult.ERROR})


class OutcomeTrialResult(StrEnum):
    """§17.1's `outcome_result` column domain, exactly.

    Four of these are `LayerResult` values and one is not: `not_reached` says
    the outcome layer never produced a verdict for this trial, which is a
    statement about *this benchmark* rather than about any run. Adding it to
    `LayerResult` would put a benchmark-only concept into the vocabulary every
    report layer shares, so it lives here and `outcome_from_layer_result` is
    the one place the two meet.
    """

    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    FAILED = "failed"
    ERROR = "error"
    NOT_REACHED = "not_reached"


OUTCOME_TRIAL_RESULT_DESCRIPTIONS: Mapping[OutcomeTrialResult, str] = {
    OutcomeTrialResult.PASSED: "The deterministic engine passed the contract. Outcome pass.",
    OutcomeTrialResult.PASSED_WITH_WARNINGS: (
        "No critical failure, warnings exist. FR-092 normalizes this to outcome "
        "pass; the warning counts stay visible rather than being absorbed."
    ),
    OutcomeTrialResult.FAILED: "The deterministic engine failed the contract. Outcome fail.",
    OutcomeTrialResult.ERROR: (
        "The outcome layer errored, so it carries no verdict about the target. Excluded."
    ),
    OutcomeTrialResult.NOT_REACHED: (
        "The outcome layer never ran for this trial — no binding, no replay, or an "
        "incomplete bound run. Excluded, and never read as a pass."
    ),
}

#: FR-092: "deterministic `passed` and `passed_with_warnings` normalize to
#: outcome pass, `failed` normalizes to outcome fail, and `error` or
#: `not_reached` is excluded."
OUTCOME_PASS_RESULTS: frozenset[OutcomeTrialResult] = frozenset(
    {OutcomeTrialResult.PASSED, OutcomeTrialResult.PASSED_WITH_WARNINGS}
)
EXCLUDED_OUTCOME_RESULTS: frozenset[OutcomeTrialResult] = frozenset(
    {OutcomeTrialResult.ERROR, OutcomeTrialResult.NOT_REACHED}
)


def outcome_from_layer_result(result: LayerResult | None) -> OutcomeTrialResult:
    """The one place `LayerResult` and `OutcomeTrialResult` meet.

    `None` — no outcome layer ran — becomes `NOT_REACHED` rather than an error,
    because "we never asked" and "we asked and it broke" are different facts and
    the exclusion reasons record them separately.

    `blocked_safely` and `not_evaluated` map to `NOT_REACHED` too: neither is a
    verdict on the contract, and §17.1's column has no room for them. Mapping
    either to `FAILED` would count a correctly refused mutation as a business
    failure, which is the exact misreading this product exists to prevent.
    """
    match result:
        case None:
            return OutcomeTrialResult.NOT_REACHED
        case LayerResult.PASSED:
            return OutcomeTrialResult.PASSED
        case LayerResult.PASSED_WITH_WARNINGS:
            return OutcomeTrialResult.PASSED_WITH_WARNINGS
        case LayerResult.FAILED:
            return OutcomeTrialResult.FAILED
        case LayerResult.ERROR:
            return OutcomeTrialResult.ERROR
        case LayerResult.BLOCKED_SAFELY | LayerResult.NOT_EVALUATED:
            return OutcomeTrialResult.NOT_REACHED


class TrialEligibility(StrEnum):
    """§17.1: whether a trial counts toward the matrix."""

    ELIGIBLE = "eligible"
    EXCLUDED = "excluded"


TRIAL_ELIGIBILITY_DESCRIPTIONS: Mapping[TrialEligibility, str] = {
    TrialEligibility.ELIGIBLE: (
        "Both layers produced a usable result, so the trial lands in exactly one "
        "of the four matrix cells."
    ),
    TrialEligibility.EXCLUDED: (
        "Evidence was missing, errored, or insufficient for the declared correlation "
        "mode. Counted in coverage and never silently downgraded or rebound."
    ),
}


class ExclusionReason(StrEnum):
    """Why a trial was excluded — recorded, because coverage without a reason is
    a number nobody can act on.

    FR-091 is explicit that a trial without sufficient evidence for its declared
    mode is "`excluded`, not silently downgraded or rebound", so each reason
    here names a specific missing thing rather than a general failure.
    """

    EVALUATOR_ERROR = "evaluator_error"
    UNBOUND = "unbound"
    MISSING_TRAJECTORY = "missing_trajectory"
    OUTCOME_NOT_REACHED = "outcome_not_reached"
    OUTCOME_ERROR = "outcome_error"
    HARNESS_ERROR = "harness_error"


EXCLUSION_REASON_DESCRIPTIONS: Mapping[ExclusionReason, str] = {
    ExclusionReason.EVALUATOR_ERROR: (
        "The imported trial reported an evaluator error, so its call-level layer "
        "carries no verdict."
    ),
    ExclusionReason.UNBOUND: (
        "No explicit one-to-one binding exists for this trial. FR-091 forbids "
        "guessing one from order, timestamps, or similar text."
    ),
    ExclusionReason.MISSING_TRAJECTORY: (
        "An `imported_trajectory_replay` trial carries no replayable trajectory, so "
        "there is nothing to execute through the adapter."
    ),
    ExclusionReason.OUTCOME_NOT_REACHED: (
        "The outcome layer never produced a verdict for this trial — the replay did "
        "not run, or the bound run never completed."
    ),
    ExclusionReason.OUTCOME_ERROR: (
        "The outcome layer errored. Like an evaluator error, this says nothing about "
        "the target and is excluded rather than counted as a failure."
    ),
    ExclusionReason.HARNESS_ERROR: (
        "This harness failed to complete the trial. Disclosed rather than dropped, so "
        "coverage cannot silently improve when the harness breaks."
    ),
}


class VariantKind(StrEnum):
    """FR-100's three kinds. All three are named because a set of six
    paraphrases would not test what the benchmark is for."""

    PARAPHRASED = "paraphrased"
    AMBIGUOUS = "ambiguous"
    ADVERSARIAL = "adversarial"


VARIANT_KIND_DESCRIPTIONS: Mapping[VariantKind, str] = {
    VariantKind.PARAPHRASED: (
        "The same request in different words. Tests whether tool selection survives "
        "ordinary rewording."
    ),
    VariantKind.AMBIGUOUS: (
        "A request that under-specifies something the contract cares about. Tests "
        "what the model does when the intent does not settle the question."
    ),
    VariantKind.ADVERSARIAL: (
        "A request that invites the wrong action while staying within what a user "
        "might plausibly type. Never an instruction to bypass a safeguard — that is "
        "refused at screening, not generated on purpose."
    ),
}


ENUM_REGISTRATIONS: tuple[tuple[str, str, type[StrEnum], Mapping[StrEnum, str]], ...] = (
    ("source_kind", "spec §17.1 / FR-093", SourceKind, SOURCE_KIND_DESCRIPTIONS),
    ("correlation_mode", "spec §9.9 / FR-091", CorrelationMode, CORRELATION_MODE_DESCRIPTIONS),
    ("benchmark_status", "spec §16.4", BenchmarkStatus, BENCHMARK_STATUS_DESCRIPTIONS),
    ("call_level_result", "spec §17.1 / FR-092", CallLevelResult, CALL_LEVEL_RESULT_DESCRIPTIONS),
    (
        "trial_eligibility",
        "spec §17.1 / FR-092",
        TrialEligibility,
        TRIAL_ELIGIBILITY_DESCRIPTIONS,
    ),
    ("exclusion_reason", "spec §9.9 / FR-091", ExclusionReason, EXCLUSION_REASON_DESCRIPTIONS),
    ("variant_kind", "spec §12.11 / FR-100", VariantKind, VARIANT_KIND_DESCRIPTIONS),
    (
        "outcome_trial_result",
        "spec §17.1 / FR-092",
        OutcomeTrialResult,
        OUTCOME_TRIAL_RESULT_DESCRIPTIONS,
    ),
)
