"""Explicit, ordered schema migrations for the store's own database.

Spec v1.9 §17.1 (`buggy_store_state`, `buggy_store_idempotency_records`, and the
unique constraint on `(workspace_id, tool_name, request_id)`); ADR-0003
("schema is created by an explicit, ordered, tested migration runner invoked once
at startup. No `CREATE TABLE IF NOT EXISTS` in repository code, and no
placeholder migration files").

The distinction ADR-0003 draws is worth restating, because it looks like
pedantry until the first time it matters: *running a migration runner at startup*
is expected, while *a repository creating its table on first use* is forbidden.
The second hides a schema change behind whichever code path happens to run first,
so two deployments of the same build can end up with different schemas and
neither one knows.

Table names carry the `buggy_store_` prefix from §17.1 because the composed image
may put harness and demo tables in one file. The store still owns its own
database in every other deployment; the prefix costs nothing and removes the
collision entirely.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, NamedTuple

import aiosqlite

__all__ = ["MIGRATIONS", "Migration", "apply_migrations", "schema_version"]


class Migration(NamedTuple):
    """One ordered, irreversible schema step."""

    version: int
    name: str
    statements: tuple[str, ...]


#: Ordered and append-only. A migration that has shipped is never edited: the
#: database that already ran it would not run it again, so an edit changes the
#: schema only for installations that had not upgraded yet.
MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=1,
        name="canonical state and idempotency records",
        statements=(
            """
            CREATE TABLE buggy_store_state (
                workspace_id  TEXT    NOT NULL PRIMARY KEY,
                state_version INTEGER NOT NULL,
                state_json    TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL
            )
            """,
            """
            CREATE TABLE buggy_store_idempotency_records (
                workspace_id        TEXT    NOT NULL,
                tool_name           TEXT    NOT NULL,
                request_id          TEXT    NOT NULL,
                request_hash        TEXT    NOT NULL,
                response_json       TEXT    NOT NULL,
                state_version_after INTEGER NOT NULL,
                created_at          TEXT    NOT NULL,
                PRIMARY KEY (workspace_id, tool_name, request_id),
                FOREIGN KEY (workspace_id)
                    REFERENCES buggy_store_state (workspace_id)
                    ON DELETE CASCADE
            )
            """,
            # §17.1's unique constraint is the primary key above. This index
            # serves the other direction: finding a workspace's records to purge
            # them on reset, which the composed harness does per run.
            """
            CREATE INDEX buggy_store_idempotency_by_workspace
                ON buggy_store_idempotency_records (workspace_id)
            """,
        ),
    ),
)


async def schema_version(connection: aiosqlite.Connection) -> int:
    """The highest migration this database has applied.

    `user_version` is a SQLite header field, so it needs no table of its own and
    cannot itself be the thing a missing migration failed to create.
    """
    async with connection.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def apply_migrations(
    connection: aiosqlite.Connection, migrations: Sequence[Migration] = MIGRATIONS
) -> int:
    """Apply every migration this database has not yet run, in order.

    Each migration and its version bump commit together, so an interrupted run
    leaves the database at a version whose schema actually exists. Returns the
    resulting schema version.
    """
    current = await schema_version(connection)
    for migration in migrations:
        if migration.version <= current:
            continue
        if migration.version != current + 1:
            raise RuntimeError(
                f"migration {migration.version} ({migration.name}) cannot follow schema "
                f"version {current}; migrations are ordered and contiguous"
            )
        await connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                await connection.execute(statement)
            # PRAGMA cannot be parameterised, and `version` is an int from a
            # module-level literal rather than anything a caller supplies.
            await connection.execute(f"PRAGMA user_version = {int(migration.version)}")
        except Exception:
            await connection.rollback()
            raise
        await connection.commit()
        current = migration.version
    return current
