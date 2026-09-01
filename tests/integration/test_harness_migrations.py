"""004-T1 — the harness schema is created by an explicit, ordered migration runner.

Spec v1.9 §17.1 (the nine Tier 1 tables), ADR-0003 ("no `CREATE TABLE IF NOT
EXISTS` in repository code, and no placeholder migration files"), constitution
§4 ("schema changes use explicit, tested migrations; startup-time table creation
and placeholder migrations are forbidden").

The tests that matter here are the negative ones. That the tables exist is
almost self-evident from the migration text; that Tier 2 tables *don't*, that a
failed migration leaves nothing behind, and that `workspace_id` really cascades
are the properties a later change could break without anyone noticing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import aiosqlite
import pytest
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.migrations import (
    MIGRATIONS,
    TIER_ONE_TABLES,
    Migration,
    apply_migrations,
    schema_version,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Tables §17.1 defines but M6/M7 own. Shipping them now would be a placeholder.
TIER_TWO_TABLES = (
    "evaluation_cases",
    "evaluation_runs",
    "benchmark_suites",
    "benchmark_trials",
    "shopify_pairings",
)


async def _table_names(connection: aiosqlite.Connection) -> set[str]:
    async with connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cursor:
        return {row[0] for row in await cursor.fetchall()}


@pytest.fixture
def database(tmp_path: Path) -> Database:
    return Database(tmp_path / "harness.sqlite3")


async def test_migrations_create_every_tier_one_table(database: Database) -> None:
    # Arrange / Act
    version = await database.initialize()

    # Assert
    assert version == MIGRATIONS[-1].version
    async with database.connect() as connection:
        assert set(TIER_ONE_TABLES) <= await _table_names(connection)


async def test_no_tier_two_table_ships_early(database: Database) -> None:
    """A schema no code fills is a wish, not a record (ADR-0003)."""
    # Arrange / Act
    await database.initialize()

    # Assert
    async with database.connect() as connection:
        present = await _table_names(connection)
    assert present.isdisjoint(TIER_TWO_TABLES)


async def test_applying_migrations_twice_is_a_no_op(database: Database) -> None:
    """Startup runs the runner every time; only the first run may do work."""
    # Arrange
    first = await database.initialize()

    # Act
    second = await database.initialize()

    # Assert
    assert first == second
    async with database.connect() as connection:
        assert await schema_version(connection) == second


async def test_event_sequence_is_unique_within_a_run(database: Database) -> None:
    """FR-034's monotonic sequence has a schema-level backstop (ADR-0003)."""
    # Arrange
    await database.initialize()
    async with database.transaction() as work:
        await _seed_run(work, workspace_id="ws_a", run_id="run_a")
        await _insert_event(work, run_id="run_a", sequence=1)

    # Act / Assert
    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as work:
            await _insert_event(work, run_id="run_a", sequence=1)


async def test_the_same_sequence_in_a_different_run_is_allowed(database: Database) -> None:
    """The constraint is `(run_id, sequence_number)`, not `sequence_number`."""
    # Arrange
    await database.initialize()

    # Act
    async with database.transaction() as work:
        await _seed_run(work, workspace_id="ws_a", run_id="run_a")
        await _seed_run(work, workspace_id="ws_a", run_id="run_b")
        await _insert_event(work, run_id="run_a", sequence=1)
        await _insert_event(work, run_id="run_b", sequence=1)

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT run_id FROM events ORDER BY run_id")
    assert [row["run_id"] for row in rows] == ["run_a", "run_b"]


async def test_deleting_a_workspace_cascades_to_its_evidence(database: Database) -> None:
    """`workspace_id` is the cascade root, so cleanup is one statement (FR-009)."""
    # Arrange
    await database.initialize()
    async with database.transaction() as work:
        await _seed_run(work, workspace_id="ws_a", run_id="run_a")
        await _seed_run(work, workspace_id="ws_b", run_id="run_b")
        await _insert_event(work, run_id="run_a", sequence=1)
        await _insert_event(work, run_id="run_b", sequence=1)

    # Act
    async with database.transaction() as work:
        await work.execute("DELETE FROM workspaces WHERE id = ?", ("ws_a",))

    # Assert — the deleted workspace's run and its events went with it, and the
    # other workspace's evidence is untouched.
    async with database.reading() as work:
        runs = await work.fetch_all("SELECT id FROM runs")
        events = await work.fetch_all("SELECT run_id FROM events")
    assert [row["id"] for row in runs] == ["run_b"]
    assert [row["run_id"] for row in events] == ["run_b"]


async def test_a_failing_migration_leaves_no_partial_schema(tmp_path: Path) -> None:
    """An interrupted migration must not leave a version whose schema is absent."""
    # Arrange — a migration whose second statement is invalid.
    broken = (
        Migration(
            version=1,
            name="half-valid",
            statements=(
                "CREATE TABLE first_half (id TEXT PRIMARY KEY)",
                "CREATE TABLE second_half (id TEXT PRIMARY KEY, FOREIGN KEY nonsense)",
            ),
        ),
    )
    database = Database(tmp_path / "harness.sqlite3")

    # Act / Assert
    async with database.connect() as connection:
        with pytest.raises(sqlite3.OperationalError):
            await apply_migrations(connection, broken)

        assert await schema_version(connection) == 0
        assert "first_half" not in await _table_names(connection)


async def test_migrations_must_be_contiguous(tmp_path: Path) -> None:
    """A gap means a migration was deleted; applying the rest would be a guess."""
    # Arrange
    gapped = (Migration(version=2, name="skips one", statements=()),)
    database = Database(tmp_path / "harness.sqlite3")

    # Act / Assert
    async with database.connect() as connection:
        with pytest.raises(RuntimeError, match="ordered and contiguous"):
            await apply_migrations(connection, gapped)


# --- helpers ----------------------------------------------------------------


async def _seed_run(work: object, *, workspace_id: str, run_id: str) -> None:
    await work.execute(  # type: ignore[attr-defined]
        """
        INSERT OR IGNORE INTO workspaces (id, kind, created_at, last_seen_at)
        VALUES (?, 'interactive', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
        """,
        (workspace_id,),
    )
    await work.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO runs (
            id, workspace_id, target_id, target_adapter_id,
            implementation_version, status, started_at
        ) VALUES (?, ?, 'buggy_store', 'buggy_store', '0.1.0', 'armed',
                  '2026-01-01T00:00:00Z')
        """,
        (run_id, workspace_id),
    )


async def _insert_event(work: object, *, run_id: str, sequence: int) -> None:
    await work.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO events (
            id, run_id, sequence_number, event_type, actor,
            redacted_payload_json, created_at
        ) VALUES (?, ?, ?, 'run_armed', 'system', '{}', '2026-01-01T00:00:00Z')
        """,
        (f"evt_{run_id}_{sequence}", run_id, sequence),
    )
