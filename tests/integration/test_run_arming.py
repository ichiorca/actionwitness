"""005-T1 — arming an outcome run (FR-030, FR-012, FR-025, FR-039, FR-040).

The harness runs against a **real Buggy Store**, reached the way production
reaches it: an injected `httpx.AsyncClient` wired to the store's own ASGI app
(ADR-0001). Nothing here fakes the observation provider, because the property
under test is that arming captures *authoritative* state — and a fake provider
would make that assertion vacuous.

Three properties carry this task:

* a failed precondition creates **neither a run nor a partial snapshot**, which
  is asserted by counting rows after the refusal rather than by trusting that
  the check ran early enough;
* the observation that preconditions were validated against is the **same
  value** that gets persisted, so the baseline a verdict later rests on is the
  one the contract was admitted against;
* a configuration change during the capture refuses instead of arming a run
  against a selection nobody made. That is FR-012 under concurrency, and it is
  the failure a single-client test cannot see.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.security.canonical import content_hash
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

from integrations.buggy_store import (
    OBSERVATION_NAMESPACE,
    PROVENANCE,
    PROVIDER_ID,
    TARGET_ID,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"}
WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"

#: The template whose preconditions an empty store satisfies: §10.1's canonical
#: example asserts an empty cart and no order, which is exactly a fresh
#: workspace.
CANONICAL = "one_mug_save20_no_checkout"


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """The harness, wired to a real store over ADR-0001's injected client."""
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


async def _select_canonical_contract(visitor: httpx.AsyncClient) -> str:
    """Walk the 004 routes a real client would: list, then select."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    selected = await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    assert selected.status_code == 200
    return str(chosen["contract_id"])


# --- the happy path ---------------------------------------------------------


async def test_arming_creates_the_run_its_snapshot_and_its_events(stack: FastAPI) -> None:
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        contract_id = await _select_canonical_contract(visitor)

        # Act
        response = await visitor.post(RUNS)

    # Assert — the wire answer.
    assert response.status_code == 201
    body = response.json()
    run_id = body["run_id"]
    assert body["status"] == "armed"
    assert body["contract_id"] == contract_id
    assert body["target_id"] == TARGET_ID
    assert body["initial_snapshot"]["content_hash"].startswith("sha256:")

    # Assert — everything landed together.
    async with database.reading() as work:
        run = await work.fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        snapshot = await work.fetch_one(
            "SELECT phase, provider, content_hash, redacted_state_json FROM snapshots "
            "WHERE run_id = ?",
            (run_id,),
        )
        events = await work.fetch_all(
            "SELECT event_type, actor FROM events WHERE run_id = ? ORDER BY sequence_number",
            (run_id,),
        )
    assert run["status"] == "armed"
    assert snapshot["phase"] == "before"
    # The run's own creation is its first event: a `snapshot_captured` at
    # sequence 1 would describe a run that, by its own timeline, did not exist.
    assert [row["event_type"] for row in events] == ["run_armed", "snapshot_captured"]
    assert [row["actor"] for row in events] == ["human", "harness"]


async def test_the_persisted_snapshot_is_the_value_preconditions_were_checked_against(
    stack: FastAPI,
) -> None:
    """FR-030: read once, validate against that exact value, persist it.

    If the harness re-read the target to persist, the baseline a verdict later
    rests on would be a *different* observation than the one that admitted the
    contract — and nothing downstream would notice.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)

        # Act
        body = (await visitor.post(RUNS)).json()

    # Assert — the hash the caller was told matches the stored payload's hash.
    async with database.reading() as work:
        snapshot = await work.fetch_one(
            "SELECT content_hash, redacted_state_json FROM snapshots WHERE run_id = ?",
            (body["run_id"],),
        )
    stored = json.loads(snapshot["redacted_state_json"])
    assert snapshot["content_hash"] == content_hash(stored)
    assert body["initial_snapshot"]["content_hash"] == snapshot["content_hash"]


async def test_the_snapshot_holds_authoritative_state_not_a_manufactured_one(
    stack: FastAPI,
) -> None:
    """The observation came from the store's own canonical read (FR-040)."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)
        body = (await visitor.post(RUNS)).json()

    # Assert
    async with database.reading() as work:
        snapshot = await work.fetch_one(
            "SELECT provider, namespace, provenance, redacted_state_json FROM snapshots "
            "WHERE run_id = ?",
            (body["run_id"],),
        )
    payload = json.loads(snapshot["redacted_state_json"])
    assert snapshot["namespace"] == OBSERVATION_NAMESPACE
    assert snapshot["provider"] == PROVIDER_ID
    assert snapshot["provenance"] == PROVENANCE
    # A real store document, not an empty stand-in.
    assert {"cart", "order"} <= set(payload)


async def test_the_run_records_its_controlled_inputs(stack: FastAPI) -> None:
    """FR-012: the configuration is copied in at arming and never updated."""
    # Arrange
    async with client(stack) as visitor:
        contract_id = await _select_canonical_contract(visitor)
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})
        await visitor.put(
            f"{WORKSPACE}/failure-profile",
            json={"failure_profile": "discount_reported_but_not_applied"},
        )

        # Act
        body = (await visitor.post(RUNS)).json()

    # Assert
    database: Database = stack.state.database
    async with database.reading() as work:
        run = await work.fetch_one("SELECT * FROM runs WHERE id = ?", (body["run_id"],))
        contract = await work.fetch_one(
            "SELECT content_hash FROM contracts WHERE id = ?", (contract_id,)
        )
    assert run["contract_content_hash"] == contract["content_hash"]
    assert run["scenario_mode"] == "pre_fix"
    assert run["failure_profile"] == "discount_reported_but_not_applied"
    assert run["target_adapter_id"] == "buggy_store"
    assert run["implementation_version"]
    assert run["intent_content_hash"].startswith("sha256:")


async def test_arming_points_the_workspace_at_its_active_run(stack: FastAPI) -> None:
    # Arrange
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)

        # Act
        body = (await visitor.post(RUNS)).json()
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert workspace["active_run"]["id"] == body["run_id"]
    assert workspace["next_action"] == "await_active_run"


# --- refusals that write nothing --------------------------------------------


async def test_a_failed_precondition_creates_neither_a_run_nor_a_snapshot(
    stack: FastAPI,
) -> None:
    """FR-030's refusal, and the reason it is checked by counting rows.

    The canonical contract requires an empty cart. Putting a mug in the cart
    first makes the precondition fail against real observed state rather than
    against a contrived one.
    """
    # Arrange — a mug in the cart, placed through the store's own API.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _put_a_mug_in_the_cart(stack, workspace_id)

        # Act
        response = await visitor.post(RUNS)

    # Assert — 409, which is the registry's status for this code: the request
    # was well-formed and the target's state is what refused it.
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "PRECONDITION_FAILED"
    # Every unmet precondition at once, so a caller does not fix them one round
    # trip at a time.
    assert body["details"]
    assert any("cart" in detail["path"] for detail in body["details"])

    async with database.reading() as work:
        assert await work.fetch_all("SELECT id FROM runs") == []
        assert await work.fetch_all("SELECT id FROM snapshots") == []
        assert await work.fetch_all("SELECT id FROM events") == []


async def test_arming_without_a_selected_contract_is_refused(stack: FastAPI) -> None:
    # Arrange / Act
    async with client(stack) as visitor:
        await visitor.get(WORKSPACE)
        response = await visitor.post(RUNS)

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_a_second_run_is_refused_while_one_is_in_flight(stack: FastAPI) -> None:
    """FR-039's lease at its first gate."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)
        first = await visitor.post(RUNS)
        assert first.status_code == 201

        # Act
        second = await visitor.post(RUNS)

    # Assert
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "RUN_IN_PROGRESS"
    async with database.reading() as work:
        runs = await work.fetch_all("SELECT id FROM runs")
    assert len(runs) == 1


async def test_a_terminal_run_does_not_block_arming_another(stack: FastAPI) -> None:
    """Only in-flight work holds the lease; a finished run must not lock the
    workspace out of ever running again."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)
        first = (await visitor.post(RUNS)).json()
        async with database.transaction() as work:
            await work.execute("UPDATE runs SET status = 'passed' WHERE id = ?", (first["run_id"],))

        # Act
        second = await visitor.post(RUNS)

    # Assert
    assert second.status_code == 201
    assert second.json()["run_id"] != first["run_id"]


async def test_proposal_mode_is_refused_rather_than_silently_downgraded(
    stack: FastAPI,
) -> None:
    """Arming a verification run for someone who asked for a proposal would be
    worse than saying no."""
    # Arrange / Act
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)
        response = await visitor.post(RUNS, json={"mode": "proposal"})

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_an_unknown_mode_is_refused(stack: FastAPI) -> None:
    # Arrange / Act
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)
        response = await visitor.post(RUNS, json={"mode": "whatever"})

    # Assert
    assert response.status_code == 422


async def test_an_unknown_body_field_is_refused(stack: FastAPI) -> None:
    # Arrange / Act
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)
        response = await visitor.post(RUNS, json={"contract_id": "con_somebody_elses"})

    # Assert — §15.3 takes no contract identifier; accepting one would
    # reintroduce the combination FR-024 forbids.
    assert response.status_code == 422


async def test_arming_is_refused_when_the_target_is_unavailable(tmp_path: Path) -> None:
    """A bounded refusal, not a crash, and nothing written (§21.1)."""
    # Arrange — seed templates with the target on, then run with it off.
    seeding = create_app(environ=ENV, database_path=tmp_path / "harness.sqlite3")
    async with seeding.router.lifespan_context(seeding):
        pass

    off = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}
    harness = create_app(environ=off, database_path=tmp_path / "harness.sqlite3")
    async with harness.router.lifespan_context(harness):
        database: Database = harness.state.database
        async with database.reading() as work:
            rows = await work.fetch_all(
                "SELECT id FROM contracts WHERE workspace_id IS NULL ORDER BY id"
            )
        async with client(harness) as visitor:
            workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
            # Selection is refused too, so the column is set directly to reach
            # the arming path with a target that cannot be resolved.
            async with database.transaction() as work:
                await work.execute(
                    "UPDATE workspaces SET selected_contract_id = ? WHERE id = ?",
                    (rows[0]["id"], workspace_id),
                )

            # Act
            response = await visitor.post(RUNS)

        async with database.reading() as work:
            runs = await work.fetch_all("SELECT id FROM runs")

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TARGET_UNAVAILABLE"
    assert runs == []


# --- concurrency ------------------------------------------------------------


async def test_two_clients_arm_independent_runs_in_their_own_workspaces(
    stack: FastAPI,
) -> None:
    """AC-11 at the arming route: separate workspaces, separate targets state,
    separate runs."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as alice, client(stack) as bob:
        await _select_canonical_contract(alice)
        await _select_canonical_contract(bob)

        # Act
        first = await alice.post(RUNS)
        second = await bob.post(RUNS)

    # Assert
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["run_id"] != second.json()["run_id"]
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id, workspace_id FROM runs")
    assert len({row["workspace_id"] for row in rows}) == 2


async def test_concurrent_arming_in_one_workspace_produces_exactly_one_run(
    stack: FastAPI,
) -> None:
    """Two tabs, one workspace. The lease is decided in the transaction, so the
    loser is refused rather than both being admitted."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)

        # Act
        first, second = await asyncio.gather(visitor.post(RUNS), visitor.post(RUNS))

    # Assert
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [201, 409]
    async with database.reading() as work:
        runs = await work.fetch_all("SELECT id FROM runs")
    assert len(runs) == 1


async def test_a_configuration_change_during_the_capture_refuses_to_arm(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-012 under concurrency.

    The observation happens outside the transaction, by ADR-0003's rule that
    nothing is held across a wait. So the arming transaction re-reads the
    configuration and refuses if it moved — otherwise a run would be armed
    against a contract the workspace no longer has selected, and its evidence
    would be labelled with a selection nobody made.
    """
    # Arrange — swap the selected contract while the capture is in flight.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_canonical_contract(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        other = next(t for t in templates if t["source_template_id"] != CANONICAL)

        from actionwitness_service.application import run_service

        original = run_service.RunService._capture

        async def capture_then_swap(self, selected, ws_id):  # type: ignore[no-untyped-def]
            observation = await original(self, selected, ws_id)
            async with database.transaction() as work:
                await work.execute(
                    "UPDATE workspaces SET selected_contract_id = ? WHERE id = ?",
                    (other["contract_id"], workspace_id),
                )
            return observation

        monkeypatch.setattr(run_service.RunService, "_capture", capture_then_swap)

        # Act
        response = await visitor.post(RUNS)

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_IN_PROGRESS"
    async with database.reading() as work:
        assert await work.fetch_all("SELECT id FROM runs") == []
        assert await work.fetch_all("SELECT id FROM snapshots") == []


# --- helpers ----------------------------------------------------------------


async def _put_a_mug_in_the_cart(harness: FastAPI, workspace_id: str) -> None:
    """Change real target state through the store's own API.

    Reached through the same injected client the adapter uses, so the state the
    harness later observes is state the store actually holds rather than a row
    this test wrote into the harness's database.
    """
    registry = harness.state.adapters
    adapter = registry.adapter("buggy_store")
    client_ = adapter._client
    response = await client_.post(
        "/demo/api/v1/store/cart/mutations",
        headers={"X-Workspace-Id": workspace_id},
        json={
            "product_id": "mug-ceramic-001",
            "quantity": 1,
            "request_id": "req_precondition_setup",
        },
    )
    assert response.status_code < 400, response.text
