"""005-T2 — guidance transitions as append-only evidence (§12.13, §16.1, §17.1).

§12.13: "Guidance before a run exists is recorded in the separate
workspace-scoped `guidance_events` stream. After arming, `guidance_transitioned`
is also appended to the run timeline **using the same guidance-event ID**."

That shared identifier is the property under test. Two rows written at the same
moment are not linked — a reader reconstructing "who was asked to do what" would
be guessing from timestamps. So the assertion is that the run event names the
guidance row's own id, not that the two exist.

The other property is FR-125's: guidance is *evidence*. It is append-only, and a
later transition adds a row rather than editing the last one.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.journeys.guidance import COPY_VERSION
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.application.guidance_service import GuidanceRecorder
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"}
WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    store = create_store(database_path=tmp_path / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ=ENV,
            database_path=tmp_path / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            yield harness


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _arm(visitor: httpx.AsyncClient) -> str:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    armed = await visitor.post(RUNS)
    assert armed.status_code == 201, armed.text
    return str(armed.json()["run_id"])


# --- the two streams, joined by one id --------------------------------------


async def test_arming_records_a_guidance_event_in_the_workspace_stream(
    stack: FastAPI,
) -> None:
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT * FROM guidance_events ORDER BY workspace_version")
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == run_id
    assert row["phase"] == "armed"
    assert row["active_actor"] == "agent"
    assert row["action_code"] == "invoke_target_tool"
    # §12.13: the rendered sentence and the version it came from are stored
    # together, so a historical row stays readable after the copy changes.
    assert row["copy_version"] == COPY_VERSION
    assert row["instruction"]
    assert row["reason"]
    assert row["expected_consequence"]


async def test_the_run_timeline_references_the_guidance_events_own_id(
    stack: FastAPI,
) -> None:
    """The property §12.13 actually states, and the one a timestamp cannot give."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

    # Assert
    async with database.reading() as work:
        guidance = await work.fetch_one(
            "SELECT id, correlation_id FROM guidance_events WHERE run_id = ?", (run_id,)
        )
        event = await work.fetch_one(
            "SELECT redacted_payload_json, correlation_id, actor FROM events "
            "WHERE run_id = ? AND event_type = 'guidance_transitioned'",
            (run_id,),
        )
    payload = json.loads(event["redacted_payload_json"])
    assert payload["guidance_event_id"] == guidance["id"]
    assert event["correlation_id"] == guidance["correlation_id"]
    # The server moved guidance; the actor it is addressed to did not act.
    assert event["actor"] == "harness"


async def test_the_run_event_carries_the_stable_code_not_only_the_copy(
    stack: FastAPI,
) -> None:
    """§12.13 makes the action code the durable half; a payload holding only a
    sentence would become unreadable the first time the copy changed."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

    # Assert
    async with database.reading() as work:
        event = await work.fetch_one(
            "SELECT redacted_payload_json FROM events "
            "WHERE run_id = ? AND event_type = 'guidance_transitioned'",
            (run_id,),
        )
    payload = json.loads(event["redacted_payload_json"])
    assert payload["action_code"] == "invoke_target_tool"
    assert payload["phase"] == "armed"
    assert payload["active_actor"] == "agent"


# --- append-only ------------------------------------------------------------


async def test_a_later_transition_appends_rather_than_editing(stack: FastAPI) -> None:
    """FR-125: guidance transitions are append-only evidence."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

    from actionwitness_core.journeys.enums import WorkspacePhase
    from actionwitness_core.journeys.guidance import derive_guidance

    # Act
    async with database.transaction() as work:
        await GuidanceRecorder(work, workspace_id).append(
            derive_guidance(WorkspacePhase.RUNNING, correlation_id=run_id), run_id=run_id
        )

    # Assert — two rows, and the first is untouched.
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT phase, workspace_version FROM guidance_events "
            "WHERE workspace_id = ? ORDER BY workspace_version",
            (workspace_id,),
        )
    assert [row["phase"] for row in rows] == ["armed", "running"]
    assert [row["workspace_version"] for row in rows] == [1, 2]


async def test_workspace_version_is_monotonic_within_a_workspace(
    stack: FastAPI,
) -> None:
    """The ordering guidance needs before any run exists, which the run timeline
    cannot provide."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await visitor.get(WORKSPACE)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

    from actionwitness_core.journeys.enums import WorkspacePhase
    from actionwitness_core.journeys.guidance import derive_guidance

    # Act — five transitions with no run at all.
    for _ in range(5):
        async with database.transaction() as work:
            await GuidanceRecorder(work, workspace_id).append(
                derive_guidance(WorkspacePhase.NO_CONTRACT)
            )

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT workspace_version, run_id FROM guidance_events WHERE workspace_id = ? "
            "ORDER BY workspace_version",
            (workspace_id,),
        )
    assert [row["workspace_version"] for row in rows] == [1, 2, 3, 4, 5]
    assert all(row["run_id"] is None for row in rows)


async def test_two_workspaces_number_their_guidance_independently(
    stack: FastAPI,
) -> None:
    """AC-11 again: the version is per workspace, not global."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as alice, client(stack) as bob:
        await _arm(alice)
        await _arm(bob)

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT workspace_id, workspace_version FROM guidance_events")
    assert len(rows) == 2
    assert {row["workspace_version"] for row in rows} == {1}
    assert len({row["workspace_id"] for row in rows}) == 2


async def test_a_failed_arming_records_no_guidance(stack: FastAPI) -> None:
    """The guidance append shares arming's transaction, so a refusal leaves no
    banner claiming a run began."""
    # Arrange — no contract selected, so arming is refused.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await visitor.get(WORKSPACE)

        # Act
        response = await visitor.post(RUNS)

    # Assert
    assert response.status_code == 422
    async with database.reading() as work:
        assert await work.fetch_all("SELECT id FROM guidance_events") == []


# --- what the status route serves -------------------------------------------


async def test_the_status_route_serves_guidance_and_a_matching_next_action(
    stack: FastAPI,
) -> None:
    """§26.1: the banner and the compact form resolve from one derivation."""
    # Arrange / Act
    async with client(stack) as visitor:
        await _arm(visitor)
        body = (await visitor.get(WORKSPACE)).json()

    # Assert
    guidance = body["guidance"]
    compact = body["next_action"]
    assert guidance["phase"] == "armed"
    assert compact["actor"] == guidance["active_actor"]
    assert compact["action_code"] == guidance["action_code"]
    assert compact["instruction"] == guidance["instruction"]


async def test_guidance_moves_with_the_run_state(stack: FastAPI) -> None:
    """The banner follows authoritative state rather than a stored sentence."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        armed = (await visitor.get(WORKSPACE)).json()["guidance"]["phase"]

        async with database.transaction() as work:
            await work.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))

        # Act
        running = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert armed == "armed"
    assert running["guidance"]["phase"] == "running"
    assert running["next_action"]["action_code"] == "verify_outcome"


async def test_guidance_is_workspace_scoped(stack: FastAPI) -> None:
    """A second client sees its own guidance, not the first client's."""
    # Arrange
    async with client(stack) as alice, client(stack) as bob:
        await _arm(alice)
        await bob.get(WORKSPACE)

        # Act
        alice_view = (await alice.get(WORKSPACE)).json()
        bob_view = (await bob.get(WORKSPACE)).json()

    # Assert
    assert alice_view["guidance"]["phase"] == "armed"
    assert bob_view["guidance"]["phase"] == "no_contract"
