"""004-T2 — connection configuration and the unit of work (ADR-0003).

ADR-0003 fixes four pragmas, `BEGIN IMMEDIATE` for every workspace mutation, one
owner per transaction, and a lock timeout that is a stable retryable error rather
than a silent retry. Each is asserted here, on a real connection, because each is
invisible at the call site: a connection missing `foreign_keys = ON` behaves
exactly like a correct one right up until a cascade silently does nothing.

The rollback tests carry the weight. Exit-gate item 3 says a rejection commits
nothing, and the only way to know that holds for *every* rejection is to prove
the commit is unreachable from the failure path — not to check one handler.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.persistence.database import BUSY_TIMEOUT_MS, Database, utc_now

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "harness.sqlite3")
    await db.initialize()
    return db


async def test_every_connection_applies_the_four_adr_0003_pragmas(database: Database) -> None:
    # Arrange / Act
    async with database.connect() as connection:
        pragmas = {}
        for name in ("journal_mode", "foreign_keys", "busy_timeout", "synchronous"):
            async with connection.execute(f"PRAGMA {name}") as cursor:
                row = await cursor.fetchone()
            pragmas[name] = row[0]

    # Assert
    assert str(pragmas["journal_mode"]).lower() == "wal"
    assert pragmas["foreign_keys"] == 1
    assert pragmas["busy_timeout"] == BUSY_TIMEOUT_MS
    assert pragmas["synchronous"] == 2  # FULL


async def test_foreign_keys_are_enforced_not_merely_declared(database: Database) -> None:
    """Without `PRAGMA foreign_keys = ON` the cascade root is a comment."""
    # Arrange / Act / Assert
    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO runs (
                    id, workspace_id, target_id, target_adapter_id,
                    implementation_version, status, started_at
                ) VALUES ('run_orphan', 'ws_missing', 't', 'a', '0.1.0', 'armed',
                          '2026-01-01T00:00:00Z')
                """
            )


async def test_a_transaction_holds_the_write_lock_from_the_start(database: Database) -> None:
    """`BEGIN IMMEDIATE`, not deferred.

    A deferred transaction takes the write lock at its first *write*, so two
    deferred transactions can both read, both decide, and one can lose its
    update. The observable difference is exactly this: with `BEGIN IMMEDIATE`,
    a second writer is refused before it has read anything.
    """
    # Arrange — an empty transaction, so nothing but the BEGIN can hold the lock.
    async with database.transaction():
        second = _rival(database)

        # Act
        with pytest.raises(ApiError) as caught:
            async with second.transaction():
                pytest.fail("the second transaction must not have been granted")

    # Assert
    assert caught.value.code is ApiErrorCode.WORKSPACE_LOCK_TIMEOUT


async def test_a_lock_timeout_is_retryable_and_never_silently_retried(
    database: Database,
) -> None:
    """ADR-0003: a stable retryable error, decided by the caller (constitution §5)."""
    # Arrange
    async with database.transaction():
        second = _rival(database)

        # Act
        with pytest.raises(ApiError) as caught:
            async with second.transaction():
                pytest.fail("the second transaction must not have been granted")

    # Assert
    envelope = caught.value.as_envelope()
    assert envelope["error"]["code"] == "WORKSPACE_LOCK_TIMEOUT"
    assert envelope["error"]["retryable"] is True


async def test_a_failed_unit_of_work_commits_nothing(database: Database) -> None:
    """Exit-gate item 3: a rejection leaves no partial state behind."""

    # Arrange
    class Rejected(Exception):
        """A domain rejection raised after a write has already happened."""

    # Act
    with pytest.raises(Rejected):
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO workspaces (id, kind, created_at, last_seen_at)
                VALUES ('ws_a', 'interactive', '2026-01-01T00:00:00Z',
                        '2026-01-01T00:00:00Z')
                """
            )
            raise Rejected

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id FROM workspaces")
    assert rows == []


async def test_a_cancelled_unit_of_work_commits_nothing(database: Database) -> None:
    """Cancellation is a rejection too — `CancelledError` is not an `Exception`
    subclass's cousin to be forgotten (constitution §5: partially completed
    operations remain visible rather than being silently retried)."""

    # Arrange
    async def mutate_then_cancel() -> None:
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO workspaces (id, kind, created_at, last_seen_at)
                VALUES ('ws_cancelled', 'interactive', '2026-01-01T00:00:00Z',
                        '2026-01-01T00:00:00Z')
                """
            )
            raise asyncio.CancelledError

    # Act
    with pytest.raises(asyncio.CancelledError):
        await mutate_then_cancel()

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id FROM workspaces")
    assert rows == []


async def test_a_successful_unit_of_work_commits(database: Database) -> None:
    """The counterpart assertion: rollback-on-everything must not mean never commit."""
    # Arrange / Act
    async with database.transaction() as work:
        await work.execute(
            """
            INSERT INTO workspaces (id, kind, created_at, last_seen_at)
            VALUES ('ws_a', 'interactive', ?, ?)
            """,
            (work.now(), work.now()),
        )

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id FROM workspaces")
    assert [row["id"] for row in rows] == ["ws_a"]


async def test_the_unit_of_work_stamps_rows_from_the_injected_clock(tmp_path: Path) -> None:
    """Injected clocks are what make evaluation and replay deterministic (§1)."""
    # Arrange
    frozen = utc_now().replace(year=2031, month=2, day=3, hour=4, minute=5, second=6, microsecond=0)
    database = Database(tmp_path / "harness.sqlite3", clock=lambda: frozen)
    await database.initialize()

    # Act
    async with database.transaction() as work:
        stamped = work.now()

    # Assert
    assert stamped == "2031-02-03T04:05:06Z"


async def test_a_non_contention_operational_error_is_not_advertised_as_retryable(
    database: Database,
) -> None:
    """A malformed statement arrives as the same exception type as a lock wait;
    calling it retryable would invite a caller to repeat a request that can
    never succeed."""
    # Arrange / Act
    with pytest.raises(ApiError) as caught:
        async with database.transaction() as work:
            await work.execute("SELECT * FROM a_table_that_does_not_exist")

    # Assert
    assert caught.value.code is ApiErrorCode.HARNESS_ERROR
    assert caught.value.as_envelope()["error"]["retryable"] is False


async def test_a_harness_error_leaks_no_internal_detail(database: Database) -> None:
    """§15.8 / §20: no internal message or traceback reaches a client."""
    # Arrange / Act
    with pytest.raises(ApiError) as caught:
        async with database.transaction() as work:
            await work.execute("SELECT * FROM a_table_that_does_not_exist")

    # Assert
    message = caught.value.as_envelope()["error"]["message"]
    assert "a_table_that_does_not_exist" not in message
    assert "sqlite" not in message.lower()


async def test_reads_do_not_take_the_write_lock(database: Database) -> None:
    """ADR-0003 keeps read paths off the write lock so polling cannot starve a
    mutation. A reader open at the same time as a writer proves it."""
    # Arrange
    second = _rival(database)

    # Act / Assert — no exception, and the writer still commits.
    async with database.reading() as reader:
        async with second.transaction() as writer:
            await writer.execute(
                """
                INSERT INTO workspaces (id, kind, created_at, last_seen_at)
                VALUES ('ws_a', 'interactive', '2026-01-01T00:00:00Z',
                        '2026-01-01T00:00:00Z')
                """
            )
        assert await reader.fetch_one("SELECT COUNT(*) AS n FROM workspaces") is not None


def _rival(database: Database) -> Database:
    """A second, independent client on the same file.

    Its busy timeout is shortened so a contention test observes the refusal in
    milliseconds instead of waiting out ADR-0003's real five seconds. The
    refusal itself is unchanged — only how long SQLite waits before reporting
    it — and the pragma test above still asserts the production default.
    """
    return Database(database.path, busy_timeout_ms=50)
