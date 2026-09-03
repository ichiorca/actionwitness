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
    TIER_THREE_SHOPIFY_TABLES,
    TIER_TWO_BENCHMARK_TABLES,
    TIER_TWO_EVAL_TABLES,
    Migration,
    apply_migrations,
    schema_version,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Every table §17.1 defines that some milestone has landed the code for. M6
#: landed the three eval tables in migration 2, M7 the two benchmark tables in
#: migration 3, migration 5 the audit table, and migration 9 the Shopify pairing
#: table alongside `ShopifyPairingService` — the code that fills it.
#:
#: This list used to be its complement: the tables that had *not* been built,
#: asserted absent. That reading retired itself as each one landed, and would
#: have gone vacuous the moment the last one did. The positive form keeps the
#: same property and survives: a table added to the schema without being
#: declared here fails, so no table arrives unannounced.
DECLARED_TABLES = (
    *TIER_ONE_TABLES,
    *TIER_TWO_EVAL_TABLES,
    *TIER_TWO_BENCHMARK_TABLES,
    *TIER_THREE_SHOPIFY_TABLES,
    "external_audits",
    # The rebuild scratch table migration 4 drops before it commits; named so
    # the accounting below is total rather than approximately total.
    "evaluation_runs_rebuilt",
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


async def test_no_table_ships_before_the_code_that_fills_it(database: Database) -> None:
    """A schema no code fills is a wish, not a record (ADR-0003).

    Every table the runner creates must be one this module names, and each name
    in `DECLARED_TABLES` is one a milestone has landed the writing code for. A
    table added ahead of its code fails here, which is the placeholder this gate
    forbids; a table renamed without updating the list fails too, which is the
    decay the old absent-tables reading could not catch.
    """
    # Arrange / Act
    await database.initialize()

    # Assert
    async with database.connect() as connection:
        present = await _table_names(connection)
    undeclared = sorted(present - set(DECLARED_TABLES) - {"sqlite_sequence"})
    assert undeclared == [], f"tables created by no declared milestone: {undeclared}"


async def test_migration_one_still_carries_only_tier_one_tables(database: Database) -> None:
    """The M3 gate's real property, kept once Tier 2 arrived.

    "Nothing from Tier 2 slipped in early" cannot be checked against the
    finished database any more, because Tier 2 has legitimately landed. What
    stays checkable — and stays the thing worth protecting — is that the *first*
    migration is still exactly the nine tables M3 shipped. A later table quietly
    edited into migration 1 would change the schema of every database that
    already ran it, which is the failure ordered migrations exist to prevent.
    """
    # Arrange
    first = MIGRATIONS[0]

    # Act
    created = {
        statement.split("CREATE TABLE")[1].split("(")[0].strip()
        for statement in first.statements
        if "CREATE TABLE" in statement
    }

    # Assert
    assert created == set(TIER_ONE_TABLES)
    assert created.isdisjoint(TIER_TWO_EVAL_TABLES)
    assert created.isdisjoint(TIER_TWO_BENCHMARK_TABLES)


async def test_the_eval_tables_arrive_in_their_own_migration(database: Database) -> None:
    """M6's tables ship in migration 2, applied by the same ordered runner."""
    # Arrange / Act
    version = await database.initialize()

    # Assert
    assert version >= 2
    async with database.connect() as connection:
        assert set(TIER_TWO_EVAL_TABLES) <= await _table_names(connection)


async def test_the_benchmark_tables_arrive_in_their_own_migration(database: Database) -> None:
    """M7's tables ship in migration 3, applied by the same ordered runner.

    Their own migration, not an edit to migration 2: a database that already ran
    2 must reach the same schema by running 3, which is only true if 2 is left
    exactly as it shipped.
    """
    # Arrange / Act
    version = await database.initialize()

    # Assert
    assert version >= 3
    async with database.connect() as connection:
        assert set(TIER_TWO_BENCHMARK_TABLES) <= await _table_names(connection)

    second = MIGRATIONS[1]
    created_by_two = {
        statement.split("CREATE TABLE")[1].split("(")[0].strip()
        for statement in second.statements
        if "CREATE TABLE" in statement
    }
    assert created_by_two == set(TIER_TWO_EVAL_TABLES)


async def test_a_benchmark_trial_cannot_bind_one_run_twice(database: Database) -> None:
    """FR-091 at the storage layer: "a source run cannot be counted twice in one
    benchmark".

    Enforced by a partial unique index rather than by a read-then-write, which
    two concurrent binders would both pass.
    """
    # Arrange
    await database.initialize()
    async with database.connect() as connection:
        await connection.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) "
            "VALUES ('ws', 'interactive', 't', 't')"
        )
        await connection.execute(
            "INSERT INTO artifacts (id, workspace_id, artifact_type, schema_version, "
            "content_hash, metadata_json, relative_path, created_at) "
            "VALUES ('art', 'ws', 'evaluator_report', '1.0', 'sha256:x', '{}', 'p', 't')"
        )
        await connection.execute(
            "INSERT INTO benchmark_suites (id, workspace_id, schema_version, source_kind, "
            "manifest_content_hash, manifest_json, correlation_mode, status, "
            "normalized_adapter_version, created_at) "
            "VALUES ('suite', 'ws', '1.0', 'recorded_fixture', 'sha256:y', '{}', "
            "'executed_browser', 'draft', '1', 't')"
        )

        def _trial(trial_id: str, external: str) -> tuple[str, ...]:
            return (
                f"INSERT INTO benchmark_trials (id, benchmark_suite_id, "
                f"external_source_artifact_id, external_trial_id, scenario_id, "
                f"correlation_mode, outcome_run_id, call_level_result, outcome_result, "
                f"eligibility, metadata_json, created_at) VALUES "
                f"('{trial_id}', 'suite', 'art', '{external}', 'scenario-a', "
                f"'executed_browser', 'run-1', 'passed', 'failed', 'eligible', '{{}}', 't')",
            )

        await connection.execute(*_trial("t1", "trial-1"))

        # Act / Assert — a second trial naming the same outcome run is refused.
        with pytest.raises(sqlite3.IntegrityError):
            await connection.execute(*_trial("t2", "trial-2"))


async def test_migration_ten_preserves_old_pairings_and_adds_safe_capture_paths(
    database: Database,
) -> None:
    """A version-9 pairing upgrades without invented provenance or secret-bearing paths."""
    async with database.connect() as connection:
        assert await apply_migrations(connection, MIGRATIONS[:9]) == 9
        await connection.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) "
            "VALUES ('ws_shopify', 'interactive', '2026-01-01T00:00:00Z', "
            "'2026-01-01T00:00:00Z')"
        )
        await connection.execute(
            "INSERT INTO contracts (id, workspace_id, content_hash, name, schema_version, "
            "document_json, created_at) VALUES ('con_shopify', 'ws_shopify', 'sha256:x', "
            "'shopify', '1.0', '{}', '2026-01-01T00:00:00Z')"
        )
        await connection.execute(
            "INSERT INTO shopify_pairings (id, workspace_id, contract_id, "
            "contract_content_hash, store_origin, pairing_token_hash, status, expires_at, "
            "created_at) VALUES ('pair_old', 'ws_shopify', 'con_shopify', 'sha256:x', "
            "'https://dev-store.myshopify.com', 'digest', 'created', "
            "'2026-01-01T00:15:00Z', '2026-01-01T00:00:00Z')"
        )
        await connection.commit()

        assert await apply_migrations(connection) == 10
        async with connection.execute("PRAGMA table_info(shopify_pairings)") as cursor:
            columns = {str(row[1]) for row in await cursor.fetchall()}
        assert {"before_capture_path", "after_capture_path"} <= columns

        async with connection.execute(
            "SELECT before_capture_path, after_capture_path FROM shopify_pairings "
            "WHERE id = 'pair_old'"
        ) as cursor:
            old_pairing = await cursor.fetchone()
        assert old_pairing is not None
        assert tuple(old_pairing) == (None, None)

        await connection.execute(
            "UPDATE shopify_pairings SET before_capture_path = '/cart.js', "
            "after_capture_path = '/en/cart.js' WHERE id = 'pair_old'"
        )
        await connection.commit()
        for column in ("before_capture_path", "after_capture_path"):
            with pytest.raises(sqlite3.IntegrityError):
                await connection.execute(
                    f"UPDATE shopify_pairings SET {column} = ? WHERE id = 'pair_old'",
                    ("/cart.js?credential=secret",),
                )
            await connection.rollback()


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
