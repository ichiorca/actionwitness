"""FR-092's matrix and metrics, computed from integer counts.

Pure and synchronous: given normalized trials, this module produces the counts,
the five metrics, and the per-scenario and per-failure-profile breakdowns. It
reads no clock, no database, and no configuration, so the same trials always
produce the same numbers — which is what makes a benchmark artifact worth
hashing.

**Classification happens once, here.** `classify` decides which of the four
cells a trial lands in, or that it is excluded, and everything else counts what
it decided. Two places deciding eligibility is how a matrix ends up not summing
to its own denominator.

**Nothing in this module adds two populations together.** FR-093 and §9.9 forbid
pooling across correlation modes, source kinds, scenario modes, and failure
profiles. `tally` counts one population; `break_down` produces several, each
labelled. There is deliberately no function that merges them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from actionwitness_core.benchmarks.enums import (
    EXCLUDED_CALL_LEVEL_RESULTS,
    EXCLUDED_OUTCOME_RESULTS,
    OUTCOME_PASS_RESULTS,
    CallLevelResult,
    ExclusionReason,
    TrialEligibility,
)
from actionwitness_core.benchmarks.models import (
    BenchmarkMetrics,
    MatrixCounts,
    NormalizedTrial,
    Population,
    Rate,
)

__all__ = ["break_down", "classify", "exclusion_for", "metrics_for", "tally"]


def exclusion_for(trial: NormalizedTrial) -> ExclusionReason | None:
    """Why FR-092 keeps this trial out of the denominator, or `None`.

    Order matters only in what it reports, not in whether the trial is
    excluded: an evaluator error is named before a missing outcome because it
    is the earlier failure, and a reader chasing coverage wants the first thing
    that went wrong rather than the last.
    """
    if trial.call_level_result in EXCLUDED_CALL_LEVEL_RESULTS:
        return ExclusionReason.EVALUATOR_ERROR
    if trial.outcome_result in EXCLUDED_OUTCOME_RESULTS:
        # An outcome that errored and one that never ran are different facts,
        # and coverage is only actionable if it says which.
        return (
            ExclusionReason.OUTCOME_ERROR
            if trial.outcome_result.value == "error"
            else ExclusionReason.OUTCOME_NOT_REACHED
        )
    return None


def classify(trial: NormalizedTrial) -> tuple[TrialEligibility, ExclusionReason | None]:
    """Whether a trial counts, and if not, why not.

    A trial the importer already marked excluded stays excluded with the reason
    it was given — `UNBOUND` and `MISSING_TRAJECTORY` are decided upstream,
    where the binding is known, and re-deriving them here would need this pure
    module to know about bindings.
    """
    if trial.eligibility is TrialEligibility.EXCLUDED:
        return TrialEligibility.EXCLUDED, trial.exclusion_reason
    reason = exclusion_for(trial)
    if reason is not None:
        return TrialEligibility.EXCLUDED, reason
    return TrialEligibility.ELIGIBLE, None


def tally(trials: Iterable[NormalizedTrial]) -> MatrixCounts:
    """The two-by-two plus coverage, over exactly one population.

    `error_trials` counts the excluded trials whose reason was an error in
    either layer or in this harness — FR-092's "disclosed subset", never added
    to the total a second time.
    """
    cells = {
        (True, True): 0,
        (True, False): 0,
        (False, True): 0,
        (False, False): 0,
    }
    excluded = 0
    errors = 0
    error_reasons = {
        ExclusionReason.EVALUATOR_ERROR,
        ExclusionReason.OUTCOME_ERROR,
        ExclusionReason.HARNESS_ERROR,
    }

    for trial in trials:
        eligibility, reason = classify(trial)
        if eligibility is TrialEligibility.EXCLUDED:
            excluded += 1
            if reason in error_reasons:
                errors += 1
            continue
        call_pass = trial.call_level_result is CallLevelResult.PASSED
        outcome_pass = trial.outcome_result in OUTCOME_PASS_RESULTS
        cells[(call_pass, outcome_pass)] += 1

    return MatrixCounts(
        call_level_pass_outcome_pass=cells[(True, True)],
        call_level_pass_outcome_fail=cells[(True, False)],
        call_level_fail_outcome_pass=cells[(False, True)],
        call_level_fail_outcome_fail=cells[(False, False)],
        excluded_trials=excluded,
        error_trials=errors,
    )


def metrics_for(counts: MatrixCounts) -> BenchmarkMetrics:
    """FR-092's five metrics from those counts.

    Every rate is built by `Rate.of`, so a zero denominator produces `null`
    rather than a number. `silent_outcome_failure_rate` has its own denominator
    — call-level passes, not eligible trials — because the question it answers
    is "of the trials the evaluator was happy with, how many were actually
    broken?", and dividing by the wrong population would answer a different one.
    """
    eligible = counts.eligible_trials
    return BenchmarkMetrics(
        call_level_pass_rate=Rate.of(counts.call_level_passes, eligible),
        outcome_pass_rate=Rate.of(counts.outcome_passes, eligible),
        end_to_end_success_rate=Rate.of(counts.call_level_pass_outcome_pass, eligible),
        silent_outcome_failure_rate=Rate.of(
            counts.call_level_pass_outcome_fail, counts.call_level_passes
        ),
        incremental_outcome_failure_trials=counts.call_level_pass_outcome_fail,
    )


def break_down(
    trials: Sequence[NormalizedTrial],
    key: Callable[[NormalizedTrial], str | None],
) -> tuple[Population, ...]:
    """One labelled population per distinct key, sorted by label.

    A trial whose key is `None` is left out rather than gathered under an
    invented label: FR-093 says missing metadata is `null`, never inferred, and
    an "unknown" bucket would be a population nobody chose to measure.

    Sorted so the artifact hashes identically across runs — a dict's insertion
    order is a property of the input file, not of the benchmark.
    """
    groups: dict[str, list[NormalizedTrial]] = {}
    for trial in trials:
        label = key(trial)
        if label is None:
            continue
        groups.setdefault(label, []).append(trial)

    populations = []
    for label in sorted(groups):
        counts = tally(groups[label])
        populations.append(Population(label=label, counts=counts, metrics=metrics_for(counts)))
    return tuple(populations)
