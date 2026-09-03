"""The per-variant correlation view (§9.9, FR-092, FR-100, §26.5).

The question this module answers is the one repetition makes askable: over N
runs of the *same* frozen intent variant, how often did the evaluator's
call-level verdict and the independently observed business state agree, and how
often did the evaluator pass a call the observed state did not.

Five shapes carry it, because each is a different way of being wrong:

- **empty** — no trials at all must produce no populations, not a population of
  zeroes. "Nothing has run" and "everything ran and found nothing" are different
  findings, and a zero-filled group would erase the difference.
- **one trial** — a single sample is reported as a rate over one, never withheld
  and never rounded into a claim about a population.
- **all agree** — the disagreement cell is `0` with a real rate, not `null`.
- **all disagree** — the headline cell is the whole population, and the rate is
  `1.0000`.
- **mixed** — the case the product exists for: the same variant passes some
  repetitions and fails others, and the outcome distribution shows the spread a
  single sample would have hidden.

Everything here runs with no database, no clock, and no suite: the function
takes trials and returns numbers.
"""

from __future__ import annotations

import pytest
from actionwitness_core.benchmarks.enums import (
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    OutcomeTrialResult,
    TrialEligibility,
)
from actionwitness_core.benchmarks.models import NormalizedTrial
from actionwitness_service.application.benchmark_correlation import (
    VariantTrial,
    correlate,
    variant_trials,
)

pytestmark = pytest.mark.unit

VARIANT = "Please add a ceramic mug and use the SAVE20 code."
OTHER = "I would like a mug, discounted somehow."


def _trial(
    trial_id: str,
    *,
    call: CallLevelResult = CallLevelResult.PASSED,
    outcome: OutcomeTrialResult = OutcomeTrialResult.PASSED,
    eligible: bool = True,
    reason: ExclusionReason | None = None,
) -> NormalizedTrial:
    return NormalizedTrial(
        external_trial_id=trial_id,
        scenario_id="adds a mug",
        correlation_mode=CorrelationMode.IMPORTED_TRAJECTORY_REPLAY,
        call_level_result=call,
        outcome_result=outcome,
        eligibility=TrialEligibility.ELIGIBLE if eligible else TrialEligibility.EXCLUDED,
        exclusion_reason=reason,
    )


def _repetitions(
    label: str, outcomes: tuple[OutcomeTrialResult, ...], *, call: CallLevelResult
) -> list[VariantTrial]:
    """N repetitions of one variant, differing only in what was observed."""
    return [
        VariantTrial(
            label=label,
            trial=_trial(f"{label}#repetition-{index}", call=call, outcome=outcome),
        )
        for index, outcome in enumerate(outcomes, start=1)
    ]


def _cell(population, result: str) -> int:
    """One entry of the observed distribution, by result name."""
    return dict(population.observed_distribution)[result]


# --- empty -------------------------------------------------------------------


def test_no_trials_produce_no_populations() -> None:
    """A zero-filled group would read as a measured result over a population
    nobody ran."""
    # Arrange / Act
    populations = correlate([])

    # Assert
    assert populations == ()


# --- a single trial ----------------------------------------------------------


def test_one_trial_is_reported_as_a_rate_over_one() -> None:
    """A single sample is still a rate — over a denominator of one, which is
    exactly what a reader needs to see before trusting it."""
    # Arrange
    trials = _repetitions(VARIANT, (OutcomeTrialResult.FAILED,), call=CallLevelResult.PASSED)

    # Act
    (population,) = correlate(trials)

    # Assert
    assert population.label == VARIANT
    assert population.trials == 1
    assert population.overstated_trials == 1
    assert population.overstated_rate.denominator == 1
    assert population.overstated_rate.display == "1.0000"
    assert population.agreement_trials == 0
    assert population.agreement_rate.display == "0.0000"


# --- everything agrees -------------------------------------------------------


def test_when_both_layers_agree_the_disagreement_cell_is_zero_and_not_null() -> None:
    """`0` and `null` are different answers.

    `0.0000` says "we measured and found no silent failures"; `null` says "there
    was no population to measure". A view that reported the second when it meant
    the first would understate a clean result into no result at all.
    """
    # Arrange — five repetitions, all passing both layers.
    trials = _repetitions(VARIANT, (OutcomeTrialResult.PASSED,) * 5, call=CallLevelResult.PASSED)

    # Act
    (population,) = correlate(trials)

    # Assert
    assert population.trials == 5
    assert population.agreement_trials == 5
    assert population.agreement_rate.display == "1.0000"
    assert population.overstated_trials == 0
    assert population.overstated_rate.display == "0.0000"
    assert population.understated_trials == 0
    # No trial failed at call level, so the understated rate has no denominator
    # and must stay null rather than becoming a measured zero.
    assert population.understated_rate.display is None


def test_a_warning_pass_counts_as_an_agreement() -> None:
    """FR-092 normalizes `passed_with_warnings` to an outcome pass, and the
    correlation view must not invent a stricter rule of its own — a variant that
    passed with warnings did not disagree with an evaluator that passed it."""
    # Arrange
    trials = _repetitions(
        VARIANT,
        (OutcomeTrialResult.PASSED, OutcomeTrialResult.PASSED_WITH_WARNINGS),
        call=CallLevelResult.PASSED,
    )

    # Act
    (population,) = correlate(trials)

    # Assert
    assert population.agreement_trials == 2
    assert population.overstated_trials == 0
    # The distribution still keeps the two apart: the warning did not vanish
    # into the pass it normalizes to.
    assert _cell(population, "passed") == 1
    assert _cell(population, "passed_with_warnings") == 1


# --- everything disagrees ----------------------------------------------------


def test_when_every_repetition_disagrees_the_headline_rate_is_one() -> None:
    """The product's claim in its strongest form: the evaluator scored every call
    correct, and the independently observed state disagreed every time."""
    # Arrange
    trials = _repetitions(VARIANT, (OutcomeTrialResult.FAILED,) * 4, call=CallLevelResult.PASSED)

    # Act
    (population,) = correlate(trials)

    # Assert
    assert population.overstated_trials == 4
    assert population.overstated_rate.display == "1.0000"
    assert population.agreement_trials == 0
    assert population.agreement_rate.display == "0.0000"
    assert dict(population.evaluator_distribution)["passed"] == 4
    assert _cell(population, "failed") == 4


def test_the_other_disagreement_is_counted_separately() -> None:
    """An evaluator that failed a call the observed state passed is a
    disagreement too — and folding it in with the cell above would let a
    pessimistic evaluator hide silent damage behind its own false alarms."""
    # Arrange
    trials = _repetitions(VARIANT, (OutcomeTrialResult.PASSED,) * 3, call=CallLevelResult.FAILED)

    # Act
    (population,) = correlate(trials)

    # Assert
    assert population.understated_trials == 3
    assert population.understated_rate.display == "1.0000"
    assert population.overstated_trials == 0
    # No call-level pass, so FR-092's silent-failure rate has no denominator.
    assert population.overstated_rate.display is None


# --- mixed -------------------------------------------------------------------


def test_repetitions_of_one_variant_show_the_spread_a_single_sample_hides() -> None:
    """The whole reason to run a variant more than once.

    Sampled once this variant is a coin toss reported as a fact. Sampled five
    times it is a 0.4 silent-failure rate, and the outcome distribution says
    which repetitions went which way.
    """
    # Arrange — the same evaluator verdict throughout, because the evaluator
    # scored this variant once; only the observed state is re-sampled.
    trials = _repetitions(
        VARIANT,
        (
            OutcomeTrialResult.PASSED,
            OutcomeTrialResult.FAILED,
            OutcomeTrialResult.PASSED,
            OutcomeTrialResult.FAILED,
            OutcomeTrialResult.PASSED,
        ),
        call=CallLevelResult.PASSED,
    )

    # Act
    (population,) = correlate(trials)

    # Assert
    assert population.trials == 5
    assert population.overstated_trials == 2
    assert population.overstated_rate.display == "0.4000"
    assert population.agreement_trials == 3
    assert population.agreement_rate.display == "0.6000"
    assert _cell(population, "passed") == 3
    assert _cell(population, "failed") == 2
    # A distribution that omitted its zeroes would leave a reader unable to tell
    # "never errored" from "the error count was not reported".
    assert _cell(population, "error") == 0
    assert _cell(population, "not_reached") == 0


def test_an_excluded_repetition_leaves_the_denominator_alone() -> None:
    """FR-092 keeps errors out of the denominator so a broken harness cannot be
    read as a broken target — and a repetition the harness could not run is
    exactly that case."""
    # Arrange
    trials = [
        *_repetitions(
            VARIANT,
            (OutcomeTrialResult.PASSED, OutcomeTrialResult.FAILED),
            call=CallLevelResult.PASSED,
        ),
        VariantTrial(
            label=VARIANT,
            trial=_trial(
                f"{VARIANT}#repetition-3",
                outcome=OutcomeTrialResult.NOT_REACHED,
                eligible=False,
                reason=ExclusionReason.HARNESS_ERROR,
            ),
        ),
    ]

    # Act
    (population,) = correlate(trials)

    # Assert — three recorded, two counted, and the excluded one disclosed.
    assert population.trials == 3
    assert population.counts.eligible_trials == 2
    assert population.counts.excluded_trials == 1
    assert population.counts.error_trials == 1
    assert population.overstated_rate.denominator == 2
    assert population.overstated_rate.display == "0.5000"


def test_two_variants_stay_two_populations_and_are_sorted() -> None:
    """§9.9 forbids pooling populations, so there is no total to reach for — and
    the order is the labels' own, so two identical suites produce identical
    documents."""
    # Arrange
    trials = [
        *_repetitions(OTHER, (OutcomeTrialResult.PASSED,), call=CallLevelResult.PASSED),
        *_repetitions(VARIANT, (OutcomeTrialResult.FAILED,), call=CallLevelResult.PASSED),
    ]

    # Act
    populations = correlate(trials)

    # Assert
    assert [population.label for population in populations] == sorted([OTHER, VARIANT])
    assert {population.trials for population in populations} == {1}


def test_the_document_keeps_the_two_layers_apart() -> None:
    """The serialized view is what the UI reads, and it must not offer a single
    blended accuracy: the evaluator's distribution and the observed distribution
    are separate keys, under separate vocabularies."""
    # Arrange
    trials = _repetitions(VARIANT, (OutcomeTrialResult.FAILED,), call=CallLevelResult.PASSED)

    # Act
    document = correlate(trials)[0].canonical_document()

    # Assert
    assert document["evaluator_distribution"] == [
        {"result": "passed", "trials": 1},
        {"result": "failed", "trials": 0},
        {"result": "error", "trials": 0},
    ]
    assert {"result": "failed", "trials": 1} in document["observed_distribution"]
    assert document["overstated_trials"] == 1
    assert document["overstated_rate"] == {"numerator": 1, "denominator": 1, "value": "1.0000"}


# --- labelling ---------------------------------------------------------------


def test_a_repetition_is_labelled_with_the_variant_text_it_ran() -> None:
    """A reader should see the words the agent was given, not an index."""
    # Arrange
    rows = [
        {
            "external_trial_id": "adds a mug#0#repetition-1",
            "scenario_id": "adds a mug",
            "correlation_mode": "imported_trajectory_replay",
            "call_level_result": "passed",
            "outcome_result": "failed",
            "eligibility": "eligible",
            "exclusion_reason": None,
            "contract_content_hash": None,
            "scenario_mode": None,
            "failure_profile": None,
            "outcome_run_id": None,
            "evaluation_run_id": "evr_1",
            "metadata_json": "{}",
            "variant_index": 1,
        }
    ]
    frozen = {
        "variants": [{"kind": "paraphrased", "text": VARIANT}, {"kind": "ambiguous", "text": OTHER}]
    }

    # Act
    labelled = variant_trials(rows, frozen_variants=frozen)

    # Assert
    assert [entry.label for entry in labelled] == [OTHER]


def test_a_trial_with_no_variant_is_labelled_by_its_scenario() -> None:
    """Normalization already treats the test name as the shared intent that
    repeated trials of one test repeat, so that is the population an imported
    trial belongs to."""
    # Arrange
    rows = [
        {
            "external_trial_id": "adds a mug#0",
            "scenario_id": "adds a mug",
            "correlation_mode": "imported_trajectory_replay",
            "call_level_result": "passed",
            "outcome_result": "not_reached",
            "eligibility": "excluded",
            "exclusion_reason": "outcome_not_reached",
            "contract_content_hash": None,
            "scenario_mode": None,
            "failure_profile": None,
            "outcome_run_id": None,
            "evaluation_run_id": None,
            "metadata_json": "{}",
            "variant_index": None,
        }
    ]

    # Act
    labelled = variant_trials(rows, frozen_variants=None)

    # Assert
    assert [entry.label for entry in labelled] == ["adds a mug"]


def test_a_variant_the_manifest_cannot_resolve_is_not_folded_into_the_scenario() -> None:
    """Regrouping it under the scenario would merge a repetition into a
    population it does not belong to. FR-093's rule that missing metadata is
    never inferred applies to a label as much as to a field."""
    # Arrange
    rows = [
        {
            "external_trial_id": "adds a mug#0#repetition-1",
            "scenario_id": "adds a mug",
            "correlation_mode": "imported_trajectory_replay",
            "call_level_result": "passed",
            "outcome_result": "passed",
            "eligibility": "eligible",
            "exclusion_reason": None,
            "contract_content_hash": None,
            "scenario_mode": None,
            "failure_profile": None,
            "outcome_run_id": None,
            "evaluation_run_id": "evr_1",
            "metadata_json": "{}",
            "variant_index": 4,
        }
    ]

    # Act
    labelled = variant_trials(rows, frozen_variants={"variants": []})

    # Assert
    assert [entry.label for entry in labelled] == ["variant 4"]
