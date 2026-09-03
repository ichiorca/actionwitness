"""The evaluator's verdicts against the deterministic ones, per intent variant.

Spec v1.9 §9.9 (the dual-layer matrix), FR-092 (counts, rates, and what stays
out of the denominator), FR-100 (frozen intent variants), §26.5 (six variants
with five repeated trials each).

`benchmark_metrics.summarize` already answers "what does this whole suite look
like". This module answers the question repetition makes askable: *for one frozen
variant run N times, how often did the two layers agree, and how often did the
evaluator say the call was correct while the independently observed business
state said it was not.* One trial makes that a fact about one sample; N trials
make it a rate, and a rate is the only form in which a non-deterministic agent's
behaviour can be characterised at all.

**Pure, synchronous, and deterministic.** No database, no clock, no
configuration — rows are handed in already read. Groups come back sorted by
label and every distribution is emitted in its enum's declaration order, so the
same trials always produce the same document.

**Nothing here decides an outcome or recomputes a rate.** Eligibility, the four
cells and the five metrics come from `actionwitness_core.benchmarks.matrix`;
`Rate.of` builds the two rates this view adds. A module that classified trials
its own way would be a second opinion on numbers the finalized artifact
publishes, and the two would disagree the first time either changed.

**There is deliberately no total across variants.** §9.9 and FR-093 forbid
pooling populations, and `matrix.break_down` makes the same refusal by returning
labelled groups with no merge function beside them. A caller who wants one
number has to say which population it is about.

**The two layers stay two layers.** A group carries the evaluator's distribution
and the observed distribution separately, and they are typed by separate enums
(`CallLevelResult`, `OutcomeTrialResult`) all the way down. `overstated_trials`
is the cell where they disagree in the direction the product exists to expose —
the evaluator scored the call correct, and the independently observed state did
not agree. It is never averaged with `understated_trials` into a single
"accuracy", because the two disagreements have opposite consequences for whoever
is deciding whether to trust the agent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from actionwitness_core.benchmarks.enums import CallLevelResult, OutcomeTrialResult
from actionwitness_core.benchmarks.matrix import metrics_for, tally
from actionwitness_core.benchmarks.models import (
    BenchmarkMetrics,
    MatrixCounts,
    NormalizedTrial,
    Rate,
)
from actionwitness_core.kernel import JsonValue

from actionwitness_service.application.benchmark_metrics import trial_from_row

__all__ = [
    "CorrelatedPopulation",
    "VariantTrial",
    "correlate",
    "variant_trials",
]


@dataclass(frozen=True, slots=True)
class VariantTrial:
    """One trial, under the label of the population it belongs to.

    The label is supplied rather than derived: a trial row knows its variant
    *index*, and turning an index into a name needs the frozen manifest, which
    is not this function's to read. Keeping the mapping outside means
    `correlate` can be exercised over hand-built trials with no manifest, no
    suite, and no database.
    """

    label: str
    trial: NormalizedTrial


@dataclass(frozen=True, slots=True)
class CorrelatedPopulation:
    """One variant's two layers, side by side over its repetitions."""

    label: str
    #: Every recorded repetition, eligible or not. `counts.total_trials` is the
    #: same number; it is repeated here so a reader of one group does not have
    #: to know that `MatrixCounts` derives its total.
    trials: int
    counts: MatrixCounts
    metrics: BenchmarkMetrics
    #: What the evaluator reported, counted per `CallLevelResult`. Over every
    #: trial in the group, including the excluded ones: an evaluator error is
    #: still something the evaluator said, and hiding it would make coverage
    #: look better than it is.
    evaluator_distribution: tuple[tuple[str, int], ...]
    #: What the deterministic engine observed, counted per `OutcomeTrialResult`.
    #: This is the distribution that spreads out when the agent, or the target,
    #: is non-deterministic — the reason to run a variant more than once.
    observed_distribution: tuple[tuple[str, int], ...]
    #: Both layers reached the same verdict: the two agreeing cells of the
    #: two-by-two, over the eligible trials.
    agreement_trials: int
    agreement_rate: Rate
    #: The evaluator passed the call and the observed state did not agree. FR-092
    #: already names this rate `silent_outcome_failure_rate` over exactly this
    #: denominator, so the same object is carried here rather than a second
    #: division that could round differently.
    overstated_trials: int
    overstated_rate: Rate
    #: The evaluator failed the call and the observed state passed anyway. Kept
    #: separate: it is a disagreement, but a harmless-looking one, and folding it
    #: in with the cell above would let a suite hide silent damage behind
    #: pessimism.
    understated_trials: int
    understated_rate: Rate

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "label": self.label,
            "trials": self.trials,
            "counts": self.counts.canonical_document(),
            "metrics": self.metrics.canonical_document(),
            "evaluator_distribution": [
                {"result": result, "trials": count} for result, count in self.evaluator_distribution
            ],
            "observed_distribution": [
                {"result": result, "trials": count} for result, count in self.observed_distribution
            ],
            "agreement_trials": self.agreement_trials,
            "agreement_rate": self.agreement_rate.canonical_document(),
            "overstated_trials": self.overstated_trials,
            "overstated_rate": self.overstated_rate.canonical_document(),
            "understated_trials": self.understated_trials,
            "understated_rate": self.understated_rate.canonical_document(),
        }


def correlate(trials: Sequence[VariantTrial]) -> tuple[CorrelatedPopulation, ...]:
    """One labelled population per variant, sorted by label.

    An empty input yields an empty tuple rather than a zero-filled group: "no
    trials have run" and "trials ran and found nothing" are different findings,
    and a caller that received a group of zeroes could not tell them apart.
    """
    groups: dict[str, list[NormalizedTrial]] = {}
    for entry in trials:
        groups.setdefault(entry.label, []).append(entry.trial)
    return tuple(_population(label, groups[label]) for label in sorted(groups))


def _population(label: str, trials: Sequence[NormalizedTrial]) -> CorrelatedPopulation:
    """The two-by-two for one label, plus what repetition adds to it."""
    counts = tally(trials)
    metrics = metrics_for(counts)
    agreement = counts.call_level_pass_outcome_pass + counts.call_level_fail_outcome_fail
    call_level_fails = counts.call_level_fail_outcome_pass + counts.call_level_fail_outcome_fail
    return CorrelatedPopulation(
        label=label,
        trials=counts.total_trials,
        counts=counts,
        metrics=metrics,
        evaluator_distribution=_distribution(
            CallLevelResult, [trial.call_level_result for trial in trials]
        ),
        observed_distribution=_distribution(
            OutcomeTrialResult, [trial.outcome_result for trial in trials]
        ),
        agreement_trials=agreement,
        agreement_rate=Rate.of(agreement, counts.eligible_trials),
        overstated_trials=counts.call_level_pass_outcome_fail,
        # FR-092's own rate over FR-092's own denominator. Recomputing it here
        # would be a second opinion on the number the artifact publishes.
        overstated_rate=metrics.silent_outcome_failure_rate,
        understated_trials=counts.call_level_fail_outcome_pass,
        understated_rate=Rate.of(counts.call_level_fail_outcome_pass, call_level_fails),
    )


def _distribution(vocabulary: Any, observed: Sequence[Any]) -> tuple[tuple[str, int], ...]:
    """Every member of a closed vocabulary and how often it occurred.

    Members with no occurrences are kept. A variant that never errored and a
    variant whose error count was omitted read identically otherwise, and the
    first is a result while the second is a gap in the report.

    Declaration order rather than count order, so two identical suites produce
    identical documents — the same normalization `break_down` applies by sorting
    its labels.
    """
    tallies = dict.fromkeys(vocabulary, 0)
    for value in observed:
        tallies[value] += 1
    return tuple((member.value, count) for member, count in tallies.items())


def variant_trials(
    rows: Sequence[Mapping[str, Any]],
    *,
    frozen_variants: Mapping[str, Any] | None = None,
) -> tuple[VariantTrial, ...]:
    """Label each stored trial row with the population it belongs to.

    A row that records a `variant_index` is a repetition of that frozen variant
    and is labelled with the variant's own text, so a reader sees the words the
    agent was actually given. A row that records none is labelled with its
    `scenario_id`, which normalization already treats as the shared intent that
    repeated trials of one test repeat.

    An index the frozen set cannot resolve falls back to `variant N` rather than
    to the scenario. Silently regrouping it under the scenario would merge a
    repetition into a population it does not belong to, and FR-093's rule that
    missing metadata is never inferred applies to a label as much as to a field.
    """
    texts = _variant_texts(frozen_variants)
    labelled: list[VariantTrial] = []
    for row in rows:
        trial = trial_from_row(row)
        index = _variant_index(row)
        if index is None:
            labelled.append(VariantTrial(label=trial.scenario_id, trial=trial))
            continue
        text = texts[index] if 0 <= index < len(texts) else ""
        labelled.append(
            VariantTrial(label=text or f"variant {index}", trial=trial),
        )
    return tuple(labelled)


def _variant_texts(frozen_variants: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The approved variant texts, in the order the manifest froze them."""
    if frozen_variants is None:
        return ()
    variants = frozen_variants.get("variants")
    if not isinstance(variants, Sequence) or isinstance(variants, str | bytes):
        return ()
    return tuple(
        entry["text"] if isinstance(entry, Mapping) and isinstance(entry.get("text"), str) else ""
        for entry in variants
    )


def _variant_index(row: Mapping[str, Any]) -> int | None:
    """The row's variant position, or `None` when it records none.

    Tolerant of a mapping that has no such key at all, so this function can be
    exercised over rows built by hand as well as over a `SELECT *`.
    """
    try:
        value = row["variant_index"]
    except (KeyError, IndexError):
        return None
    return None if value is None else int(value)
