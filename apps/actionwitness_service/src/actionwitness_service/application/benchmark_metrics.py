"""Turn a suite's stored trials into FR-092's counts, metrics, and breakdowns.

The arithmetic itself lives in `actionwitness_core.benchmarks.matrix` and is
pure. This module does the one thing the core must not: read rows out of a
workspace's database and rebuild the trials the arithmetic runs over.

**Nothing here decides an outcome.** Eligibility, the four cells, and every rate
come from the core functions. A service that recomputed any of them would be a
second opinion on numbers the artifact publishes, and the two would drift the
first time one changed.

**Populations are assembled, never pooled.** FR-093 and §9.9 forbid aggregating
across correlation modes, source kinds, scenario modes, and failure profiles. A
suite already holds exactly one mode and one source kind (enforced at import),
so the remaining risk is the breakdowns — and `break_down` labels each one
rather than returning a total anybody could add up.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from actionwitness_core.benchmarks.enums import (
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    OutcomeTrialResult,
    TrialEligibility,
)
from actionwitness_core.benchmarks.matrix import break_down, metrics_for, tally
from actionwitness_core.benchmarks.models import (
    BenchmarkMetrics,
    MatrixCounts,
    NormalizedTrial,
    Population,
)

__all__ = ["BenchmarkSummary", "summarize", "trial_from_row"]


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    """One suite's coverage, matrix, metrics, and labelled breakdowns."""

    counts: MatrixCounts
    metrics: BenchmarkMetrics
    by_scenario: tuple[Population, ...]
    by_failure_profile: tuple[Population, ...]
    trials: tuple[NormalizedTrial, ...]


def trial_from_row(row: Mapping[str, Any]) -> NormalizedTrial:
    """Rebuild a normalized trial from its stored row.

    The trajectory and the `null` metadata were stored together in
    `metadata_json`; both are read back rather than re-derived, because the
    report they came from is no longer in hand and re-deriving would mean
    guessing.
    """
    stored = _stored(row["metadata_json"])
    reason = row["exclusion_reason"]
    return NormalizedTrial(
        external_trial_id=str(row["external_trial_id"]),
        scenario_id=str(row["scenario_id"]),
        correlation_mode=CorrelationMode(str(row["correlation_mode"])),
        call_level_result=CallLevelResult(str(row["call_level_result"])),
        outcome_result=OutcomeTrialResult(str(row["outcome_result"])),
        eligibility=TrialEligibility(str(row["eligibility"])),
        exclusion_reason=None if reason is None else ExclusionReason(str(reason)),
        contract_content_hash=_optional(row["contract_content_hash"]),
        scenario_mode=_optional(row["scenario_mode"]),
        failure_profile=_optional(row["failure_profile"]),
        outcome_run_id=_optional(row["outcome_run_id"]),
        evaluation_run_id=_optional(row["evaluation_run_id"]),
        trajectory=tuple(
            step for step in stored.get("trajectory", []) if isinstance(step, Mapping)
        ),
        metadata=stored.get("metadata", {}),
        addressable=bool(stored.get("addressable", True)),
    )


def summarize(rows: Sequence[Mapping[str, Any]]) -> BenchmarkSummary:
    """FR-092 over one suite's trials.

    The breakdowns are computed from the same trial list as the totals, so a
    reader can add up a breakdown's counts and get the total's — which is the
    only reason to publish both.
    """
    trials = tuple(trial_from_row(row) for row in rows)
    counts = tally(trials)
    return BenchmarkSummary(
        counts=counts,
        metrics=metrics_for(counts),
        by_scenario=break_down(trials, lambda trial: trial.scenario_id),
        # FR-093 keeps failure-profile populations separate. A trial with no
        # profile is left out rather than gathered under an invented label —
        # "no profile" is not a profile anybody chose to measure.
        by_failure_profile=break_down(trials, lambda trial: trial.failure_profile),
        trials=trials,
    )


def _stored(metadata_json: Any) -> dict[str, Any]:
    try:
        loaded = json.loads(str(metadata_json or "{}"))
    except json.JSONDecodeError:  # pragma: no cover - written by this service
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)
