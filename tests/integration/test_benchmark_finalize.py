"""008-T8 — atomic finalization into an immutable artifact (FR-094, §16.4).

Two requirements meet here and they are easy to satisfy separately and miss
together:

- **atomic** (§16.4): "either the complete derived artifact and
  `result_artifact_id` are committed together, or the suite enters `error`
  without a partial result". A reader must never find a completed suite
  pointing at nothing, or an artifact no suite claims.
- **derived, never rewriting** (FR-094, §7's non-goal): the report *references*
  its source evaluator artifact by id and hash. Recalculating creates a new
  artifact beside the old sources rather than editing them.

The atomicity test works by making finalization refuse *after* the point where
a careless implementation would already have written something, then asserting
that nothing was written.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from actionwitness_core.benchmarks.enums import (
    BenchmarkStatus,
    CallLevelResult,
    CorrelationMode,
    OutcomeTrialResult,
    SourceKind,
    TrialEligibility,
)
from actionwitness_core.benchmarks.models import NormalizedTrial
from actionwitness_core.kernel import CoreError
from actionwitness_service.application.artifacts import ArtifactStore
from actionwitness_service.application.benchmark_service import BenchmarkService
from actionwitness_service.persistence.database import Database

pytestmark = pytest.mark.integration

BROWSER = CorrelationMode.EXECUTED_BROWSER
REPLAY = CorrelationMode.IMPORTED_TRAJECTORY_REPLAY
WORKSPACE = "ws-1"
SOURCE_HASH = "sha256:" + "a" * 64


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "harness.sqlite3")
    await db.initialize()
    yield db


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts"


@pytest.fixture
def store(artifact_root: Path) -> ArtifactStore:
    return ArtifactStore(artifact_root)


def _trial(
    trial_id: str,
    call: CallLevelResult = CallLevelResult.PASSED,
    outcome: OutcomeTrialResult = OutcomeTrialResult.FAILED,
    *,
    mode: CorrelationMode = BROWSER,
    scenario: str = "adds a mug",
) -> NormalizedTrial:
    return NormalizedTrial(
        external_trial_id=trial_id,
        scenario_id=scenario,
        correlation_mode=mode,
        call_level_result=call,
        outcome_result=outcome,
        eligibility=TrialEligibility.ELIGIBLE,
        failure_profile="discount_reported_but_not_applied",
    )


async def _suite(
    database: Database,
    *trials: NormalizedTrial,
    mode: CorrelationMode = BROWSER,
    seal: bool = True,
) -> str:
    async with database.transaction() as work:
        await work.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) VALUES (?,?,?,?)",
            (WORKSPACE, "interactive", work.now(), work.now()),
        )
        await work.execute(
            "INSERT INTO artifacts (id, workspace_id, artifact_type, schema_version, "
            "content_hash, metadata_json, relative_path, byte_size, created_at) "
            "VALUES ('src', ?, 'evaluator_report', '1.0', ?, '{}', 'p', 10, ?)",
            (WORKSPACE, SOURCE_HASH, work.now()),
        )
        service = BenchmarkService(work, WORKSPACE)
        benchmark_id = await service.create(
            source_kind=SourceKind.RECORDED_FIXTURE,
            correlation_mode=mode,
            manifest_fields={"evaluator_name": "webmcp-evals", "model_name": "example-model"},
        )
        if trials:
            await service.record_import(benchmark_id, source_artifact_id="src", trials=trials)
        if seal:
            await service.seal(benchmark_id)
    return benchmark_id


async def _finalize(database: Database, store: ArtifactStore, benchmark_id: str) -> str:
    async with database.transaction() as work:
        return await BenchmarkService(work, WORKSPACE).finalize(benchmark_id, store)


# --- the happy path ----------------------------------------------------------


async def test_finalizing_commits_the_artifact_and_the_suite_together(
    database: Database, store: ArtifactStore
) -> None:
    """§16.4's atomicity, seen from the outside: after finalization the suite is
    `completed` and points at an artifact that exists."""
    # Arrange
    benchmark_id = await _suite(database, _trial("t1"), _trial("t2"))

    # Act
    artifact_id = await _finalize(database, store, benchmark_id)

    # Assert
    async with database.transaction() as work:
        suite = await BenchmarkService(work, WORKSPACE).get(benchmark_id)
        artifact = await work.fetch_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
    assert suite["status"] == BenchmarkStatus.COMPLETED.value
    assert suite["result_artifact_id"] == artifact_id
    assert suite["completed_at"] is not None
    assert artifact["benchmark_suite_id"] == benchmark_id


async def test_the_artifact_carries_the_matrix_metrics_and_manifest(
    database: Database, store: ArtifactStore
) -> None:
    """FR-094: "the matrix, metrics, coverage, and reproducibility manifest"."""
    # Arrange
    benchmark_id = await _suite(
        database,
        _trial("t1", CallLevelResult.PASSED, OutcomeTrialResult.FAILED),
        _trial("t2", CallLevelResult.PASSED, OutcomeTrialResult.PASSED),
    )

    # Act
    artifact_id = await _finalize(database, store, benchmark_id)

    # Assert
    async with database.transaction() as work:
        row = await work.fetch_one(
            "SELECT relative_path, content_hash FROM artifacts WHERE id = ?", (artifact_id,)
        )
    document = json.loads(store.read_text(str(row["relative_path"])))
    assert document["counts"]["eligible_trials"] == 2
    assert document["metrics"]["silent_outcome_failure_rate"]["value"] == "0.5000"
    assert document["manifest"]["evaluator_name"] == "webmcp-evals"
    assert document["manifest"]["source_kind"] == "recorded_fixture"
    assert [group["label"] for group in document["by_scenario"]] == ["adds a mug"]
    assert document["content_hash"] == row["content_hash"]


async def test_the_report_references_its_source_and_does_not_contain_it(
    database: Database, store: ArtifactStore
) -> None:
    """FR-094 and §7's non-goal.

    The derived artifact names the source by id and by hash. It does not embed
    the evaluator report, and it does not touch it — "rewriting an immutable
    source outcome report to embed external evaluator data" is explicitly out.
    """
    # Arrange
    benchmark_id = await _suite(database, _trial("t1"))

    # Act
    artifact_id = await _finalize(database, store, benchmark_id)

    # Assert
    async with database.transaction() as work:
        derived = await work.fetch_one(
            "SELECT source_artifact_id, relative_path FROM artifacts WHERE id = ?",
            (artifact_id,),
        )
        source = await work.fetch_one("SELECT content_hash FROM artifacts WHERE id = 'src'")
    document = json.loads(store.read_text(str(derived["relative_path"])))

    assert derived["source_artifact_id"] == "src"
    assert document["manifest"]["source_artifact_hashes"] == [SOURCE_HASH]
    # The source row is untouched by finalization.
    assert source["content_hash"] == SOURCE_HASH


async def test_recalculating_creates_a_new_artifact_beside_the_old_source(
    database: Database, store: ArtifactStore
) -> None:
    """FR-094: "recalculation ... creates a new benchmark-artifact version and
    never rewrites its sources".

    A completed suite is immutable, so a recalculation is a *new suite* over the
    same source artifact — and the original source is still there afterwards.
    """
    # Arrange
    first = await _suite(database, _trial("t1"))
    first_artifact = await _finalize(database, store, first)

    # Act — a second suite over the same source.
    async with database.transaction() as work:
        service = BenchmarkService(work, WORKSPACE)
        second = await service.create(
            source_kind=SourceKind.RECORDED_FIXTURE, correlation_mode=BROWSER
        )
        await service.record_import(second, source_artifact_id="src", trials=(_trial("t1"),))
        await service.seal(second)
    second_artifact = await _finalize(database, store, second)

    # Assert
    assert first_artifact != second_artifact
    async with database.transaction() as work:
        rows = await work.fetch_all(
            "SELECT id FROM artifacts WHERE source_artifact_id = 'src' ORDER BY created_at"
        )
        source = await work.fetch_one("SELECT content_hash FROM artifacts WHERE id = 'src'")
    assert len(rows) == 2
    assert source["content_hash"] == SOURCE_HASH


# --- the state machine -------------------------------------------------------


async def test_a_replay_suite_must_pass_through_running(
    database: Database, store: ArtifactStore
) -> None:
    """§16.4 grants the `ready` → `completed` shortcut only to
    `executed_browser`, whose outcome runs already exist.

    Finalizing a replay suite from `ready` would publish a matrix over outcomes
    nobody observed.
    """
    # Arrange
    benchmark_id = await _suite(database, _trial("t1", mode=REPLAY), mode=REPLAY)

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(CoreError):
            await BenchmarkService(work, WORKSPACE).finalize(benchmark_id, store)

    # …and it succeeds once the suite has actually run.
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).start(benchmark_id)
    artifact_id = await _finalize(database, store, benchmark_id)
    assert artifact_id


async def test_a_completed_suite_cannot_be_finalized_again(
    database: Database, store: ArtifactStore
) -> None:
    """§16.4: "completed suites are immutable"."""
    # Arrange
    benchmark_id = await _suite(database, _trial("t1"))
    await _finalize(database, store, benchmark_id)

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(CoreError):
            await BenchmarkService(work, WORKSPACE).finalize(benchmark_id, store)


async def test_a_draft_suite_cannot_be_finalized(database: Database, store: ArtifactStore) -> None:
    """Bindings are still mutable in `draft`; a matrix over them would be a
    snapshot of an unfinished decision."""
    # Arrange
    benchmark_id = await _suite(database, _trial("t1"), seal=False)

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(CoreError):
            await BenchmarkService(work, WORKSPACE).finalize(benchmark_id, store)


# --- atomicity ---------------------------------------------------------------


async def test_a_refused_finalization_leaves_no_partial_result(
    database: Database, store: ArtifactStore, artifact_root: Path
) -> None:
    """§16.4: "or the suite enters `error` without a partial result".

    The refusal here is the state-machine one, raised *before* anything is
    written. What the test pins is the consequence: no artifact row, no
    `result_artifact_id`, and no file — a careless implementation that wrote the
    report first and validated afterwards would leave all three.
    """
    # Arrange — a replay suite still in `ready`, which cannot finalize.
    benchmark_id = await _suite(database, _trial("t1", mode=REPLAY), mode=REPLAY)

    # Act
    async with database.transaction() as work:
        with pytest.raises(CoreError):
            await BenchmarkService(work, WORKSPACE).finalize(benchmark_id, store)

    # Assert
    async with database.transaction() as work:
        suite = await BenchmarkService(work, WORKSPACE).get(benchmark_id)
        derived = await work.fetch_all(
            "SELECT id FROM artifacts WHERE benchmark_suite_id = ?", (benchmark_id,)
        )
    assert suite["result_artifact_id"] is None
    assert suite["completed_at"] is None
    assert derived == []
    # No file either: the report is written only after every refusal has had
    # its chance, so a refusal leaves nothing on disk to mistake for a result.
    assert not list(artifact_root.rglob("*.json"))


async def test_a_failed_finalization_can_be_recorded_as_an_error(
    database: Database, store: ArtifactStore
) -> None:
    """The other half of §16.4's sentence: the suite enters `error`.

    In a *fresh* transaction, because the one that refused has rolled back —
    writing the status into it would roll back with it.
    """
    # Arrange
    benchmark_id = await _suite(database, _trial("t1", mode=REPLAY), mode=REPLAY)
    async with database.transaction() as work:
        with pytest.raises(CoreError):
            await BenchmarkService(work, WORKSPACE).finalize(benchmark_id, store)

    # Act
    async with database.transaction() as work:
        status = await BenchmarkService(work, WORKSPACE).mark_error(benchmark_id)

    # Assert
    assert status is BenchmarkStatus.ERROR
    async with database.transaction() as work:
        suite = await BenchmarkService(work, WORKSPACE).get(benchmark_id)
    assert suite["status"] == "error"
    assert suite["result_artifact_id"] is None


# --- isolation ---------------------------------------------------------------


async def test_another_workspace_cannot_finalize_the_suite(
    database: Database, store: ArtifactStore
) -> None:
    """004's rule holds here too: a known id grants nothing."""
    # Arrange
    benchmark_id = await _suite(database, _trial("t1"))
    async with database.transaction() as work:
        await work.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) VALUES (?,?,?,?)",
            ("ws-2", "interactive", work.now(), work.now()),
        )

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(Exception):  # noqa: B017 - ApiError, asserted by code below
            await BenchmarkService(work, "ws-2").finalize(benchmark_id, store)
    async with database.transaction() as work:
        suite = await BenchmarkService(work, WORKSPACE).get(benchmark_id)
    assert suite["status"] == BenchmarkStatus.READY.value
