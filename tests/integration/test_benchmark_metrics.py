"""008-T7 — coverage, the matrix, and FR-092's metrics from stored trials.

The core's arithmetic is unit-tested in `tests/unit/test_benchmark_matrix.py`.
What this file checks is the *assembly*: that a suite's rows rebuild into the
trials the arithmetic runs over, that the counting identities hold on real
stored data, and that breakdowns stay labelled rather than pooled.

Three identities are the ones a reader of a published benchmark relies on, and
each gets a test on data assembled from the database rather than hand-built:

    four cells == eligible_trials
    eligible + excluded == total
    error_trials ⊆ excluded_trials

§26.5 adds the one about denominators: "exclude and count errors or incomplete
evidence without folding them into the two-by-two denominator".
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from actionwitness_core.benchmarks.enums import (
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    OutcomeTrialResult,
    SourceKind,
    TrialEligibility,
)
from actionwitness_core.benchmarks.models import NormalizedTrial
from actionwitness_service.application.benchmark_metrics import trial_from_row
from actionwitness_service.application.benchmark_service import BenchmarkService
from actionwitness_service.persistence.database import Database

pytestmark = pytest.mark.integration

BROWSER = CorrelationMode.EXECUTED_BROWSER


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "harness.sqlite3")
    await db.initialize()
    yield db


def _trial(
    trial_id: str,
    call: CallLevelResult,
    outcome: OutcomeTrialResult,
    *,
    scenario: str = "adds a mug",
    profile: str | None = None,
    eligibility: TrialEligibility = TrialEligibility.ELIGIBLE,
    reason: ExclusionReason | None = None,
) -> NormalizedTrial:
    return NormalizedTrial(
        external_trial_id=trial_id,
        scenario_id=scenario,
        correlation_mode=BROWSER,
        call_level_result=call,
        outcome_result=outcome,
        eligibility=eligibility,
        exclusion_reason=reason,
        failure_profile=profile,
    )


async def _suite(database: Database, *trials: NormalizedTrial) -> tuple[str, str]:
    workspace_id = "ws-1"
    async with database.transaction() as work:
        await work.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) VALUES (?,?,?,?)",
            (workspace_id, "interactive", work.now(), work.now()),
        )
        await work.execute(
            "INSERT INTO artifacts (id, workspace_id, artifact_type, schema_version, "
            "content_hash, metadata_json, relative_path, created_at) "
            "VALUES ('art', ?, 'evaluator_report', '1.0', 'sha256:x', '{}', 'p', ?)",
            (workspace_id, work.now()),
        )
        service = BenchmarkService(work, workspace_id)
        benchmark_id = await service.create(
            source_kind=SourceKind.RECORDED_FIXTURE, correlation_mode=BROWSER
        )
        if trials:
            await service.record_import(benchmark_id, source_artifact_id="art", trials=trials)
    return benchmark_id, workspace_id


async def _summary(database: Database, benchmark_id: str, workspace_id: str):
    async with database.transaction() as work:
        return await BenchmarkService(work, workspace_id).summarize(benchmark_id)


# --- the counting identities -------------------------------------------------


async def test_the_four_cells_sum_to_the_eligible_denominator(database: Database) -> None:
    """FR-092, on trials assembled from the database rather than hand-built."""
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
        _trial("b", CallLevelResult.PASSED, OutcomeTrialResult.FAILED),
        _trial("c", CallLevelResult.FAILED, OutcomeTrialResult.PASSED),
        _trial("d", CallLevelResult.FAILED, OutcomeTrialResult.FAILED),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    counts = summary.counts
    assert (
        (
            counts.call_level_pass_outcome_pass
            + counts.call_level_pass_outcome_fail
            + counts.call_level_fail_outcome_pass
            + counts.call_level_fail_outcome_fail
        )
        == counts.eligible_trials
        == 4
    )


async def test_eligible_plus_excluded_equals_total(database: Database) -> None:
    """FR-092: `total_trials = eligible_trials + excluded_trials`."""
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
        _trial("b", CallLevelResult.ERROR, OutcomeTrialResult.NOT_REACHED),
        _trial(
            "c",
            CallLevelResult.PASSED,
            OutcomeTrialResult.NOT_REACHED,
            eligibility=TrialEligibility.EXCLUDED,
            reason=ExclusionReason.UNBOUND,
        ),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    counts = summary.counts
    assert counts.eligible_trials == 1
    assert counts.excluded_trials == 2
    assert counts.total_trials == 3


async def test_errors_are_counted_without_entering_the_denominator(
    database: Database,
) -> None:
    """§26.5: "exclude and count errors or incomplete evidence without folding
    them into the two-by-two denominator".

    An evaluator that crashed says nothing about the model or the target;
    counting it would make a flaky harness read as a bad result.
    """
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
        _trial("b", CallLevelResult.ERROR, OutcomeTrialResult.NOT_REACHED),
        _trial("c", CallLevelResult.PASSED, OutcomeTrialResult.ERROR),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert — two errors, both excluded, neither in the denominator.
    assert summary.counts.eligible_trials == 1
    assert summary.counts.error_trials == 2
    assert summary.counts.excluded_trials == 2
    assert summary.metrics.call_level_pass_rate.denominator == 1


# --- the five metrics --------------------------------------------------------


async def test_the_metrics_read_the_stored_population(database: Database) -> None:
    """FR-092's formulas over a suite chosen so no two rates agree."""
    # Arrange — 2 both-pass, 3 silent defects, 1 fail/pass, 4 fail/fail.
    trials = [
        *[_trial(f"pp{i}", CallLevelResult.PASSED, OutcomeTrialResult.PASSED) for i in range(2)],
        *[_trial(f"pf{i}", CallLevelResult.PASSED, OutcomeTrialResult.FAILED) for i in range(3)],
        _trial("fp0", CallLevelResult.FAILED, OutcomeTrialResult.PASSED),
        *[_trial(f"ff{i}", CallLevelResult.FAILED, OutcomeTrialResult.FAILED) for i in range(4)],
    ]
    benchmark_id, workspace_id = await _suite(database, *trials)

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    metrics = summary.metrics
    assert summary.counts.eligible_trials == 10
    assert metrics.call_level_pass_rate.display == "0.5000"
    assert metrics.outcome_pass_rate.display == "0.3000"
    assert metrics.end_to_end_success_rate.display == "0.2000"
    assert metrics.silent_outcome_failure_rate.display == "0.6000"
    assert metrics.incremental_outcome_failure_trials == 3


async def test_every_rate_prints_four_decimals(database: Database) -> None:
    """FR-092: "display four decimal places", from unrounded integer counts.

    1/3 must be 0.3333 every time — a float would make the fourth place a
    rounding accident.
    """
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
        _trial("b", CallLevelResult.FAILED, OutcomeTrialResult.FAILED),
        _trial("c", CallLevelResult.FAILED, OutcomeTrialResult.FAILED),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    assert summary.metrics.call_level_pass_rate.display == "0.3333"
    assert summary.metrics.call_level_pass_rate.numerator == 1
    assert summary.metrics.call_level_pass_rate.denominator == 3


async def test_a_suite_with_no_eligible_trial_reports_null_rates(
    database: Database,
) -> None:
    """FR-092: "all rate fields are `null` when `eligible_trials` is zero".

    `0.0000` would say "we measured and found none", which is a claim nobody
    made — and it is the reading a dashboard would put in front of somebody.
    """
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.ERROR, OutcomeTrialResult.NOT_REACHED),
        _trial("b", CallLevelResult.ERROR, OutcomeTrialResult.NOT_REACHED),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    for rate in (
        summary.metrics.call_level_pass_rate,
        summary.metrics.outcome_pass_rate,
        summary.metrics.end_to_end_success_rate,
        summary.metrics.silent_outcome_failure_rate,
    ):
        assert rate.value is None
        assert rate.display is None


async def test_the_silent_failure_rate_is_null_when_nothing_passed_at_call_level(
    database: Database,
) -> None:
    """FR-092's *second* null case, distinct from the first: trials are
    eligible, but the denominator this rate needs is empty."""
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.FAILED, OutcomeTrialResult.PASSED),
        _trial("b", CallLevelResult.FAILED, OutcomeTrialResult.FAILED),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    assert summary.metrics.call_level_pass_rate.display == "0.0000"
    assert summary.metrics.silent_outcome_failure_rate.value is None


async def test_an_empty_suite_reports_zeroes_and_nulls_rather_than_failing(
    database: Database,
) -> None:
    """The empty case is a real one — a suite is created before anything is
    imported, and the panel reads it in that state."""
    # Arrange
    benchmark_id, workspace_id = await _suite(database)

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    assert summary.counts.total_trials == 0
    assert summary.metrics.incremental_outcome_failure_trials == 0
    assert summary.metrics.outcome_pass_rate.value is None
    assert summary.by_scenario == ()


# --- breakdowns stay separate ------------------------------------------------


async def test_scenario_breakdowns_are_labelled_and_add_up(database: Database) -> None:
    """FR-093 and §9.9 forbid pooling; per-scenario counts must also reconcile
    with the total, which is the only reason to publish both."""
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED, scenario="one"),
        _trial("b", CallLevelResult.PASSED, OutcomeTrialResult.FAILED, scenario="two"),
        _trial("c", CallLevelResult.FAILED, OutcomeTrialResult.FAILED, scenario="two"),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    assert [group.label for group in summary.by_scenario] == ["one", "two"]
    assert sum(g.counts.eligible_trials for g in summary.by_scenario) == (
        summary.counts.eligible_trials
    )
    assert summary.by_scenario[1].metrics.call_level_pass_rate.display == "0.5000"


async def test_failure_profile_populations_are_separate(database: Database) -> None:
    """AC-16: "does not pool ... failure-profile populations"."""
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.FAILED, profile="discount"),
        _trial("b", CallLevelResult.PASSED, OutcomeTrialResult.PASSED, profile="retry"),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    assert [group.label for group in summary.by_failure_profile] == ["discount", "retry"]
    assert summary.by_failure_profile[0].metrics.silent_outcome_failure_rate.display == "1.0000"
    assert summary.by_failure_profile[1].metrics.silent_outcome_failure_rate.display == "0.0000"


async def test_a_trial_with_no_profile_joins_no_profile_population(
    database: Database,
) -> None:
    """FR-093: missing metadata is `null`, never inferred. An "unknown" bucket
    would be a population nobody chose to measure — and its rate would be
    published beside ones that were."""
    # Arrange
    benchmark_id, workspace_id = await _suite(
        database,
        _trial("a", CallLevelResult.PASSED, OutcomeTrialResult.PASSED, profile="discount"),
        _trial("b", CallLevelResult.PASSED, OutcomeTrialResult.PASSED, profile=None),
    )

    # Act
    summary = await _summary(database, benchmark_id, workspace_id)

    # Assert
    assert [group.label for group in summary.by_failure_profile] == ["discount"]
    assert summary.counts.total_trials == 2


# --- the row round trip ------------------------------------------------------


async def test_a_stored_trial_rebuilds_with_everything_it_was_given(
    database: Database,
) -> None:
    """The metrics are only as honest as the rebuild they run over."""
    # Arrange
    original = NormalizedTrial(
        external_trial_id="#0",
        scenario_id="adds a mug",
        correlation_mode=BROWSER,
        call_level_result=CallLevelResult.PASSED,
        outcome_result=OutcomeTrialResult.FAILED,
        eligibility=TrialEligibility.ELIGIBLE,
        failure_profile="discount",
        trajectory=({"name": "update_cart", "arguments": {"quantity": 1}},),
        metadata={"someUpstreamField": None},
        addressable=False,
    )
    benchmark_id, workspace_id = await _suite(database, original)

    # Act
    async with database.transaction() as work:
        rows = await BenchmarkService(work, workspace_id).trials(benchmark_id)
    rebuilt = trial_from_row(rows[0])

    # Assert
    assert rebuilt.external_trial_id == "#0"
    assert rebuilt.addressable is False
    assert rebuilt.metadata == {"someUpstreamField": None}
    assert rebuilt.trajectory == ({"name": "update_cart", "arguments": {"quantity": 1}},)
    assert rebuilt.failure_profile == "discount"
    assert rebuilt.outcome_result is OutcomeTrialResult.FAILED
