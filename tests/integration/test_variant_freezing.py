"""010-T6 — freezing approved variants into the manifest (FR-100, §16.4).

FR-100's final clause: "Approved variants are frozen into the content-hashed
benchmark manifest before trials begin; generation is not rerun between
repetitions."

Both halves are timing requirements, and both are enforced by structure rather
than by a check on a clock:

- **before trials begin** — freezing is permitted only while the suite is
  `draft`, which is the one state in which no trial has been imported or
  replayed.
- **not rerun between repetitions** — a second freeze is refused rather than
  overwriting. A suite whose variants changed midway would have repetitions that
  measured different things, and the manifest hash would describe none of them.

The manifest hash moving when the variants land is the third property: FR-100
says *content-hashed*, so a frozen set that did not change the hash would not
actually be sealed into anything.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from actionwitness_core.benchmarks.approval import approve, freeze
from actionwitness_core.benchmarks.enums import CorrelationMode, SourceKind, VariantKind
from actionwitness_core.benchmarks.intents import validate_candidates
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.benchmark_service import BenchmarkService
from actionwitness_service.persistence.database import Database

pytestmark = pytest.mark.integration

WORKSPACE = "ws-1"
WHEN = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
CANONICAL = "Add one ceramic mug to the cart and apply the SAVE20 discount."
THREE = (
    "Please add a ceramic mug and use the SAVE20 code.",
    "I would like one mug, discounted with SAVE20.",
    "Put a mug in my basket and take twenty percent off.",
)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "harness.sqlite3")
    await db.initialize()
    yield db


def _approved(*texts: str, indices: list[int] | None = None):
    candidates = validate_candidates(
        CANONICAL, [{"kind": VariantKind.PARAPHRASED.value, "text": text} for text in texts]
    )
    return approve(
        candidates,
        approved_indices=range(len(texts)) if indices is None else indices,
        reviewer="operator",
        approved_at=WHEN,
    )


async def _suite(database: Database) -> str:
    async with database.transaction() as work:
        await work.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) VALUES (?,?,?,?)",
            (WORKSPACE, "interactive", work.now(), work.now()),
        )
        return await BenchmarkService(work, WORKSPACE).create(
            source_kind=SourceKind.LIVE_MODEL_RUN,
            correlation_mode=CorrelationMode.IMPORTED_TRAJECTORY_REPLAY,
        )


async def _manifest(database: Database, benchmark_id: str) -> dict:
    async with database.transaction() as work:
        suite = await BenchmarkService(work, WORKSPACE).get(benchmark_id)
    return json.loads(str(suite["manifest_json"]))


# --- the seal ----------------------------------------------------------------


async def test_approved_variants_land_in_the_manifest(database: Database) -> None:
    """FR-100: "approved variants are frozen into the ... manifest"."""
    # Arrange
    benchmark_id = await _suite(database)

    # Act
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).freeze_variants(benchmark_id, _approved(*THREE))

    # Assert
    frozen = (await _manifest(database, benchmark_id))["frozen_variants"]
    assert frozen["canonical_intent"] == CANONICAL
    assert [variant["text"] for variant in frozen["variants"]] == list(THREE)


async def test_only_the_approved_subset_is_frozen(database: Database) -> None:
    """A reviewer who rejected one has decided; the manifest carries what they
    accepted and not what they turned down."""
    # Arrange
    benchmark_id = await _suite(database)

    # Act
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).freeze_variants(
            benchmark_id, _approved(*THREE, indices=[0, 2])
        )

    # Assert
    frozen = (await _manifest(database, benchmark_id))["frozen_variants"]
    assert [variant["text"] for variant in frozen["variants"]] == [THREE[0], THREE[2]]


async def test_the_frozen_set_carries_who_approved_it(database: Database) -> None:
    """A frozen set whose provenance had been dropped would be
    indistinguishable from one somebody typed in."""
    # Arrange
    benchmark_id = await _suite(database)

    # Act
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).freeze_variants(benchmark_id, _approved(*THREE))

    # Assert
    approval = (await _manifest(database, benchmark_id))["frozen_variants"]["approval"]
    assert approval["reviewer"] == "operator"
    assert approval["actor"] == "human"
    assert approval["approved_at"].startswith("2026-09-01")


async def test_freezing_changes_the_manifest_hash(database: Database) -> None:
    """FR-100 says *content-hashed*. A frozen set that left the hash unchanged
    would not be sealed into anything."""
    # Arrange
    benchmark_id = await _suite(database)
    async with database.transaction() as work:
        before = (await BenchmarkService(work, WORKSPACE).get(benchmark_id))[
            "manifest_content_hash"
        ]

    # Act
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).freeze_variants(benchmark_id, _approved(*THREE))

    # Assert
    async with database.transaction() as work:
        after = (await BenchmarkService(work, WORKSPACE).get(benchmark_id))["manifest_content_hash"]
    assert before != after


async def test_the_returned_hash_describes_the_frozen_set(database: Database) -> None:
    """So a caller can record what it sealed without re-reading the manifest."""
    # Arrange
    benchmark_id = await _suite(database)
    approved = _approved(*THREE)

    # Act
    async with database.transaction() as work:
        returned = await BenchmarkService(work, WORKSPACE).freeze_variants(benchmark_id, approved)

    # Assert
    assert returned == freeze(approved).content_hash()


# --- not rerun between repetitions -------------------------------------------


async def test_a_second_freeze_is_refused(database: Database) -> None:
    """FR-100: "generation is not rerun between repetitions".

    Refused rather than overwritten, because overwriting *is* the rerun the
    requirement forbids — and repetitions either side of it would have measured
    different things under one manifest hash.
    """
    # Arrange
    benchmark_id = await _suite(database)
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).freeze_variants(benchmark_id, _approved(*THREE))

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, WORKSPACE).freeze_variants(
                benchmark_id, _approved("A different phrasing of the mug request entirely.")
            )
    assert refused.value.code is ApiErrorCode.PRECONDITION_FAILED
    assert "new suite" in str(refused.value)


async def test_the_first_frozen_set_survives_a_refused_second(database: Database) -> None:
    """A refusal that had already half-written would be worse than allowing the
    overwrite: the manifest would describe neither set."""
    # Arrange
    benchmark_id = await _suite(database)
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).freeze_variants(benchmark_id, _approved(*THREE))

    # Act
    async with database.transaction() as work:
        with pytest.raises(ApiError):
            await BenchmarkService(work, WORKSPACE).freeze_variants(
                benchmark_id, _approved("A different phrasing of the mug request entirely.")
            )

    # Assert
    frozen = (await _manifest(database, benchmark_id))["frozen_variants"]
    assert [variant["text"] for variant in frozen["variants"]] == list(THREE)


# --- before trials begin -----------------------------------------------------


async def test_freezing_is_refused_once_the_suite_leaves_draft(
    database: Database,
) -> None:
    """ "Before trials begin" — and `draft` is the one state in which no trial
    has been imported or replayed, so the state machine enforces the timing."""
    # Arrange
    benchmark_id = await _suite(database)
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).seal(benchmark_id)

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, WORKSPACE).freeze_variants(benchmark_id, _approved(*THREE))
    assert refused.value.code is ApiErrorCode.BENCHMARK_BINDINGS_SEALED


async def test_a_suite_with_no_variants_keeps_a_null_rather_than_an_empty_set(
    database: Database,
) -> None:
    """A Tier 2 import from a checked-in fixture generates no variants at all.

    `null` says "this suite never had any"; an empty approved set would say "a
    human reviewed some and kept none", which is a different fact.
    """
    # Arrange
    benchmark_id = await _suite(database)

    # Act
    manifest = await _manifest(database, benchmark_id)

    # Assert
    assert manifest["frozen_variants"] is None


async def test_a_reviewer_may_freeze_an_empty_approved_set(database: Database) -> None:
    """And that is distinguishable from never having generated any: the
    approval is present, and it names nobody's variants."""
    # Arrange
    benchmark_id = await _suite(database)

    # Act
    async with database.transaction() as work:
        await BenchmarkService(work, WORKSPACE).freeze_variants(
            benchmark_id, _approved(*THREE, indices=[])
        )

    # Assert
    frozen = (await _manifest(database, benchmark_id))["frozen_variants"]
    assert frozen is not None
    assert frozen["variants"] == []
    assert frozen["approval"]["reviewer"] == "operator"


# --- isolation ---------------------------------------------------------------


async def test_another_workspace_cannot_freeze_variants(database: Database) -> None:
    """004's rule: a known identifier grants nothing."""
    # Arrange
    benchmark_id = await _suite(database)
    async with database.transaction() as work:
        await work.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) VALUES (?,?,?,?)",
            ("ws-2", "interactive", work.now(), work.now()),
        )

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-2").freeze_variants(benchmark_id, _approved(*THREE))
    assert refused.value.code is ApiErrorCode.RESOURCE_NOT_FOUND
