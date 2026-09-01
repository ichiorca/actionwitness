"""008-T2 — the core benchmark vocabulary, matrix, and state machine.

Spec v1.9 §9.9, §16.4, FR-092, FR-093.

The arithmetic is worth testing hard because every number it produces is
published as evidence. Three properties carry the weight:

- the four cells sum to `eligible_trials`, and `eligible + excluded == total`;
- `error_trials` is a *subset* of exclusions, never a third population;
- a rate over an empty population is `null`, never `0.0000`.

The last one is the easiest to get wrong and the most misleading when wrong:
`0.0000` reads as "we looked and found none", which is a finding nobody made.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from actionwitness_core.benchmarks.enums import (
    BenchmarkStatus,
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    OutcomeTrialResult,
    TrialEligibility,
    outcome_from_layer_result,
)
from actionwitness_core.benchmarks.matrix import break_down, classify, metrics_for, tally
from actionwitness_core.benchmarks.models import (
    BenchmarkManifest,
    BenchmarkReport,
    MatrixCounts,
    NormalizedTrial,
    Rate,
    TrialBinding,
)
from actionwitness_core.benchmarks.states import can_transition, require_transition
from actionwitness_core.kernel import CoreError
from actionwitness_core.reports.enums import LayerResult
from pydantic import ValidationError

pytestmark = pytest.mark.unit

REPLAY = CorrelationMode.IMPORTED_TRAJECTORY_REPLAY
BROWSER = CorrelationMode.EXECUTED_BROWSER


def _trial(
    trial_id: str,
    call: CallLevelResult,
    outcome: OutcomeTrialResult,
    *,
    scenario: str = "scenario-a",
    profile: str | None = None,
    eligibility: TrialEligibility = TrialEligibility.ELIGIBLE,
    reason: ExclusionReason | None = None,
) -> NormalizedTrial:
    return NormalizedTrial(
        external_trial_id=trial_id,
        scenario_id=scenario,
        correlation_mode=REPLAY,
        call_level_result=call,
        outcome_result=outcome,
        eligibility=eligibility,
        exclusion_reason=reason,
        failure_profile=profile,
    )


# --- the four cells ----------------------------------------------------------


def test_each_trial_lands_in_exactly_one_cell() -> None:
    """§9.9's matrix, one trial per interpretation."""
    # Arrange
    trials = [
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
        _trial("b", CallLevelResult.PASSED, OutcomeTrialResult.FAILED),
        _trial("c", CallLevelResult.FAILED, OutcomeTrialResult.PASSED),
        _trial("d", CallLevelResult.FAILED, OutcomeTrialResult.FAILED),
    ]

    # Act
    counts = tally(trials)

    # Assert
    assert counts.call_level_pass_outcome_pass == 1
    assert counts.call_level_pass_outcome_fail == 1
    assert counts.call_level_fail_outcome_pass == 1
    assert counts.call_level_fail_outcome_fail == 1
    assert counts.eligible_trials == 4


def test_passed_with_warnings_is_an_outcome_pass_and_stays_visible() -> None:
    """FR-092 normalizes it to pass; the warning itself is not erased, it simply
    is not a failure."""
    # Arrange
    trials = [_trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED_WITH_WARNINGS)]

    # Act
    counts = tally(trials)

    # Assert
    assert counts.call_level_pass_outcome_pass == 1
    assert counts.call_level_pass_outcome_fail == 0


def test_the_cells_sum_to_the_eligible_denominator() -> None:
    """FR-092: "the four matrix cells shall sum to `eligible_trials`"."""
    # Arrange
    trials = [
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
        _trial("b", CallLevelResult.PASSED, OutcomeTrialResult.FAILED),
        _trial("c", CallLevelResult.ERROR, OutcomeTrialResult.PASSED),
        _trial("d", CallLevelResult.PASSED, OutcomeTrialResult.NOT_REACHED),
    ]

    # Act
    counts = tally(trials)

    # Assert
    assert counts.eligible_trials == 2
    assert counts.excluded_trials == 2
    assert counts.total_trials == 4


# --- exclusions --------------------------------------------------------------


def test_an_evaluator_error_is_excluded_rather_than_counted_as_a_failure() -> None:
    """FR-092: "evaluator or adapter `error` is excluded".

    Counting it as a call-level failure would make a crashed evaluator look like
    a bad model.
    """
    # Arrange
    trial = _trial("a", CallLevelResult.ERROR, OutcomeTrialResult.PASSED)

    # Act
    eligibility, reason = classify(trial)

    # Assert
    assert eligibility is TrialEligibility.EXCLUDED
    assert reason is ExclusionReason.EVALUATOR_ERROR


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (OutcomeTrialResult.ERROR, ExclusionReason.OUTCOME_ERROR),
        (OutcomeTrialResult.NOT_REACHED, ExclusionReason.OUTCOME_NOT_REACHED),
    ],
)
def test_an_unusable_outcome_names_which_kind_it_was(
    outcome: OutcomeTrialResult, expected: ExclusionReason
) -> None:
    """ "It broke" and "we never asked" are different facts, and coverage is only
    actionable if it says which."""
    # Arrange
    trial = _trial("a", CallLevelResult.PASSED, outcome)

    # Act
    _, reason = classify(trial)

    # Assert
    assert reason is expected


def test_errors_are_a_disclosed_subset_of_exclusions() -> None:
    """FR-092: "not a third additive population".

    Two exclusions, one of them an error — so the total must be four, not five.
    """
    # Arrange
    trials = [
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
        _trial("b", CallLevelResult.FAILED, OutcomeTrialResult.FAILED),
        _trial("c", CallLevelResult.ERROR, OutcomeTrialResult.PASSED),
        _trial("d", CallLevelResult.PASSED, OutcomeTrialResult.NOT_REACHED),
    ]

    # Act
    counts = tally(trials)

    # Assert
    assert counts.excluded_trials == 2
    assert counts.error_trials == 1
    assert counts.total_trials == 4


def test_an_upstream_exclusion_keeps_the_reason_it_was_given() -> None:
    """`UNBOUND` is decided where bindings are known; the matrix must not
    relabel it as something it can derive."""
    # Arrange
    trial = _trial(
        "a",
        CallLevelResult.PASSED,
        OutcomeTrialResult.NOT_REACHED,
        eligibility=TrialEligibility.EXCLUDED,
        reason=ExclusionReason.UNBOUND,
    )

    # Act
    eligibility, reason = classify(trial)

    # Assert
    assert eligibility is TrialEligibility.EXCLUDED
    assert reason is ExclusionReason.UNBOUND


def test_error_trials_cannot_exceed_excluded_trials() -> None:
    """The subset relation is a model invariant, not a convention."""
    # Arrange / Act / Assert
    with pytest.raises(ValidationError):
        MatrixCounts(excluded_trials=1, error_trials=2)


# --- the five metrics --------------------------------------------------------


def test_every_metric_matches_its_formula() -> None:
    """FR-092's five formulas over a population chosen so no two agree."""
    # Arrange — 2 both-pass, 3 silent defects, 1 fail/pass, 4 fail/fail.
    counts = MatrixCounts(
        call_level_pass_outcome_pass=2,
        call_level_pass_outcome_fail=3,
        call_level_fail_outcome_pass=1,
        call_level_fail_outcome_fail=4,
    )

    # Act
    metrics = metrics_for(counts)

    # Assert
    assert counts.eligible_trials == 10
    assert metrics.call_level_pass_rate.display == "0.5000"  # 5/10
    assert metrics.outcome_pass_rate.display == "0.3000"  # 3/10
    assert metrics.end_to_end_success_rate.display == "0.2000"  # 2/10
    assert metrics.silent_outcome_failure_rate.display == "0.6000"  # 3/5
    assert metrics.incremental_outcome_failure_trials == 3


def test_the_silent_failure_rate_divides_by_call_level_passes() -> None:
    """Its denominator is the call-level passes, not the eligible trials.

    The question is "of the trials the evaluator was happy with, how many were
    actually broken?" — dividing by the whole population answers a different one.
    """
    # Arrange
    counts = MatrixCounts(call_level_pass_outcome_pass=1, call_level_pass_outcome_fail=1)

    # Act
    metrics = metrics_for(counts)

    # Assert
    assert metrics.silent_outcome_failure_rate.denominator == 2
    assert metrics.call_level_pass_rate.denominator == 2
    assert metrics.silent_outcome_failure_rate.display == "0.5000"


def test_every_rate_is_null_when_no_trial_is_eligible() -> None:
    """FR-092: "all rate fields are `null` when `eligible_trials` is zero".

    `0.0000` would claim a measurement nobody made.
    """
    # Arrange
    counts = MatrixCounts(excluded_trials=3, error_trials=1)

    # Act
    metrics = metrics_for(counts)

    # Assert
    for rate in (
        metrics.call_level_pass_rate,
        metrics.outcome_pass_rate,
        metrics.end_to_end_success_rate,
        metrics.silent_outcome_failure_rate,
    ):
        assert rate.value is None
        assert rate.display is None
    assert metrics.incremental_outcome_failure_trials == 0


def test_the_silent_failure_rate_is_null_when_nothing_passed_at_call_level() -> None:
    """FR-092's second null case, distinct from the first: trials are eligible,
    but none of them is in the denominator this rate needs."""
    # Arrange
    counts = MatrixCounts(call_level_fail_outcome_pass=2, call_level_fail_outcome_fail=1)

    # Act
    metrics = metrics_for(counts)

    # Assert
    assert metrics.call_level_pass_rate.display == "0.0000"
    assert metrics.silent_outcome_failure_rate.value is None


def test_a_rate_is_exact_rather_than_a_rounded_float() -> None:
    """ "Rates shall use unrounded integer counts as inputs and display four
    decimal places" — 1/3 must be 0.3333, from the integers, every time."""
    # Arrange / Act
    rate = Rate.of(1, 3)

    # Assert
    assert rate.value == Decimal("0.3333")
    assert rate.display == "0.3333"
    assert (rate.numerator, rate.denominator) == (1, 3)


def test_a_rate_cannot_claim_a_number_over_an_empty_population() -> None:
    """The invariant, not just the constructor's habit."""
    # Arrange / Act / Assert
    with pytest.raises(ValidationError):
        Rate(numerator=0, denominator=0, value=Decimal("0.0000"), display="0.0000")


# --- populations stay separate ----------------------------------------------


def test_breakdowns_are_labelled_and_never_pooled() -> None:
    """FR-093 and §9.9 forbid pooling; each population carries its own label and
    its own denominator."""
    # Arrange
    trials = [
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED, scenario="one"),
        _trial("b", CallLevelResult.PASSED, OutcomeTrialResult.FAILED, scenario="two"),
        _trial("c", CallLevelResult.FAILED, OutcomeTrialResult.FAILED, scenario="two"),
    ]

    # Act
    populations = break_down(trials, lambda trial: trial.scenario_id)

    # Assert
    assert [group.label for group in populations] == ["one", "two"]
    assert populations[0].counts.eligible_trials == 1
    assert populations[1].counts.eligible_trials == 2
    assert populations[1].metrics.call_level_pass_rate.display == "0.5000"


def test_a_trial_with_no_profile_is_left_out_rather_than_bucketed() -> None:
    """FR-093: missing metadata is `null`, never inferred. An "unknown" bucket
    would be a population nobody chose to measure."""
    # Arrange
    trials = [
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED, profile="discount"),
        _trial("b", CallLevelResult.PASSED, OutcomeTrialResult.PASSED, profile=None),
    ]

    # Act
    populations = break_down(trials, lambda trial: trial.failure_profile)

    # Assert
    assert [group.label for group in populations] == ["discount"]
    assert populations[0].counts.total_trials == 1


def test_a_report_refuses_to_mix_correlation_modes() -> None:
    """§9.9: the two modes "shall never be aggregated into one rate"."""
    # Arrange
    mixed = (
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
        NormalizedTrial(
            external_trial_id="b",
            scenario_id="scenario-a",
            correlation_mode=BROWSER,
            call_level_result=CallLevelResult.PASSED,
            outcome_result=OutcomeTrialResult.PASSED,
            eligibility=TrialEligibility.ELIGIBLE,
        ),
    )
    manifest = BenchmarkManifest(
        source_kind="recorded_fixture", correlation_mode=REPLAY, benchmark_id="bench"
    )
    counts = tally(mixed)

    # Act / Assert
    with pytest.raises(ValidationError):
        BenchmarkReport(
            benchmark_id="bench",
            manifest=manifest,
            counts=counts,
            metrics=metrics_for(counts),
            trials=mixed,
        )


# --- bindings ----------------------------------------------------------------


def test_a_browser_binding_names_its_outcome_run() -> None:
    """FR-091: bound one-to-one to "the exact completed outcome `run_id`"."""
    # Arrange / Act
    binding = TrialBinding(external_trial_id="t1", correlation_mode=BROWSER, outcome_run_id="run-1")

    # Assert
    assert binding.evaluation_run_id is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"correlation_mode": BROWSER},
        {"correlation_mode": BROWSER, "outcome_run_id": "r", "evaluation_run_id": "e"},
        {"correlation_mode": REPLAY, "outcome_run_id": "r"},
    ],
)
def test_a_binding_carries_exactly_one_reference_for_its_mode(kwargs: dict) -> None:
    """FR-091 gives each trial exactly one mode; a binding that names both, or
    neither, is not a one-to-one binding."""
    # Arrange / Act / Assert
    with pytest.raises(ValidationError):
        TrialBinding(external_trial_id="t1", **kwargs)


# --- the outcome mapping -----------------------------------------------------


@pytest.mark.parametrize(
    ("layer", "expected"),
    [
        (LayerResult.PASSED, OutcomeTrialResult.PASSED),
        (LayerResult.PASSED_WITH_WARNINGS, OutcomeTrialResult.PASSED_WITH_WARNINGS),
        (LayerResult.FAILED, OutcomeTrialResult.FAILED),
        (LayerResult.ERROR, OutcomeTrialResult.ERROR),
        (LayerResult.BLOCKED_SAFELY, OutcomeTrialResult.NOT_REACHED),
        (LayerResult.NOT_EVALUATED, OutcomeTrialResult.NOT_REACHED),
        (None, OutcomeTrialResult.NOT_REACHED),
    ],
)
def test_every_layer_result_maps_somewhere_explicit(
    layer: LayerResult | None, expected: OutcomeTrialResult
) -> None:
    """A safely blocked mutation is not a business failure.

    Mapping it to `FAILED` would count correct refusal as a defect — the exact
    misreading this product exists to prevent.
    """
    # Arrange / Act / Assert
    assert outcome_from_layer_result(layer) is expected


# --- the state machine -------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (BenchmarkStatus.DRAFT, BenchmarkStatus.READY),
        (BenchmarkStatus.READY, BenchmarkStatus.RUNNING),
        (BenchmarkStatus.RUNNING, BenchmarkStatus.COMPLETED),
        (BenchmarkStatus.DRAFT, BenchmarkStatus.CANCELLED),
        (BenchmarkStatus.READY, BenchmarkStatus.ERROR),
    ],
)
def test_permitted_transitions_are_permitted(
    current: BenchmarkStatus, target: BenchmarkStatus
) -> None:
    """§16.4's table, read forwards."""
    # Arrange / Act / Assert
    assert can_transition(current, target) is True


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (BenchmarkStatus.READY, BenchmarkStatus.DRAFT),
        (BenchmarkStatus.COMPLETED, BenchmarkStatus.RUNNING),
        (BenchmarkStatus.CANCELLED, BenchmarkStatus.READY),
        (BenchmarkStatus.ERROR, BenchmarkStatus.COMPLETED),
        (BenchmarkStatus.DRAFT, BenchmarkStatus.RUNNING),
    ],
)
def test_forbidden_transitions_are_refused(
    current: BenchmarkStatus, target: BenchmarkStatus
) -> None:
    """No path back to `draft` — bindings freeze at `ready` — and every terminal
    state stays terminal."""
    # Arrange / Act / Assert
    assert can_transition(current, target) is False
    with pytest.raises(CoreError):
        require_transition(current, target)


def test_a_browser_suite_may_finalize_straight_from_ready() -> None:
    """§16.4: "its outcome runs already exist"."""
    # Arrange / Act
    target = require_transition(
        BenchmarkStatus.READY, BenchmarkStatus.COMPLETED, correlation_mode=BROWSER
    )

    # Assert
    assert target is BenchmarkStatus.COMPLETED


def test_a_replay_suite_must_pass_through_running() -> None:
    """Its outcome evidence does not exist until the replay produces it, so
    finalizing from `ready` would publish a matrix over outcomes nobody
    observed."""
    # Arrange / Act / Assert
    with pytest.raises(CoreError):
        require_transition(
            BenchmarkStatus.READY, BenchmarkStatus.COMPLETED, correlation_mode=REPLAY
        )


# --- the artifact ------------------------------------------------------------


def test_a_report_hashes_over_everything_but_its_own_hash() -> None:
    """FR-089: the stored document and the live object hash identically, so a
    reader can verify a benchmark they were handed."""
    # Arrange
    trials = (_trial("a", CallLevelResult.PASSED, OutcomeTrialResult.FAILED),)
    counts = tally(trials)
    report = BenchmarkReport(
        benchmark_id="bench",
        manifest=BenchmarkManifest(
            source_kind="recorded_fixture", correlation_mode=REPLAY, benchmark_id="bench"
        ),
        counts=counts,
        metrics=metrics_for(counts),
        trials=trials,
    )

    # Act
    stored = report.as_stored_document()

    # Assert
    assert stored["content_hash"] == report.content_hash()
    assert "content_hash" not in report.canonical_document()
