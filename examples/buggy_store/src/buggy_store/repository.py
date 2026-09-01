"""Per-workspace persistence for the store's canonical state and retries.

Spec v1.9 §17 (WAL, foreign keys, 5,000 ms busy timeout, `BEGIN IMMEDIATE`, no
`sqlite3` on the event loop), §17.1 (the two tables and their unique
constraint), §13.2 (monotonic `state_version`), Appendix D.2 (retry semantics);
ADR-0003 fixes the connection configuration and the transaction model.

The workspace is the isolation boundary, and here that is literal: every
statement is scoped by `workspace_id`, and there is no method that reads or
writes across workspaces. Two shoppers in one deployment cannot see each other's
carts even knowing the other's ID, which is the property AC-11 checks from the
harness side and this side has to actually provide.

Idempotency is stored, not inferred. Appendix D.2 requires an identical
`(request_id, payload)` to return *the first persisted result* rather than a
recomputed one - so the response is recorded at mutation time and replayed
verbatim. Recomputing it would produce a different answer the moment anything
else in the cart moved, which is exactly the bug a retry is supposed to avoid.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Final

import aiosqlite

from buggy_store.migrations import apply_migrations
from buggy_store.models import StoreState, empty_state

__all__ = [
    "BUSY_TIMEOUT_MS",
    "IdempotencyConflict",
    "IdempotencyRecord",
    "StoreRepository",
    "request_hash",
]

#: Spec §17 and ADR-0003. Bounds lock contention instead of failing instantly.
BUSY_TIMEOUT_MS: Final = 5_000


class IdempotencyConflict(RuntimeError):
    """A request ID was reused with a different payload (Appendix D.2).

    Deliberately not a retry and not a success. §D.2: reuse with a different
    payload "returns `IDEMPOTENCY_KEY_REUSED`, `retryable: false`" - the caller
    asked for two different things under one key, and guessing which one they
    meant is how a duplicate mutation happens.
    """

    def __init__(self, tool_name: str, request_id: str) -> None:
        super().__init__(
            f"request_id {request_id!r} was already used for {tool_name!r} with a different payload"
        )
        self.tool_name = tool_name
        self.request_id = request_id


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    """The first persisted result for one `(workspace, tool, request_id)`."""

    request_hash: str
    response: Mapping[str, Any]
    state_version_after: int


def request_hash(payload: Mapping[str, Any]) -> str:
    """A stable hash of a request payload, for detecting changed intent.

    `sort_keys` rather than full RFC 8785: this value never leaves the store and
    never enters an evidence chain, so it needs to be stable, not canonical. The
    harness's own hashing is the core's job, and reaching for it here would mean
    importing the package this application must run without.
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


class StoreRepository:
    """Owns the store's database. One instance per running store process."""

    def __init__(self, database_path: Path | str, *, clock=None) -> None:
        self._path = str(database_path)
        #: Injected so recorded timestamps are reproducible in tests and replay
        #: (constitution §1). Defaults to the wall clock for a running store.
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- connection lifecycle ------------------------------------------------

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open a connection configured exactly as ADR-0003 requires.

        Every pragma is applied before any statement runs, and a connection that
        cannot apply them is closed rather than returned - a connection running
        with foreign keys off would silently drop the isolation this module
        exists to provide.
        """
        connection = await aiosqlite.connect(self._path, isolation_level=None)
        try:
            await connection.execute("PRAGMA journal_mode = WAL")
            await connection.execute("PRAGMA foreign_keys = ON")
            await connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            await connection.execute("PRAGMA synchronous = FULL")
            connection.row_factory = aiosqlite.Row
            yield connection
        finally:
            await connection.close()

    async def initialize(self) -> int:
        """Run migrations once at startup. Returns the resulting schema version."""
        async with self.connect() as connection:
            return await apply_migrations(connection)

    @asynccontextmanager
    async def transaction(self, connection: aiosqlite.Connection) -> AsyncIterator[None]:
        """One `BEGIN IMMEDIATE` unit of work.

        Public because it is the repository's contract, not an implementation
        detail: ADR-0003 gives a unit of work one owner, so the service layer
        opens it and the repository's own methods join it rather than each
        opening a transaction of its own.

        ADR-0003: the deferred default takes the write lock at the first write,
        which turns a read-then-write sequence into a lost update under
        concurrency. Taking it up front converts that race into a bounded wait.
        """
        await connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            await connection.rollback()
            raise
        await connection.commit()

    # -- canonical state -----------------------------------------------------

    async def ensure_workspace(
        self, connection: aiosqlite.Connection, workspace_id: str
    ) -> StoreState:
        """Return this workspace's state, seeding an empty one on first contact.

        Seeding reference data is a separate step from migrations (ADR-0003) and
        is idempotent: a second call returns the existing row rather than
        resetting a shopper's cart.
        """
        existing = await self.read_state(connection, workspace_id)
        if existing is not None:
            return existing
        state = empty_state()
        async with self.transaction(connection):
            await connection.execute(
                """
                INSERT INTO buggy_store_state (workspace_id, state_version, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (workspace_id) DO NOTHING
                """,
                (
                    workspace_id,
                    state.state_version,
                    json.dumps(state.canonical_document(), sort_keys=True),
                    self._now(),
                ),
            )
        return await self.read_state(connection, workspace_id) or state

    async def read_state(
        self, connection: aiosqlite.Connection, workspace_id: str
    ) -> StoreState | None:
        """This workspace's canonical state, or `None` if it has none yet."""
        async with connection.execute(
            "SELECT state_json FROM buggy_store_state WHERE workspace_id = ?",
            (workspace_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return _state_from_document(json.loads(row["state_json"]))

    async def write_state(
        self, connection: aiosqlite.Connection, workspace_id: str, state: StoreState
    ) -> None:
        """Persist a new version of this workspace's state.

        The `state_version > excluded` guard is the monotonicity backstop
        (§13.2): a write that would move a version backwards updates nothing
        rather than quietly rewriting history, and the caller's read-back sees
        the mismatch.
        """
        await connection.execute(
            """
            UPDATE buggy_store_state
               SET state_version = ?, state_json = ?, updated_at = ?
             WHERE workspace_id = ? AND state_version < ?
            """,
            (
                state.state_version,
                json.dumps(state.canonical_document(), sort_keys=True),
                self._now(),
                workspace_id,
                state.state_version,
            ),
        )

    # -- idempotency records -------------------------------------------------

    async def find_record(
        self,
        connection: aiosqlite.Connection,
        workspace_id: str,
        tool_name: str,
        request_id: str,
    ) -> IdempotencyRecord | None:
        """The first persisted result for this key, if there is one."""
        async with connection.execute(
            """
            SELECT request_hash, response_json, state_version_after
              FROM buggy_store_idempotency_records
             WHERE workspace_id = ? AND tool_name = ? AND request_id = ?
            """,
            (workspace_id, tool_name, request_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return IdempotencyRecord(
            request_hash=row["request_hash"],
            response=json.loads(row["response_json"]),
            state_version_after=int(row["state_version_after"]),
        )

    async def replay_or_claim(
        self,
        connection: aiosqlite.Connection,
        workspace_id: str,
        tool_name: str,
        request_id: str,
        payload: Mapping[str, Any],
    ) -> IdempotencyRecord | None:
        """Return the recorded result for an identical repeat, or `None` to proceed.

        Raises `IdempotencyConflict` when the key was used for a *different*
        payload. The three outcomes are distinct on purpose: replay, proceed, and
        refuse. Collapsing refuse into proceed is the duplicate-mutation bug; the
        `duplicate_on_retry` profile of §13.3 exists to demonstrate exactly that,
        and it must never be reachable by accident from the correct path.
        """
        record = await self.find_record(connection, workspace_id, tool_name, request_id)
        if record is None:
            return None
        if record.request_hash != request_hash(payload):
            raise IdempotencyConflict(tool_name, request_id)
        return record

    async def record_result(
        self,
        connection: aiosqlite.Connection,
        workspace_id: str,
        tool_name: str,
        request_id: str,
        payload: Mapping[str, Any],
        response: Mapping[str, Any],
        state_version_after: int,
    ) -> None:
        """Persist the first result for this key.

        `INSERT` without an upsert: the unique constraint on
        `(workspace_id, tool_name, request_id)` is the backstop, and a second
        insert under one key means two concurrent transactions both believed
        they were first. That must surface rather than overwrite the answer the
        first one already returned to a caller.
        """
        await connection.execute(
            """
            INSERT INTO buggy_store_idempotency_records (
                workspace_id, tool_name, request_id, request_hash,
                response_json, state_version_after, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id,
                tool_name,
                request_id,
                request_hash(payload),
                json.dumps(dict(response), sort_keys=True),
                state_version_after,
                self._now(),
            ),
        )

    # -- reset ---------------------------------------------------------------

    async def reset_workspace(self, connection: aiosqlite.Connection, workspace_id: str) -> None:
        """Return one workspace to its seeded state, dropping its retry records.

        Scoped by `workspace_id` like everything else here: a reset that took a
        wider lock or a broader `DELETE` would be a cross-workspace mutation
        wearing a maintenance label.
        """
        state = empty_state()
        async with self.transaction(connection):
            await connection.execute(
                "DELETE FROM buggy_store_idempotency_records WHERE workspace_id = ?",
                (workspace_id,),
            )
            await connection.execute(
                """
                UPDATE buggy_store_state
                   SET state_version = ?, state_json = ?, updated_at = ?
                 WHERE workspace_id = ?
                """,
                (
                    state.state_version,
                    json.dumps(state.canonical_document(), sort_keys=True),
                    self._now(),
                    workspace_id,
                ),
            )

    def _now(self) -> str:
        instant = self._clock()
        if instant.tzinfo is None:
            raise ValueError("a persisted instant must be timezone-aware")
        return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _state_from_document(document: Mapping[str, Any]) -> StoreState:
    """Rebuild a validated `StoreState` from its stored document.

    Validated on read as well as on write (constitution §4: persisted JSON is
    "schema-versioned and validated on both write and read; opaque, unvalidated
    blobs are forbidden"). A row edited by hand, or written by an older build,
    fails here rather than flowing into a cart as data.
    """
    target = document["target_state"]
    cart = target["cart"]
    return StoreState.model_validate(
        {
            "state_version": document["state_version"],
            "target_state": {
                "cart": {
                    "items": cart["items"],
                    "discount": cart["discount"],
                    "subtotal": cart["subtotal"],
                    "total": cart["total"],
                },
                "order": target["order"],
                "preferences": target["preferences"],
            },
        }
    )
