"""Connection configuration and the unit of work (ADR-0003, spec v1.9 §17).

ADR-0003 fixes all of this and is Accepted, so the job here is to implement it
exactly rather than to re-decide it:

* every connection applies WAL, foreign keys, a 5,000 ms busy timeout, and
  `synchronous=FULL` **before any statement runs**;
* every workspace mutation runs inside `BEGIN IMMEDIATE`;
* a unit of work has **one owner** — the application service opens it and
  repositories join it, and no repository opens its own;
* a lock timeout is a stable retryable error, never a silent retry.

The pragmas are not decoration. Foreign keys are off by default in SQLite, and
workspace isolation depends on them: without `PRAGMA foreign_keys = ON` the
cascade root in the migrations is a comment. A connection that cannot apply them
is closed rather than returned.

`BEGIN IMMEDIATE` is the other one worth restating. `aiosqlite` inherits the
standard library's autocommit behaviour, so a deferred transaction takes the
write lock at the *first write* — turning read-then-write into a lost update
under concurrency. Taking it up front converts that race into a bounded wait
governed by the busy timeout.

**Nothing is held across a wait.** No transaction opened here may span browser
I/O, an SSE delivery, or a human confirmation. That rule is what makes M5's
two-transaction confirmation flow possible, and breaking it here would surface
two milestones later as a deadlock nobody could reproduce.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import aiosqlite

from actionwitness_service.api.errors import ApiError, ApiErrorCode

__all__ = [
    "BUSY_TIMEOUT_MS",
    "Database",
    "UnitOfWork",
    "utc_now",
]

#: Spec §17 and ADR-0003: bounds lock contention instead of failing instantly.
BUSY_TIMEOUT_MS: Final = 5_000

#: SQLite's numeric value for `PRAGMA synchronous = FULL`.
_SYNCHRONOUS_FULL: Final = 2


def utc_now() -> datetime:
    """The service's default clock. Injected everywhere it matters."""
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("a persisted instant must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class UnitOfWork:
    """One `BEGIN IMMEDIATE` transaction with one owner.

    Repositories take this rather than a connection, which is how ADR-0003's
    "one owner per transaction" is expressed in types: a repository has no way
    to open a second transaction because it never sees a connection it could
    open one on.
    """

    def __init__(self, connection: aiosqlite.Connection, clock: Callable[[], datetime]) -> None:
        self._connection = connection
        self._clock = clock

    @property
    def connection(self) -> aiosqlite.Connection:
        return self._connection

    def now(self) -> str:
        """The instant to stamp rows with, as stored ISO-8601 UTC."""
        return _iso(self._clock())

    def instant(self) -> datetime:
        """The same instant as a timezone-aware `datetime`.

        The core's models take a `datetime` and refuse a string outright — its
        `UtcInstant` is a `BeforeValidator`, not a coercion — so a caller that
        builds a core record needs this rather than `now()`. Both read the one
        injected clock, so a row and the record describing it cannot disagree
        about when they were made.
        """
        return self._clock().astimezone(UTC)

    async def execute(self, statement: str, parameters: tuple = ()) -> aiosqlite.Cursor:
        return await self._connection.execute(statement, parameters)

    async def fetch_one(self, statement: str, parameters: tuple = ()) -> aiosqlite.Row | None:
        async with self._connection.execute(statement, parameters) as cursor:
            return await cursor.fetchone()

    async def fetch_all(self, statement: str, parameters: tuple = ()) -> list[aiosqlite.Row]:
        async with self._connection.execute(statement, parameters) as cursor:
            return list(await cursor.fetchall())


class Database:
    """Owns the harness database file and hands out configured connections."""

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Callable[[], datetime] | None = None,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> None:
        """`busy_timeout_ms` exists so a contention test need not wait five real
        seconds to observe a lock timeout. It narrows the wait; it never widens
        the contract. Production construction passes nothing, and
        `test_every_connection_applies_the_four_adr_0003_pragmas` asserts the
        default is ADR-0003's 5,000 ms."""
        self._path = str(path)
        self._clock = clock or utc_now
        self._busy_timeout_ms = busy_timeout_ms

    @property
    def path(self) -> str:
        """The database file. A second client opens the same one."""
        return self._path

    @property
    def clock(self) -> Callable[[], datetime]:
        return self._clock

    @property
    def busy_timeout_ms(self) -> int:
        return self._busy_timeout_ms

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open a connection configured exactly as ADR-0003 requires."""
        connection = await aiosqlite.connect(self._path, isolation_level=None)
        try:
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
            await connection.execute("PRAGMA synchronous = FULL")
            connection.row_factory = aiosqlite.Row
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> int:
        """Run the migration runner once at startup (ADR-0003)."""
        from actionwitness_service.persistence.migrations import apply_migrations

        async with self.connect() as connection:
            return await apply_migrations(connection)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[UnitOfWork]:
        """One serialized workspace mutation.

        A `sqlite3.OperationalError` naming a lock is translated into
        `WORKSPACE_LOCK_TIMEOUT`, which ADR-0003 makes a stable **retryable**
        error: the busy timeout already elapsed, so the server does not retry on
        the caller's behalf. Constitution §5 — an ambiguous outcome is never
        automatically retried, because a retry the caller did not ask for can
        duplicate the mutation it was meant to repair.
        """
        async with self.connect() as connection:
            try:
                await connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise _lock_timeout(exc) from exc

            work = UnitOfWork(connection, self._clock)
            try:
                yield work
            except sqlite3.OperationalError as exc:
                await connection.rollback()
                raise _lock_timeout(exc) from exc
            except Exception:
                # Every failure rolls back. Exit-gate item 3 and FR-009 both
                # require a rejection to commit nothing, and the only way to
                # guarantee that for *every* rejection is to make the commit
                # unreachable from the failure path.
                await connection.rollback()
                raise
            await connection.commit()

    @asynccontextmanager
    async def reading(self) -> AsyncIterator[UnitOfWork]:
        """A read-only unit of work.

        Deferred rather than immediate, and it never escalates: ADR-0003 keeps
        read paths off the write lock so polling cannot starve a mutation.
        """
        async with self.connect() as connection:
            yield UnitOfWork(connection, self._clock)


def _lock_timeout(exc: sqlite3.OperationalError) -> ApiError:
    """Map SQLite lock contention onto the one stable retryable code."""
    text = str(exc).lower()
    if "lock" in text or "busy" in text:
        return ApiError(
            ApiErrorCode.WORKSPACE_LOCK_TIMEOUT,
            "The workspace was busy for longer than the configured wait. "
            "Retry the identical request under its original idempotency key.",
        )
    # Not contention: a real operational fault, which must not be advertised as
    # retryable just because it arrived through the same exception type.
    return ApiError(ApiErrorCode.HARNESS_ERROR, "The database rejected the operation.")
