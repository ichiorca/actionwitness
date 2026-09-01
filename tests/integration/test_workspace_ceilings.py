"""004-T8 — FR-008's hard ceilings (exact numbers, no partial commits).

The three things worth proving:

* the numbers are the ones FR-008 states, not defaults that drifted;
* the event ceiling is **249 + 1**, and the reserved slot really does receive
  the `resource_limit_exceeded` boundary event — a run that spent all 250 on
  ordinary events would have nowhere to record why it stopped;
* a refusal leaves nothing behind. That is a transaction property, so it is
  tested by refusing mid-unit-of-work and then counting rows, not by trusting
  that the guard was called early enough.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application import limits as fr008
from actionwitness_service.application.limits import WorkspaceCeilings
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.repositories import EventRepository

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "harness.sqlite3")
    await db.initialize()
    async with db.transaction() as work:
        for workspace_id in ("ws_a", "ws_b"):
            await work.execute(
                """
                INSERT INTO workspaces (id, kind, created_at, last_seen_at)
                VALUES (?, 'interactive', ?, ?)
                """,
                (workspace_id, work.now(), work.now()),
            )
    return db


async def _add_run(work: UnitOfWork, workspace_id: str, run_id: str) -> None:
    await work.execute(
        """
        INSERT INTO runs (
            id, workspace_id, target_id, target_adapter_id,
            implementation_version, status, started_at
        ) VALUES (?, ?, 'buggy_store', 'buggy_store', '0.1.0', 'running', ?)
        """,
        (run_id, workspace_id, work.now()),
    )


async def _add_artifact(work: UnitOfWork, workspace_id: str, artifact_id: str, size: int) -> None:
    await work.execute(
        """
        INSERT INTO artifacts (
            id, workspace_id, artifact_type, schema_version, content_hash,
            metadata_json, relative_path, byte_size, created_at
        ) VALUES (?, ?, 'outcome_report', '1.0.0', 'sha256:x', '{}', ?, ?, ?)
        """,
        (artifact_id, workspace_id, f"{artifact_id}.json", size, work.now()),
    )


# --- the numbers themselves -------------------------------------------------


async def test_every_fr_008_ceiling_is_declared_with_its_exact_value() -> None:
    """FR-008's numbers are specified, not tunable. Transcribed once, checked here."""
    # Arrange / Act / Assert
    assert fr008.EVENTS_PER_RUN == 250
    assert fr008.ORDINARY_EVENTS_PER_RUN == 249
    assert fr008.OUTCOME_RUNS_PER_WORKSPACE == 10
    assert fr008.EVAL_CASES_PER_WORKSPACE == 10
    assert fr008.EVAL_RUNS_PER_WORKSPACE == 20
    assert fr008.SUITES_PER_WORKSPACE == 3
    assert fr008.TRIALS_PER_SUITE == 100
    assert fr008.SHOPIFY_PAIRINGS_PER_WORKSPACE == 5
    assert fr008.ARTIFACTS_PER_WORKSPACE == 25
    assert fr008.CONCURRENT_EVENT_STREAMS == 2


async def test_the_artifact_byte_ceiling_is_mebibytes() -> None:
    """ "10 MiB", not 10 MB. The two differ by 485,760 bytes."""
    # Arrange / Act / Assert
    assert fr008.ARTIFACT_BYTES_PER_WORKSPACE == 10 * 1024 * 1024
    assert fr008.ARTIFACT_BYTES_PER_WORKSPACE != 10_000_000


async def test_one_event_slot_is_reserved_rather_than_shared() -> None:
    """The boundary event needs somewhere to go, or the stop is unexplainable."""
    # Arrange / Act / Assert
    assert fr008.ORDINARY_EVENTS_PER_RUN == fr008.EVENTS_PER_RUN - 1


# --- per-workspace row ceilings ---------------------------------------------


async def test_the_tenth_run_is_allowed_and_the_eleventh_is_not(database: Database) -> None:
    # Arrange
    async with database.transaction() as work:
        for index in range(fr008.OUTCOME_RUNS_PER_WORKSPACE):
            await WorkspaceCeilings(work, "ws_a").guard_new_run()
            await _add_run(work, "ws_a", f"run_{index}")

    # Act / Assert
    with pytest.raises(ApiError) as caught:
        async with database.transaction() as work:
            await WorkspaceCeilings(work, "ws_a").guard_new_run()
    assert caught.value.code is ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED


async def test_the_refusal_names_a_way_out(database: Database) -> None:
    """FR-008: the response carries "an action to purge completed workspace data"."""
    # Arrange
    async with database.transaction() as work:
        for index in range(fr008.OUTCOME_RUNS_PER_WORKSPACE):
            await _add_run(work, "ws_a", f"run_{index}")

    # Act
    with pytest.raises(ApiError) as caught:
        async with database.transaction() as work:
            await WorkspaceCeilings(work, "ws_a").guard_new_run()

    # Assert
    assert "purge" in caught.value.message.lower()


async def test_one_workspaces_ceiling_does_not_constrain_another(database: Database) -> None:
    """The workspace is the isolation boundary; so is its budget."""
    # Arrange
    async with database.transaction() as work:
        for index in range(fr008.OUTCOME_RUNS_PER_WORKSPACE):
            await _add_run(work, "ws_a", f"run_{index}")

    # Act / Assert — ws_b is unaffected.
    async with database.transaction() as work:
        await WorkspaceCeilings(work, "ws_b").guard_new_run()
        await _add_run(work, "ws_b", "run_b")


async def test_a_refused_creation_commits_nothing(database: Database) -> None:
    """Exit-gate item 3 and FR-009: a limit response never partially commits.

    The unit of work writes *first* and is refused *after*, which is the shape
    that would leave a partial row behind if the guard were outside the
    transaction.
    """
    # Arrange
    async with database.transaction() as work:
        for index in range(fr008.OUTCOME_RUNS_PER_WORKSPACE):
            await _add_run(work, "ws_a", f"run_{index}")

    # Act
    with pytest.raises(ApiError):
        async with database.transaction() as work:
            await _add_run(work, "ws_a", "run_over_cap")
            await work.execute(
                "UPDATE workspaces SET active_run_id = ? WHERE id = ?",
                ("run_over_cap", "ws_a"),
            )
            await WorkspaceCeilings(work, "ws_a").guard_new_run()

    # Assert — neither the run nor the workspace edit survived.
    async with database.reading() as work:
        runs = await work.fetch_all("SELECT id FROM runs WHERE workspace_id = 'ws_a'")
        workspace = await work.fetch_one("SELECT active_run_id FROM workspaces WHERE id = 'ws_a'")
    assert len(runs) == fr008.OUTCOME_RUNS_PER_WORKSPACE
    assert workspace["active_run_id"] is None


# --- artifacts: two ceilings on one insert ----------------------------------


async def test_the_twenty_sixth_artifact_is_refused(database: Database) -> None:
    # Arrange
    async with database.transaction() as work:
        for index in range(fr008.ARTIFACTS_PER_WORKSPACE):
            await _add_artifact(work, "ws_a", f"art_{index}", size=1)

    # Act / Assert
    with pytest.raises(ApiError) as caught:
        async with database.transaction() as work:
            await WorkspaceCeilings(work, "ws_a").guard_new_artifact(byte_size=1)
    assert caught.value.code is ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED


async def test_the_byte_ceiling_counts_the_artifact_about_to_be_written(
    database: Database,
) -> None:
    """A cap that admitted the write which crossed it would be off by one
    artifact — and that artifact could itself be 10 MiB."""
    # Arrange — one byte short of the ceiling.
    async with database.transaction() as work:
        await _add_artifact(work, "ws_a", "art_big", size=fr008.ARTIFACT_BYTES_PER_WORKSPACE - 1)

    # Act / Assert — one more byte fits exactly; two do not.
    async with database.transaction() as work:
        await WorkspaceCeilings(work, "ws_a").guard_new_artifact(byte_size=1)

    with pytest.raises(ApiError) as caught:
        async with database.transaction() as work:
            await WorkspaceCeilings(work, "ws_a").guard_new_artifact(byte_size=2)
    assert caught.value.code is ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED


async def test_the_byte_ceiling_is_reached_before_the_count_ceiling(
    database: Database,
) -> None:
    """Both ceilings are real: 25 artifacts of 1 MiB each exceeds 10 MiB long
    before it exceeds 25 artifacts."""
    # Arrange
    async with database.transaction() as work:
        for index in range(10):
            await _add_artifact(work, "ws_a", f"art_{index}", size=1024 * 1024)

    # Act / Assert
    with pytest.raises(ApiError):
        async with database.transaction() as work:
            await WorkspaceCeilings(work, "ws_a").guard_new_artifact(byte_size=1)


async def test_a_negative_artifact_size_is_refused_outright(database: Database) -> None:
    """Otherwise a caller could buy back budget by declaring a negative size."""
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="negative size"):
        async with database.transaction() as work:
            await WorkspaceCeilings(work, "ws_a").guard_new_artifact(byte_size=-1)


# --- the event ceiling ------------------------------------------------------


async def _fill_events(database: Database, run_id: str, count: int) -> None:
    async with database.transaction() as work:
        events = EventRepository(work)
        for _ in range(count):
            await events.append(
                run_id,
                {"event_type": "tool_invocation_completed", "actor": "agent"},
            )


async def test_an_invocation_is_allowed_while_the_budget_holds(database: Database) -> None:
    # Arrange
    async with database.transaction() as work:
        await _add_run(work, "ws_a", "run_a")
    await _fill_events(database, "run_a", fr008.ORDINARY_EVENTS_PER_RUN - 1)

    # Act — the 249th ordinary event is still permitted.
    async with database.transaction() as work:
        refusal = await WorkspaceCeilings(work, "ws_a").trip_if_event_budget_exhausted("run_a")

    # Assert
    assert refusal is None


async def test_the_next_invocation_after_249_trips_the_ceiling(database: Database) -> None:
    """FR-008, verbatim: move the run to `error`, append the boundary event
    carrying `EVENT_LIMIT_EXCEEDED`, reject, and preserve existing evidence.

    The refusal is *returned* rather than raised, because raising inside the
    unit of work would roll back the very evidence FR-008 requires be written.
    The caller commits and then raises it.
    """
    # Arrange
    async with database.transaction() as work:
        await _add_run(work, "ws_a", "run_a")
    await _fill_events(database, "run_a", fr008.ORDINARY_EVENTS_PER_RUN)

    # Act
    async with database.transaction() as work:
        refusal = await WorkspaceCeilings(work, "ws_a").trip_if_event_budget_exhausted("run_a")

    # Assert — the refusal names the code FR-008 names.
    assert refusal is not None
    assert refusal.code is ApiErrorCode.EVENT_LIMIT_EXCEEDED

    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = 'run_a'")
        events = await work.fetch_all(
            "SELECT event_type, actor, status FROM events WHERE run_id = 'run_a' "
            "ORDER BY sequence_number"
        )

    # The run stopped, and the stop is on the record.
    assert run["status"] == RunState.ERROR.value
    assert events[-1]["event_type"] == OutcomeEventType.RESOURCE_LIMIT_EXCEEDED.value
    assert events[-1]["status"] == ApiErrorCode.EVENT_LIMIT_EXCEEDED.value
    # The server is speaking about the run, not the agent under test.
    assert events[-1]["actor"] == EventActor.HARNESS.value

    # Existing evidence is preserved: the boundary event was *appended* to the
    # 249 ordinary ones, filling the reserved slot exactly.
    assert len(events) == fr008.EVENTS_PER_RUN


async def test_the_boundary_write_survives_because_it_is_not_a_rejection(
    database: Database,
) -> None:
    """The contrast that makes this guard different from the others.

    A creation past a cap must commit nothing. A run past the event ceiling
    must commit *more* — the status change and the boundary event are the
    record of why it stopped. So this one returns its refusal and the
    transaction is allowed to complete.
    """
    # Arrange
    async with database.transaction() as work:
        await _add_run(work, "ws_a", "run_a")
    await _fill_events(database, "run_a", fr008.ORDINARY_EVENTS_PER_RUN)

    # Act — the caller commits, then raises what it was handed.
    async with database.transaction() as work:
        refusal = await WorkspaceCeilings(work, "ws_a").trip_if_event_budget_exhausted("run_a")
    with pytest.raises(ApiError):
        raise refusal  # type: ignore[misc]

    # Assert — the evidence outlived the refusal.
    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = 'run_a'")
        total = await work.fetch_one("SELECT COUNT(*) AS n FROM events WHERE run_id = 'run_a'")
    assert run["status"] == RunState.ERROR.value
    assert total["n"] == fr008.EVENTS_PER_RUN


async def test_the_two_boundary_writes_share_one_transaction(database: Database) -> None:
    """There must be no state where the run is stopped but nobody recorded the
    stop, so an abort must undo both together."""
    # Arrange
    async with database.transaction() as work:
        await _add_run(work, "ws_a", "run_a")
    await _fill_events(database, "run_a", fr008.ORDINARY_EVENTS_PER_RUN)

    class Aborted(Exception):
        pass

    # Act
    with pytest.raises(Aborted):
        async with database.transaction() as work:
            await WorkspaceCeilings(work, "ws_a").trip_if_event_budget_exhausted("run_a")
            raise Aborted

    # Assert
    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = 'run_a'")
        total = await work.fetch_one("SELECT COUNT(*) AS n FROM events WHERE run_id = 'run_a'")
    assert run["status"] == "running"
    assert total["n"] == fr008.ORDINARY_EVENTS_PER_RUN


async def test_one_runs_event_budget_is_its_own(database: Database) -> None:
    # Arrange
    async with database.transaction() as work:
        await _add_run(work, "ws_a", "run_a")
        await _add_run(work, "ws_a", "run_b")
    await _fill_events(database, "run_a", fr008.ORDINARY_EVENTS_PER_RUN)

    # Act / Assert — run_b is untouched by run_a's exhaustion.
    async with database.reading() as work:
        assert await WorkspaceCeilings(work, "ws_a").event_budget_remaining("run_b") == (
            fr008.ORDINARY_EVENTS_PER_RUN
        )


async def test_the_ceiling_cannot_be_tripped_from_another_workspace(
    database: Database,
) -> None:
    """FR-006 again: a stranger cannot move somebody else's run to `error`, and
    the refusal they get back must not confirm that the run exists."""
    # Arrange
    async with database.transaction() as work:
        await _add_run(work, "ws_a", "run_a")
    await _fill_events(database, "run_a", fr008.ORDINARY_EVENTS_PER_RUN)

    # Act
    async with database.transaction() as work:
        refusal = await WorkspaceCeilings(work, "ws_b").trip_if_event_budget_exhausted("run_a")

    # Assert
    assert refusal is not None
    assert refusal.code is ApiErrorCode.RESOURCE_NOT_FOUND

    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = 'run_a'")
        total = await work.fetch_one("SELECT COUNT(*) AS n FROM events WHERE run_id = 'run_a'")
    assert run["status"] == "running"
    assert total["n"] == fr008.ORDINARY_EVENTS_PER_RUN
