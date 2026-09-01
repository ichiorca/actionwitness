"""004-T3 — repositories: insert-only, append-only, workspace-scoped.

Spec v1.9 §17.1, §16.1, FR-006, FR-034, FR-043; ADR-0003 (sequence allocation
inside the appending transaction, unique constraint as backstop).

Three properties are worth more than the round-trips:

* a repository has **no** update or delete method, and a test asserts the
  absence rather than trusting nobody adds one;
* sequence allocation is the transaction's job, so the unique constraint never
  has to fire in normal operation — and the test proves the *allocation* is
  correct, not merely that the constraint exists;
* a second workspace holding a first workspace's identifier gets nothing back
  (FR-006). That is the shape of every isolation test in this milestone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from actionwitness_core import ports
from actionwitness_core.contracts.models import ContractRecord
from actionwitness_core.journeys.enums import SnapshotPhase
from actionwitness_core.ports.models import Observation
from actionwitness_core.security.canonical import content_hash
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.repositories import (
    ContractRepository,
    EventRepository,
    FindingRepository,
    SnapshotIntegrityError,
    SnapshotRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

CAPTURED_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "harness.sqlite3")
    await db.initialize()
    async with db.transaction() as work:
        for workspace_id, run_id in (("ws_a", "run_a"), ("ws_b", "run_b")):
            await _seed(work, workspace_id=workspace_id, run_id=run_id)
    return db


# --- protocol conformance ---------------------------------------------------


async def test_the_repositories_satisfy_the_core_protocols(database: Database) -> None:
    """The core owns these signatures; the service implements them (§29.2)."""
    # Arrange
    async with database.reading() as work:
        # Act / Assert
        assert isinstance(ContractRepository(work), ports.ContractRepository)
        assert isinstance(SnapshotRepository(work), ports.SnapshotRepository)
        assert isinstance(EventRepository(work), ports.EventRepository)
        assert isinstance(FindingRepository(work), ports.FindingRepository)


@pytest.mark.parametrize(
    "repository",
    [ContractRepository, SnapshotRepository, EventRepository, FindingRepository],
)
async def test_no_repository_exposes_an_update_or_delete_path(repository: type) -> None:
    """§17.1: "the repository exposes no update method for this table."

    Asserted as an absence because that is what it is. A future edit that adds
    `update_snapshot` would satisfy every other test in this file.
    """
    # Arrange
    forbidden = ("update", "delete", "remove", "set_", "edit", "overwrite", "purge")

    # Act
    names = [name for name in vars(repository) if not name.startswith("_")]

    # Assert
    assert [name for name in names if name.startswith(forbidden)] == []


# --- contracts --------------------------------------------------------------


async def test_a_contract_round_trips_through_its_own_workspace(database: Database) -> None:
    # Arrange
    record = _contract("con_1")

    # Act
    async with database.transaction() as work:
        await ContractRepository(work, "ws_a").add(record)
    async with database.reading() as work:
        found = await ContractRepository(work).get("ws_a", "con_1")

    # Assert
    assert found is not None
    assert found.content_hash == record.content_hash
    assert found.verify()


async def test_another_workspace_cannot_read_a_contract_it_knows_the_id_of(
    database: Database,
) -> None:
    """FR-006: a known identifier grants nothing. This is AC-11 in miniature."""
    # Arrange — ws_a stores a contract and ws_b learns its id.
    async with database.transaction() as work:
        await ContractRepository(work, "ws_a").add(_contract("con_secret"))

    # Act
    async with database.reading() as work:
        as_owner = await ContractRepository(work).get("ws_a", "con_secret")
        as_stranger = await ContractRepository(work).get("ws_b", "con_secret")

    # Assert
    assert as_owner is not None
    assert as_stranger is None


async def test_a_global_template_is_visible_to_every_workspace(database: Database) -> None:
    """FR-009's cleanup preserves built-in templates because they belong to nobody."""
    # Arrange
    async with database.transaction() as work:
        await ContractRepository(work, None).add(_contract("con_template"))

    # Act
    async with database.reading() as work:
        from_a = await ContractRepository(work).get("ws_a", "con_template")
        from_b = await ContractRepository(work).get("ws_b", "con_template")
        templates = await ContractRepository(work).list_templates()

    # Assert
    assert from_a is not None
    assert from_b is not None
    assert [record.contract_id for record in templates] == ["con_template"]


async def test_a_workspaces_own_contract_is_not_listed_as_a_template(
    database: Database,
) -> None:
    """Otherwise deleting a workspace would appear to delete a built-in."""
    # Arrange
    async with database.transaction() as work:
        await ContractRepository(work, "ws_a").add(_contract("con_owned"))

    # Act
    async with database.reading() as work:
        templates = await ContractRepository(work).list_templates()

    # Assert
    assert templates == []


# --- events -----------------------------------------------------------------


async def test_sequence_allocation_is_monotonic_within_a_run(database: Database) -> None:
    """FR-034. The allocation is the mechanism; the constraint is the backstop."""
    # Arrange / Act
    allocated = []
    for _ in range(3):
        async with database.transaction() as work:
            allocated.append(await EventRepository(work).append("run_a", _event()))

    # Assert
    assert allocated == [1, 2, 3]


async def test_sequences_are_scoped_to_their_run(database: Database) -> None:
    """Two runs each start at 1; the sequence is per-run, not global."""
    # Arrange / Act
    async with database.transaction() as work:
        events = EventRepository(work)
        first_a = await events.append("run_a", _event())
        first_b = await events.append("run_b", _event())
        second_a = await events.append("run_a", _event())

    # Assert
    assert (first_a, first_b, second_a) == (1, 1, 2)


async def test_events_are_read_back_in_sequence_order_after_a_cursor(
    database: Database,
) -> None:
    """§15.3's paged poll."""
    # Arrange
    async with database.transaction() as work:
        for index in range(5):
            await EventRepository(work).append("run_a", _event(tool_name=f"tool_{index}"))

    # Act
    async with database.reading() as work:
        page = await EventRepository(work).list_after("run_a", after_sequence=2, limit=2)

    # Assert
    assert [event["sequence_number"] for event in page] == [3, 4]
    assert [event["tool_name"] for event in page] == ["tool_2", "tool_3"]


async def test_a_page_must_ask_for_at_least_one_event(database: Database) -> None:
    # Arrange / Act / Assert
    async with database.reading() as work:
        with pytest.raises(ValueError, match="at least one"):
            await EventRepository(work).list_after("run_a", after_sequence=0, limit=0)


async def test_a_rejected_append_leaves_no_event_and_no_consumed_sequence(
    database: Database,
) -> None:
    """A rejection commits nothing — including the sequence number it reserved."""
    # Arrange
    async with database.transaction() as work:
        await EventRepository(work).append("run_a", _event())

    # Act — an append that succeeds, then a rejection in the same unit of work.
    with pytest.raises(RuntimeError):
        async with database.transaction() as work:
            await EventRepository(work).append("run_a", _event())
            raise RuntimeError("a domain rejection after the write")

    # Assert — the rolled-back sequence is handed out again rather than skipped,
    # so the timeline has no gap a reader would have to explain.
    async with database.transaction() as work:
        assert await EventRepository(work).append("run_a", _event()) == 2
    async with database.reading() as work:
        assert await EventRepository(work).count("run_a") == 2


async def test_appending_to_another_workspaces_run_is_the_callers_problem_not_a_silent_write(
    database: Database,
) -> None:
    """The event table hangs off `runs`, so a run id that does not exist is a
    foreign-key failure rather than an orphan row."""
    # Arrange / Act / Assert
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as work:
            await EventRepository(work).append("run_that_does_not_exist", _event())


# --- snapshots --------------------------------------------------------------


async def test_a_snapshot_round_trips_as_an_observation(database: Database) -> None:
    """FR-043: insert-only, and what comes back is an `Observation`, not a dict —
    so it cannot be mistaken for a tool's self-report (constitution §4)."""
    # Arrange
    observation = _observation({"cart": {"total": "10.00"}})

    # Act
    async with database.transaction() as work:
        await SnapshotRepository(work).add("run_a", SnapshotPhase.BEFORE, observation)
    async with database.reading() as work:
        restored = await SnapshotRepository(work).get("run_a", SnapshotPhase.BEFORE)

    # Assert
    assert restored is not None
    assert restored.namespace == observation.namespace
    assert restored.content_hash() == observation.content_hash()
    assert restored.source_classification.value == "authoritative_observation"


async def test_the_two_phases_of_one_run_are_stored_separately(database: Database) -> None:
    # Arrange / Act
    async with database.transaction() as work:
        snapshots = SnapshotRepository(work)
        await snapshots.add("run_a", SnapshotPhase.BEFORE, _observation({"n": 1}))
        await snapshots.add("run_a", SnapshotPhase.AFTER, _observation({"n": 2}))

    # Assert
    async with database.reading() as work:
        snapshots = SnapshotRepository(work)
        before = await snapshots.get("run_a", SnapshotPhase.BEFORE)
        after = await snapshots.get("run_a", SnapshotPhase.AFTER)
    assert before is not None and after is not None
    assert before.payload["n"] == 1
    assert after.payload["n"] == 2


async def test_a_second_snapshot_for_the_same_phase_is_refused(database: Database) -> None:
    """Insert-only means insert *once*: overwriting the `before` observation
    would rewrite the evidence a verdict already rests on."""
    # Arrange
    import sqlite3

    async with database.transaction() as work:
        await SnapshotRepository(work).add("run_a", SnapshotPhase.BEFORE, _observation({"n": 1}))

    # Act / Assert
    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as work:
            await SnapshotRepository(work).add(
                "run_a", SnapshotPhase.BEFORE, _observation({"n": 99})
            )


async def test_a_missing_snapshot_reads_as_none(database: Database) -> None:
    # Arrange / Act
    async with database.reading() as work:
        found = await SnapshotRepository(work).get("run_a", SnapshotPhase.AFTER)

    # Assert
    assert found is None


async def test_a_tampered_snapshot_refuses_to_read_back(database: Database) -> None:
    """Constitution §5: an evidence-verification failure is explicit and never
    degrades to success — nor to a quiet `None`, which would read as "absent"."""
    # Arrange
    async with database.transaction() as work:
        await SnapshotRepository(work).add("run_a", SnapshotPhase.BEFORE, _observation({"n": 1}))

    # Act — edit the stored payload behind the repository's back.
    async with database.transaction() as work:
        await work.execute(
            "UPDATE snapshots SET redacted_state_json = ? WHERE run_id = ?",
            ('{"n": 999}', "run_a"),
        )

    # Assert
    async with database.reading() as work:
        with pytest.raises(SnapshotIntegrityError):
            await SnapshotRepository(work).get("run_a", SnapshotPhase.BEFORE)


# --- findings ---------------------------------------------------------------


async def test_findings_round_trip_and_keep_single_and_multi_path_apart(
    database: Database,
) -> None:
    """§17.1 forbids setting both `path` and `paths_json`."""
    # Arrange
    findings = [
        {
            "check_id": "chk_1",
            "check_type": "assertion",
            "severity": "critical",
            "status": "fail",
            "path": "target.cart.total",
            "expected": "10.00",
            "actual": "12.00",
        },
        {
            "check_id": "chk_2",
            "check_type": "policy",
            "severity": "warning",
            "status": "fail",
            "paths": ["target.cart.total", "target.cart.subtotal"],
            "expected": None,
            "actual": None,
        },
    ]

    # Act
    async with database.transaction() as work:
        await FindingRepository(work).add_all("run_a", findings)
    async with database.reading() as work:
        stored = list(await FindingRepository(work).list_for_run("run_a"))

    # Assert
    single, multiple = stored
    assert single["path"] == "target.cart.total"
    assert single["paths_json"] is None
    assert multiple["path"] is None
    assert multiple["paths_json"] == '["target.cart.total", "target.cart.subtotal"]'


async def test_findings_belong_to_their_run_only(database: Database) -> None:
    # Arrange
    async with database.transaction() as work:
        await FindingRepository(work).add_all(
            "run_a",
            [
                {
                    "check_id": "chk_1",
                    "check_type": "assertion",
                    "severity": "critical",
                    "status": "fail",
                }
            ],
        )

    # Act
    async with database.reading() as work:
        other = await FindingRepository(work).list_for_run("run_b")

    # Assert
    assert list(other) == []


# --- helpers ----------------------------------------------------------------


async def _seed(work: UnitOfWork, *, workspace_id: str, run_id: str) -> None:
    await work.execute(
        """
        INSERT INTO workspaces (id, kind, created_at, last_seen_at)
        VALUES (?, 'interactive', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (workspace_id,),
    )
    await work.execute(
        """
        INSERT INTO runs (
            id, workspace_id, target_id, target_adapter_id,
            implementation_version, status, started_at
        ) VALUES (?, ?, 'buggy_store', 'buggy_store', '0.1.0', 'armed',
                  '2026-01-01T00:00:00Z')
        """,
        (run_id, workspace_id),
    )


def _contract(contract_id: str) -> ContractRecord:
    document = {"name": contract_id, "schema_version": "1.0.0"}
    return ContractRecord(
        contract_id=contract_id,
        schema_version="1.0.0",
        content_hash=content_hash(document),
        document=document,
        created_at=CAPTURED_AT,
    )


def _event(**overrides: object) -> dict[str, object]:
    event: dict[str, object] = {
        "event_type": "tool_invocation_completed",
        "actor": "agent",
        "redacted_payload": {"ok": True},
    }
    event.update(overrides)
    return event


def _observation(payload: dict[str, object]) -> Observation:
    return Observation(
        namespace="target",
        provider_id="buggy_store",
        provenance="server_observed",
        schema_version="1.0.0",
        payload=payload,
        state_version="7",
        captured_at=CAPTURED_AT,
    )
