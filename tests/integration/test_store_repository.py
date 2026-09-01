"""Store repository gates (spec v1.9 §17, §17.1, App. D.2; ADR-0003; 003-T2).

The workspace is the isolation boundary, and the tests that matter most here are
the ones that try to cross it: two workspaces sharing one database file, each
knowing the other's ID, must not be able to read or move the other's cart. AC-11
checks that from the harness side; this is the side that has to actually provide
it.

The second theme is retry semantics, which Appendix D.2 defines as three
*distinct* outcomes — replay the first result, proceed, or refuse — and which are
correct behaviour rather than a failure profile. Collapsing "refuse" into
"proceed" is precisely the duplicate-mutation defect that §13.3's
`duplicate_on_retry` exists to demonstrate later, so it must be unreachable from
the correct path now.

Every connection is asserted to carry ADR-0003's pragmas. A connection running
with foreign keys off would still pass every functional test in this file while
having silently dropped the constraint that makes cascade deletion safe.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest
from buggy_store.catalog import CATALOG_BY_LINE_KEY
from buggy_store.migrations import MIGRATIONS, apply_migrations, schema_version
from buggy_store.models import CartLine, TargetState, build_cart, empty_state
from buggy_store.money import format_amount
from buggy_store.repository import (
    BUSY_TIMEOUT_MS,
    IdempotencyConflict,
    StoreRepository,
    request_hash,
)
from pydantic import ValidationError

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class _Clock:
    """An injected clock, so stored timestamps are reproducible (constitution §1)."""

    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return EPOCH + timedelta(seconds=self._tick)


@pytest.fixture
def repository(tmp_path: Path) -> StoreRepository:
    return StoreRepository(tmp_path / "store.sqlite3", clock=_Clock())


def _line(line_key: str, quantity: int) -> CartLine:
    product = CATALOG_BY_LINE_KEY[line_key]
    return CartLine(product_id=product.product_id, quantity=quantity, unit_price=product.price)


# --- migrations (ADR-0003) --------------------------------------------------


@pytest.mark.integration
async def test_migrations_create_the_schema_and_record_their_version(
    repository: StoreRepository,
) -> None:
    assert await repository.initialize() == MIGRATIONS[-1].version
    async with repository.connect() as connection:
        assert await schema_version(connection) == MIGRATIONS[-1].version


@pytest.mark.integration
async def test_running_migrations_twice_is_a_no_op(repository: StoreRepository) -> None:
    """Startup runs the runner every time; the second run must change nothing."""
    first = await repository.initialize()
    assert await repository.initialize() == first


@pytest.mark.integration
async def test_the_specified_tables_exist_with_their_unique_constraint(
    repository: StoreRepository,
) -> None:
    """§17.1 names both tables and the `(workspace, tool, request_id)` constraint."""
    await repository.initialize()
    async with repository.connect() as connection:
        async with connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ) as cursor:
            tables = {row["name"] for row in await cursor.fetchall()}
        assert {"buggy_store_state", "buggy_store_idempotency_records"} <= tables

        unique_indexes = (
            "SELECT name FROM pragma_index_list('buggy_store_idempotency_records') "
            'WHERE "unique" = 1'
        )
        async with connection.execute(unique_indexes) as cursor:
            assert await cursor.fetchall(), "the composite key is not enforced"


@pytest.mark.integration
async def test_a_non_contiguous_migration_is_refused(repository: StoreRepository) -> None:
    """An ordered runner that tolerated a gap would apply schemas out of order."""
    from buggy_store.migrations import Migration

    await repository.initialize()
    async with repository.connect() as connection:
        with pytest.raises(RuntimeError, match="ordered and contiguous"):
            await apply_migrations(
                connection,
                (*MIGRATIONS, Migration(version=99, name="gap", statements=())),
            )


@pytest.mark.integration
async def test_a_failing_migration_leaves_no_partial_schema(tmp_path: Path) -> None:
    """The step and its version bump commit together, or neither does."""
    from buggy_store.migrations import Migration

    repository = StoreRepository(tmp_path / "store.sqlite3", clock=_Clock())
    broken = Migration(
        version=1,
        name="broken",
        statements=("CREATE TABLE ok_table (id INTEGER)", "THIS IS NOT SQL"),
    )
    async with repository.connect() as connection:
        with pytest.raises(aiosqlite.OperationalError):
            await apply_migrations(connection, (broken,))
        assert await schema_version(connection) == 0
        async with connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'ok_table'"
        ) as cursor:
            assert await cursor.fetchone() is None


# --- connection configuration (ADR-0003) ------------------------------------


@pytest.mark.integration
async def test_every_connection_applies_the_required_pragmas(
    repository: StoreRepository,
) -> None:
    """A connection with foreign keys off passes every functional test and is wrong."""
    await repository.initialize()
    async with repository.connect() as connection:
        async with connection.execute("PRAGMA journal_mode") as cursor:
            assert (await cursor.fetchone())[0].lower() == "wal"
        async with connection.execute("PRAGMA foreign_keys") as cursor:
            assert (await cursor.fetchone())[0] == 1
        async with connection.execute("PRAGMA busy_timeout") as cursor:
            assert (await cursor.fetchone())[0] == BUSY_TIMEOUT_MS
        async with connection.execute("PRAGMA synchronous") as cursor:
            assert (await cursor.fetchone())[0] == 2  # FULL


# --- seeding and canonical state --------------------------------------------


@pytest.mark.integration
async def test_a_new_workspace_is_seeded_empty(repository: StoreRepository) -> None:
    """§10.1's prebuilt contract arms against exactly this starting state."""
    await repository.initialize()
    async with repository.connect() as connection:
        state = await repository.ensure_workspace(connection, "ws-1")
    assert state.state_version == 1
    assert state.target_state.cart.items == {}
    assert state.target_state.order.created is False


@pytest.mark.integration
async def test_seeding_an_existing_workspace_does_not_reset_it(
    repository: StoreRepository,
) -> None:
    """Seeding is idempotent; a second visit must not empty a shopper's cart."""
    await repository.initialize()
    async with repository.connect() as connection:
        state = await repository.ensure_workspace(connection, "ws-1")
        updated = state.with_target_state(TargetState(cart=build_cart({"mug": _line("mug", 2)})))
        async with repository.transaction(connection):
            await repository.write_state(connection, "ws-1", updated)

        again = await repository.ensure_workspace(connection, "ws-1")
    assert again.state_version == 2
    assert format_amount(again.target_state.cart.subtotal) == "50.00"


@pytest.mark.integration
async def test_state_round_trips_through_storage_unchanged(
    repository: StoreRepository,
) -> None:
    await repository.initialize()
    async with repository.connect() as connection:
        seeded = await repository.ensure_workspace(connection, "ws-1")
        stored = seeded.with_target_state(
            TargetState(cart=build_cart({"mug": _line("mug", 1)}, "SAVE20"))
        )
        async with repository.transaction(connection):
            await repository.write_state(connection, "ws-1", stored)
        loaded = await repository.read_state(connection, "ws-1")

    assert loaded is not None
    assert loaded.canonical_document() == stored.canonical_document()
    assert format_amount(loaded.target_state.cart.total) == "20.00"


@pytest.mark.integration
async def test_a_write_that_would_move_the_version_backwards_changes_nothing(
    repository: StoreRepository,
) -> None:
    """§13.2 makes `state_version` monotonic; the guard is the backstop."""
    await repository.initialize()
    async with repository.connect() as connection:
        state = await repository.ensure_workspace(connection, "ws-1")
        forward = state.with_target_state(TargetState(cart=build_cart({"mug": _line("mug", 1)})))
        async with repository.transaction(connection):
            await repository.write_state(connection, "ws-1", forward)

        stale = empty_state()  # version 1, behind the stored version 2
        async with repository.transaction(connection):
            await repository.write_state(connection, "ws-1", stale)
        current = await repository.read_state(connection, "ws-1")

    assert current is not None
    assert current.state_version == 2
    assert current.target_state.cart.items != {}


@pytest.mark.integration
async def test_a_corrupt_stored_document_fails_on_read(repository: StoreRepository) -> None:
    """Constitution §4: persisted JSON is validated on read, never trusted as a blob."""
    # Structurally complete but semantically impossible: a 99.00 subtotal over an
    # empty cart. A blob-trusting reader would hand that straight to a shopper.
    tampered = json.dumps(
        {
            "state_version": 1,
            "target_state": {
                "cart": {"items": {}, "discount": None, "subtotal": "99.00", "total": "99.00"},
                "order": {"created": False, "order_id": None},
                "preferences": {"delivery_note": "", "gift_wrap": False},
            },
        }
    )
    await repository.initialize()
    async with repository.connect() as connection:
        await repository.ensure_workspace(connection, "ws-1")
        async with repository.transaction(connection):
            await connection.execute(
                "UPDATE buggy_store_state SET state_json = ? WHERE workspace_id = ?",
                (tampered, "ws-1"),
            )
        with pytest.raises(ValidationError, match="does not match the line total"):
            await repository.read_state(connection, "ws-1")


# --- workspace isolation ----------------------------------------------------


@pytest.mark.integration
async def test_two_workspaces_in_one_database_cannot_see_each_other(
    repository: StoreRepository,
) -> None:
    """The isolation boundary, tested from the side that has to provide it."""
    await repository.initialize()
    async with repository.connect() as connection:
        first = await repository.ensure_workspace(connection, "ws-1")
        await repository.ensure_workspace(connection, "ws-2")

        async with repository.transaction(connection):
            await repository.write_state(
                connection,
                "ws-1",
                first.with_target_state(TargetState(cart=build_cart({"mug": _line("mug", 3)}))),
            )

        other = await repository.read_state(connection, "ws-2")

    assert other is not None
    assert other.target_state.cart.items == {}
    assert other.state_version == 1


@pytest.mark.integration
async def test_reading_an_unknown_workspace_returns_nothing_rather_than_a_default(
    repository: StoreRepository,
) -> None:
    """A default cart here would let an unseeded ID look like a real workspace."""
    await repository.initialize()
    async with repository.connect() as connection:
        assert await repository.read_state(connection, "never-seen") is None


@pytest.mark.integration
async def test_resetting_one_workspace_leaves_the_other_untouched(
    repository: StoreRepository,
) -> None:
    await repository.initialize()
    async with repository.connect() as connection:
        for workspace in ("ws-1", "ws-2"):
            state = await repository.ensure_workspace(connection, workspace)
            async with repository.transaction(connection):
                await repository.write_state(
                    connection,
                    workspace,
                    state.with_target_state(TargetState(cart=build_cart({"mug": _line("mug", 1)}))),
                )

        await repository.reset_workspace(connection, "ws-1")

        cleared = await repository.read_state(connection, "ws-1")
        kept = await repository.read_state(connection, "ws-2")

    assert cleared is not None and cleared.target_state.cart.items == {}
    assert kept is not None and kept.target_state.cart.items != {}


# --- idempotency (Appendix D.2) ---------------------------------------------


@pytest.mark.integration
async def test_an_unused_request_id_proceeds(repository: StoreRepository) -> None:
    await repository.initialize()
    async with repository.connect() as connection:
        await repository.ensure_workspace(connection, "ws-1")
        assert (
            await repository.replay_or_claim(
                connection, "ws-1", "update_cart", "req-00000001", {"quantity": 1}
            )
            is None
        )


@pytest.mark.integration
async def test_an_identical_repeat_replays_the_first_persisted_result(
    repository: StoreRepository,
) -> None:
    """App. D.2: the retry returns *the first persisted result*, not a fresh one."""
    await repository.initialize()
    payload = {"product_id": "mug-ceramic-001", "quantity": 1}
    async with repository.connect() as connection:
        await repository.ensure_workspace(connection, "ws-1")
        async with repository.transaction(connection):
            await repository.record_result(
                connection,
                "ws-1",
                "update_cart",
                "req-00000001",
                payload,
                {"status": "success", "total": "25.00"},
                state_version_after=2,
            )
        replayed = await repository.replay_or_claim(
            connection, "ws-1", "update_cart", "req-00000001", payload
        )

    assert replayed is not None
    assert replayed.response == {"status": "success", "total": "25.00"}
    assert replayed.state_version_after == 2


@pytest.mark.integration
async def test_reusing_a_request_id_with_a_different_payload_is_refused(
    repository: StoreRepository,
) -> None:
    """App. D.2: `IDEMPOTENCY_KEY_REUSED`, non-retryable — never a second mutation."""
    await repository.initialize()
    async with repository.connect() as connection:
        await repository.ensure_workspace(connection, "ws-1")
        async with repository.transaction(connection):
            await repository.record_result(
                connection,
                "ws-1",
                "update_cart",
                "req-00000001",
                {"quantity": 1},
                {"status": "success"},
                state_version_after=2,
            )
        with pytest.raises(IdempotencyConflict):
            await repository.replay_or_claim(
                connection, "ws-1", "update_cart", "req-00000001", {"quantity": 5}
            )


@pytest.mark.integration
async def test_the_three_retry_outcomes_stay_distinct(repository: StoreRepository) -> None:
    """Replay, proceed, refuse. Collapsing refuse into proceed duplicates a mutation."""
    await repository.initialize()
    async with repository.connect() as connection:
        await repository.ensure_workspace(connection, "ws-1")
        async with repository.transaction(connection):
            await repository.record_result(
                connection, "ws-1", "update_cart", "req-1", {"q": 1}, {"ok": True}, 2
            )

        assert (
            await repository.replay_or_claim(connection, "ws-1", "update_cart", "req-1", {"q": 1})
        ) is not None
        assert (
            await repository.replay_or_claim(connection, "ws-1", "update_cart", "req-2", {"q": 1})
        ) is None
        with pytest.raises(IdempotencyConflict):
            await repository.replay_or_claim(connection, "ws-1", "update_cart", "req-1", {"q": 2})


@pytest.mark.integration
async def test_a_request_id_is_scoped_to_its_workspace_and_tool(
    repository: StoreRepository,
) -> None:
    """§17.1 keys the record on all three; sharing one across workspaces would leak."""
    await repository.initialize()
    async with repository.connect() as connection:
        await repository.ensure_workspace(connection, "ws-1")
        await repository.ensure_workspace(connection, "ws-2")
        async with repository.transaction(connection):
            await repository.record_result(
                connection, "ws-1", "update_cart", "req-1", {"q": 1}, {"ok": True}, 2
            )

        assert await repository.find_record(connection, "ws-2", "update_cart", "req-1") is None
        assert await repository.find_record(connection, "ws-1", "apply_discount", "req-1") is None
        assert await repository.find_record(connection, "ws-1", "update_cart", "req-1") is not None


@pytest.mark.integration
async def test_recording_one_key_twice_surfaces_rather_than_overwriting(
    repository: StoreRepository,
) -> None:
    """Two writers both believing they were first must not silently pick a winner."""
    await repository.initialize()
    async with repository.connect() as connection:
        await repository.ensure_workspace(connection, "ws-1")
        async with repository.transaction(connection):
            await repository.record_result(
                connection, "ws-1", "update_cart", "req-1", {"q": 1}, {"first": True}, 2
            )
        with pytest.raises(aiosqlite.IntegrityError):
            async with repository.transaction(connection):
                await repository.record_result(
                    connection, "ws-1", "update_cart", "req-1", {"q": 1}, {"second": True}, 3
                )

        record = await repository.find_record(connection, "ws-1", "update_cart", "req-1")
    assert record is not None
    assert record.response == {"first": True}


@pytest.mark.integration
async def test_resetting_a_workspace_clears_its_retry_records(
    repository: StoreRepository,
) -> None:
    await repository.initialize()
    async with repository.connect() as connection:
        await repository.ensure_workspace(connection, "ws-1")
        async with repository.transaction(connection):
            await repository.record_result(
                connection, "ws-1", "update_cart", "req-1", {"q": 1}, {"ok": True}, 2
            )
        await repository.reset_workspace(connection, "ws-1")
        assert await repository.find_record(connection, "ws-1", "update_cart", "req-1") is None


@pytest.mark.integration
def test_the_request_hash_ignores_member_order_but_not_values() -> None:
    """Two spellings of one payload are one intent; two values are two intents."""
    assert request_hash({"a": 1, "b": 2}) == request_hash({"b": 2, "a": 1})
    assert request_hash({"a": 1}) != request_hash({"a": 2})


# --- transactions -----------------------------------------------------------


@pytest.mark.integration
async def test_a_failed_unit_of_work_commits_nothing(repository: StoreRepository) -> None:
    """A partial write here would leave a cart nobody asked for."""
    await repository.initialize()
    async with repository.connect() as connection:
        state = await repository.ensure_workspace(connection, "ws-1")
        with pytest.raises(RuntimeError, match="deliberate"):
            async with repository.transaction(connection):
                await repository.write_state(
                    connection,
                    "ws-1",
                    state.with_target_state(TargetState(cart=build_cart({"mug": _line("mug", 4)}))),
                )
                raise RuntimeError("deliberate")
        unchanged = await repository.read_state(connection, "ws-1")

    assert unchanged is not None
    assert unchanged.state_version == 1
    assert unchanged.target_state.cart.items == {}


@pytest.mark.integration
async def test_a_naive_timestamp_is_refused(tmp_path: Path) -> None:
    """A naive instant in stored evidence is off by the machine's UTC offset."""
    repository = StoreRepository(tmp_path / "store.sqlite3", clock=lambda: datetime(2026, 1, 1))
    async with repository.connect() as connection:
        await apply_migrations(connection)
        with pytest.raises(ValueError, match="timezone-aware"):
            await repository.ensure_workspace(connection, "ws-1")
