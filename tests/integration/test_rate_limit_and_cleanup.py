"""004-T9 — the 429 over HTTP, and FR-009's garbage collection.

Two properties carry the weight:

* a 429 **commits nothing**. That is asserted by writing a route which would
  create a row, exhausting the bucket, and then showing the table is empty —
  not by trusting that the middleware was registered early enough;
* cleanup removes a workspace's rows *and* files while **preserving global
  built-in templates**. Templates survive because they belong to nobody, so the
  test seeds one and checks it outlives the sweep that removed everything else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import create_app
from actionwitness_service.api.dependencies import DatabaseDependency, WorkspaceDependency
from actionwitness_service.application import rate_limits as fr009
from actionwitness_service.application.cleanup import (
    INTERACTIVE_WORKSPACE_TTL_HOURS,
    WorkspaceCleaner,
    purge_eval_workspace_state,
)
from actionwitness_service.persistence.database import Database
from fastapi import APIRouter, FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}

probe = APIRouter(prefix="/api/v1/_probe")


@probe.post("/create-run")
async def create_run(
    workspace_id: WorkspaceDependency, database: DatabaseDependency
) -> dict[str, str]:
    """A route that writes, so "commits nothing" has something to be false about."""
    async with database.transaction() as work:
        await work.execute(
            """
            INSERT INTO runs (
                id, workspace_id, target_id, target_adapter_id,
                implementation_version, status, started_at
            ) VALUES (?, ?, 'buggy_store', 'buggy_store', '0.1.0', 'armed', ?)
            """,
            (f"run_{workspace_id[-8:]}", workspace_id, work.now()),
        )
    return {"created": "yes"}


@probe.get("/read")
async def read(workspace_id: WorkspaceDependency) -> dict[str, str]:
    return {"workspace_id": workspace_id}


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(
        environ={**ENV, "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts")},
        database_path=tmp_path / "harness.sqlite3",
        clock=lambda: NOW,
    )
    application.include_router(probe)
    async with application.router.lifespan_context(application):
        yield application


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


# --- the 429 ----------------------------------------------------------------


async def test_the_burst_is_spent_and_then_refused(app: FastAPI) -> None:
    """The clock is frozen, so nothing refills and the boundary is exact."""
    # Arrange / Act
    async with client(app) as visitor:
        statuses = [
            (await visitor.get("/api/v1/_probe/read")).status_code
            for _ in range(fr009.REQUEST_BURST + 1)
        ]

    # Assert
    assert statuses[: fr009.REQUEST_BURST] == [200] * fr009.REQUEST_BURST
    assert statuses[-1] == 429


async def test_the_refusal_is_the_standard_envelope_with_retry_after(app: FastAPI) -> None:
    # Arrange
    async with client(app) as visitor:
        for _ in range(fr009.REQUEST_BURST):
            await visitor.get("/api/v1/_probe/read")

        # Act
        response = await visitor.get("/api/v1/_probe/read")

    # Assert
    assert response.status_code == 429
    body = response.json()["error"]
    assert body["code"] == "RATE_LIMIT_EXCEEDED"
    # The one project-allocated code besides the lock timeout that is retryable:
    # waiting genuinely resolves it.
    assert body["retryable"] is True
    assert int(response.headers["retry-after"]) >= 1


async def test_a_rate_limited_request_commits_nothing(app: FastAPI) -> None:
    """FR-009: limits "shall never partially commit a mutation"."""
    # Arrange — spend the burst on reads, so no run has been created yet.
    database: Database = app.state.database
    async with client(app) as visitor:
        for _ in range(fr009.REQUEST_BURST):
            await visitor.get("/api/v1/_probe/read")

        # Act — the write that gets refused.
        refused = await visitor.post("/api/v1/_probe/create-run")

    # Assert
    assert refused.status_code == 429
    async with database.reading() as work:
        assert await work.fetch_all("SELECT id FROM runs") == []


async def test_health_checks_are_excluded_from_the_limit(app: FastAPI) -> None:
    """FR-009 excludes them. A probe running every second would otherwise
    consume half a client's allowance and take the deployment down."""
    # Arrange / Act
    async with client(app) as probe_client:
        statuses = [
            (await probe_client.get("/healthz")).status_code for _ in range(fr009.REQUEST_BURST * 3)
        ]

    # Assert
    assert set(statuses) == {200}


async def test_health_checks_do_not_spend_another_route_s_allowance(app: FastAPI) -> None:
    """Exclusion means excluded, not cheap."""
    # Arrange
    async with client(app) as visitor:
        for _ in range(50):
            await visitor.get("/healthz")

        # Act
        response = await visitor.get("/api/v1/_probe/read")

    # Assert
    assert response.status_code == 200


async def test_a_returning_visitor_does_not_spend_the_creation_bucket(
    app: FastAPI,
) -> None:
    """Ten creations per hour is a limit on new workspaces, not on page loads —
    one user refreshing would otherwise exhaust an hour's allowance in a minute."""
    # Arrange
    async with client(app) as visitor:
        first = await visitor.get("/api/v1/_probe/read")
        assert first.status_code == 200

        # Act — 20 further requests, all carrying the cookie issued above.
        statuses = [(await visitor.get("/api/v1/_probe/read")).status_code for _ in range(20)]

    # Assert
    assert set(statuses) == {200}


# --- garbage collection -----------------------------------------------------


async def _seed_workspace(
    database: Database, workspace_id: str, *, last_seen: datetime, kind: str = "interactive"
) -> None:
    stamp = last_seen.isoformat().replace("+00:00", "Z")
    async with database.transaction() as work:
        await work.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
            (workspace_id, kind, stamp, stamp),
        )


async def _seed_run_and_artifact(
    database: Database, workspace_id: str, *, relative_path: str
) -> None:
    async with database.transaction() as work:
        await work.execute(
            """
            INSERT INTO runs (
                id, workspace_id, target_id, target_adapter_id,
                implementation_version, status, started_at
            ) VALUES (?, ?, 'buggy_store', 'buggy_store', '0.1.0', 'passed', ?)
            """,
            (f"run_{workspace_id}", workspace_id, work.now()),
        )
        await work.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, run_id, artifact_type, schema_version,
                content_hash, metadata_json, relative_path, byte_size, created_at
            ) VALUES (?, ?, ?, 'outcome_report', '1.0.0', 'sha256:x', '{}', ?, 4, ?)
            """,
            (f"art_{workspace_id}", workspace_id, f"run_{workspace_id}", relative_path, work.now()),
        )


async def _seed_template(database: Database) -> None:
    async with database.transaction() as work:
        await work.execute(
            """
            INSERT INTO contracts (
                id, workspace_id, content_hash, name, schema_version,
                document_json, created_at
            ) VALUES ('con_template', NULL, 'sha256:x', 'built-in', '1.0.0', '{}', ?)
            """,
            (work.now(),),
        )


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "harness.sqlite3", clock=lambda: NOW)
    await db.initialize()
    return db


def cleaner(database: Database, artifacts: Path) -> WorkspaceCleaner:
    return WorkspaceCleaner(database, artifact_root=artifacts, clock=lambda: NOW)


async def test_a_workspace_inactive_for_24_hours_is_removed_with_its_rows_and_files(
    database: Database, tmp_path: Path
) -> None:
    # Arrange
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "stale.json").write_text("{}", encoding="utf-8")
    await _seed_workspace(
        database,
        "ws_stale",
        last_seen=NOW - timedelta(hours=INTERACTIVE_WORKSPACE_TTL_HOURS, minutes=1),
    )
    await _seed_run_and_artifact(database, "ws_stale", relative_path="stale.json")

    # Act
    result = await cleaner(database, artifacts).sweep()

    # Assert — one statement removed nine tables' worth, because `workspace_id`
    # is the cascade root.
    assert result.workspaces_removed == 1
    assert result.files_removed == 1
    assert not (artifacts / "stale.json").exists()
    async with database.reading() as work:
        assert await work.fetch_all("SELECT id FROM workspaces") == []
        assert await work.fetch_all("SELECT id FROM runs") == []
        assert await work.fetch_all("SELECT id FROM artifacts") == []


async def test_an_active_workspace_survives(database: Database, tmp_path: Path) -> None:
    """The boundary: 23 hours 59 minutes of inactivity is still active."""
    # Arrange
    await _seed_workspace(database, "ws_recent", last_seen=NOW - timedelta(hours=23, minutes=59))

    # Act
    result = await cleaner(database, tmp_path / "artifacts").sweep()

    # Assert
    assert result.workspaces_removed == 0
    async with database.reading() as work:
        assert len(await work.fetch_all("SELECT id FROM workspaces")) == 1


async def test_global_built_in_templates_are_preserved(database: Database, tmp_path: Path) -> None:
    """FR-009 says so explicitly. They survive because they belong to nobody,
    so no cascade reaches them — not because of a special case in the sweep."""
    # Arrange
    await _seed_template(database)
    await _seed_workspace(database, "ws_stale", last_seen=NOW - timedelta(hours=48))

    # Act
    await cleaner(database, tmp_path / "artifacts").sweep()

    # Assert
    async with database.reading() as work:
        templates = await work.fetch_all("SELECT id FROM contracts WHERE workspace_id IS NULL")
        assert [row["id"] for row in templates] == ["con_template"]


async def test_one_stale_workspace_does_not_take_a_live_one_with_it(
    database: Database, tmp_path: Path
) -> None:
    # Arrange
    await _seed_workspace(database, "ws_stale", last_seen=NOW - timedelta(hours=48))
    await _seed_workspace(database, "ws_live", last_seen=NOW)
    await _seed_run_and_artifact(database, "ws_live", relative_path="live.json")

    # Act
    await cleaner(database, tmp_path / "artifacts").sweep()

    # Assert
    async with database.reading() as work:
        remaining = await work.fetch_all("SELECT id FROM workspaces")
        runs = await work.fetch_all("SELECT id FROM runs")
    assert [row["id"] for row in remaining] == ["ws_live"]
    assert [row["id"] for row in runs] == ["run_ws_live"]


async def test_eval_workspaces_are_not_aged_out_on_the_interactive_clock(
    database: Database, tmp_path: Path
) -> None:
    """FR-009 gives them a different rule — mutable state goes immediately after
    report persistence — so a shared 24-hour clock would either delete an eval
    mid-flight or keep one long past the report it existed to produce."""
    # Arrange
    await _seed_workspace(database, "ws_eval", last_seen=NOW - timedelta(hours=48), kind="eval")

    # Act
    result = await cleaner(database, tmp_path / "artifacts").sweep()

    # Assert
    assert result.workspaces_removed == 0


async def test_purging_an_eval_workspace_clears_its_mutable_state_only(
    database: Database,
) -> None:
    """The *mutable* state, not the report: the row stays so what it produced
    keeps an owner."""
    # Arrange
    await _seed_workspace(database, "ws_eval", last_seen=NOW, kind="eval")
    async with database.transaction() as work:
        await work.execute("UPDATE workspaces SET active_run_id = 'run_x' WHERE id = 'ws_eval'")

    # Act
    async with database.transaction() as work:
        await purge_eval_workspace_state(work, "ws_eval")

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT active_run_id, cleaned_at FROM workspaces WHERE id = 'ws_eval'"
        )
    assert row is not None
    assert row["active_run_id"] is None
    assert row["cleaned_at"] is not None


async def test_purging_never_touches_an_interactive_workspace(database: Database) -> None:
    """The statement is kind-scoped, so a mistaken identifier is inert rather
    than destructive."""
    # Arrange
    await _seed_workspace(database, "ws_human", last_seen=NOW)
    async with database.transaction() as work:
        await work.execute("UPDATE workspaces SET active_run_id = 'run_x' WHERE id = 'ws_human'")

    # Act
    async with database.transaction() as work:
        await purge_eval_workspace_state(work, "ws_human")

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one("SELECT active_run_id FROM workspaces WHERE id = 'ws_human'")
    assert row["active_run_id"] == "run_x"


async def test_a_stored_path_escaping_the_artifact_root_is_refused(
    database: Database, tmp_path: Path
) -> None:
    """A persisted record is untrusted input like any other (constitution §5).
    A row carrying `../..` must not turn cleanup into arbitrary deletion."""
    # Arrange
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    outside = tmp_path / "not-an-artifact.txt"
    outside.write_text("keep me", encoding="utf-8")
    await _seed_workspace(database, "ws_stale", last_seen=NOW - timedelta(hours=48))
    await _seed_run_and_artifact(database, "ws_stale", relative_path="../not-an-artifact.txt")

    # Act
    result = await cleaner(database, artifacts).sweep()

    # Assert — the workspace still expires; only the traversal is refused.
    assert result.workspaces_removed == 1
    assert result.files_removed == 0
    assert result.files_failed == 1
    assert outside.exists()


async def test_a_missing_file_does_not_abort_the_sweep(database: Database, tmp_path: Path) -> None:
    """One unreadable path must not keep every other expired workspace alive."""
    # Arrange
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    await _seed_workspace(database, "ws_stale", last_seen=NOW - timedelta(hours=48))
    await _seed_run_and_artifact(database, "ws_stale", relative_path="never-written.json")

    # Act
    result = await cleaner(database, artifacts).sweep()

    # Assert
    assert result.workspaces_removed == 1
    async with database.reading() as work:
        assert await work.fetch_all("SELECT id FROM workspaces") == []


async def test_an_empty_sweep_is_a_no_op(database: Database, tmp_path: Path) -> None:
    # Arrange / Act
    result = await cleaner(database, tmp_path / "artifacts").sweep()

    # Assert
    assert result.workspaces_removed == 0
    assert result.files_removed == 0
    assert result.files_failed == 0
