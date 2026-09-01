"""004-T11 — §15.1's four workspace routes, with two clients throughout.

The exit-gate item this file exists for is **reset semantics**. FR-013 has two
halves and only one of them is obvious:

* cancel nonterminal runs and unresolved confirmations, appending the
  cancellation events;
* **preserve completed artifacts and the selected contract** so the workspace
  returns to `ContractReady`.

A reset that cleared everything would be simpler and would destroy the evidence
this product exists to keep, so the retention half is tested on its own rather
than assumed. `purge_completed` is the opt-in path that does remove terminal
evidence, and it is tested separately — including that it leaves the *other*
workspace's runs and the built-in templates alone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.persistence.database import Database
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"}
WORKSPACE = f"{API_PREFIX}/workspace"


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(environ=ENV, database_path=tmp_path / "harness.sqlite3")
    async with application.router.lifespan_context(application):
        yield application


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _workspace_id(visitor: httpx.AsyncClient) -> str:
    response = await visitor.get(WORKSPACE)
    assert response.status_code == 200
    return str(response.json()["workspace_id"])


async def _seed(
    app: FastAPI,
    workspace_id: str,
    *,
    run_id: str,
    status: str,
    contract_id: str | None = None,
    with_confirmation: bool = False,
    with_artifact: bool = False,
) -> None:
    database: Database = app.state.database
    async with database.transaction() as work:
        if contract_id is not None:
            await work.execute(
                """
                INSERT INTO contracts (
                    id, workspace_id, content_hash, name, schema_version,
                    document_json, created_at
                ) VALUES (?, ?, 'sha256:x', 'a contract', '1.0.0', '{}', ?)
                """,
                (contract_id, workspace_id, work.now()),
            )
            await work.execute(
                "UPDATE workspaces SET selected_contract_id = ? WHERE id = ?",
                (contract_id, workspace_id),
            )
        await work.execute(
            """
            INSERT INTO runs (
                id, workspace_id, target_id, target_adapter_id,
                implementation_version, status, started_at
            ) VALUES (?, ?, 'buggy-store', 'integrations.buggy_store', '0.1.0', ?, ?)
            """,
            (run_id, workspace_id, status, work.now()),
        )
        await work.execute(
            "UPDATE workspaces SET active_run_id = ? WHERE id = ?", (run_id, workspace_id)
        )
        if with_confirmation:
            await work.execute(
                """
                INSERT INTO confirmation_requests (
                    id, workspace_id, run_id, correlation_id, tool_name,
                    state_binding_hash, consequence_summary_json, status,
                    expires_at, created_at
                ) VALUES (?, ?, ?, 'corr_1', 'checkout', 'sha256:x', '{}', 'pending', ?, ?)
                """,
                (f"cnf_{run_id}", workspace_id, run_id, work.now(), work.now()),
            )
        if with_artifact:
            await work.execute(
                """
                INSERT INTO artifacts (
                    id, workspace_id, run_id, artifact_type, schema_version,
                    content_hash, metadata_json, relative_path, byte_size, created_at
                ) VALUES (?, ?, ?, 'outcome_report', '1.0.0', 'sha256:x', '{}', ?, 4, ?)
                """,
                (f"art_{run_id}", workspace_id, run_id, f"{run_id}.json", work.now()),
            )


# --- GET /workspace ---------------------------------------------------------


async def test_a_fresh_workspace_reports_an_empty_but_complete_status(app: FastAPI) -> None:
    # Arrange / Act
    async with client(app) as visitor:
        body = (await visitor.get(WORKSPACE)).json()

    # Assert — every §15.1 field is present, even when unset.
    assert set(body) >= {
        "workspace_id",
        "selected_target_id",
        "selected_contract_id",
        "scenario_mode",
        "failure_profile",
        "active_run",
        "next_action",
        "capabilities",
    }
    assert body["active_run"] is None
    assert body["next_action"] == "select_target"


async def test_the_status_reports_capability_state(app: FastAPI) -> None:
    """§15.1 asks for capability state; §29.1's bar shows unavailable ones too."""
    # Arrange / Act
    async with client(app) as visitor:
        capabilities = (await visitor.get(WORKSPACE)).json()["capabilities"]

    # Assert
    assert "buggy_store" in capabilities
    assert capabilities["buggy_store"]["status"] in {"enabled", "disabled", "misconfigured"}


async def test_two_clients_see_only_their_own_workspace(app: FastAPI) -> None:
    """AC-11 at the status route."""
    # Arrange
    async with client(app) as first, client(app) as second:
        first_id = await _workspace_id(first)
        second_id = await _workspace_id(second)
        await _seed(app, first_id, run_id="run_first", status="running")

        # Act
        first_body = (await first.get(WORKSPACE)).json()
        second_body = (await second.get(WORKSPACE)).json()

    # Assert
    assert first_id != second_id
    assert first_body["active_run"]["id"] == "run_first"
    assert second_body["active_run"] is None


# --- reset: the cancellation half -------------------------------------------


async def test_reset_cancels_a_nonterminal_run_and_records_it(app: FastAPI) -> None:
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(app, workspace_id, run_id="run_live", status="running")

        # Act
        response = await visitor.post(f"{WORKSPACE}/reset")

    # Assert
    assert response.status_code == 200
    assert response.json()["runs_cancelled"] == 1
    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = 'run_live'")
        events = await work.fetch_all(
            "SELECT event_type FROM events WHERE run_id = 'run_live' ORDER BY sequence_number"
        )
    assert run["status"] == "cancelled"
    # FR-013: "append the appropriate cancellation events" — the run says why it
    # stopped rather than merely being relabelled.
    assert [row["event_type"] for row in events] == ["run_cancelled"]


async def test_reset_cancels_an_unresolved_confirmation(app: FastAPI) -> None:
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(
            app,
            workspace_id,
            run_id="run_live",
            status="awaiting_confirmation",
            with_confirmation=True,
        )

        # Act
        body = (await visitor.post(f"{WORKSPACE}/reset")).json()

    # Assert
    assert body["confirmations_cancelled"] == 1
    async with database.reading() as work:
        confirmation = await work.fetch_one(
            "SELECT status FROM confirmation_requests WHERE id = 'cnf_run_live'"
        )
        events = await work.fetch_all(
            "SELECT event_type FROM events WHERE run_id = 'run_live' ORDER BY sequence_number"
        )
    assert confirmation["status"] == "cancelled"
    assert "confirmation_cancelled" in [row["event_type"] for row in events]


async def test_reset_does_not_rewrite_a_confirmation_a_human_already_decided(
    app: FastAPI,
) -> None:
    """A decided confirmation is a record of what a person chose. Reset cancels
    what is unresolved; it does not revoke consent already given or refused."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(app, workspace_id, run_id="run_live", status="running", with_confirmation=True)
        async with database.transaction() as work:
            await work.execute(
                "UPDATE confirmation_requests SET status = 'approved' WHERE id = 'cnf_run_live'"
            )

        # Act
        body = (await visitor.post(f"{WORKSPACE}/reset")).json()

    # Assert
    assert body["confirmations_cancelled"] == 0
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT status FROM confirmation_requests WHERE id = 'cnf_run_live'"
        )
    assert row["status"] == "approved"


# --- reset: the retention half (exit-gate item 4) ---------------------------


async def test_reset_retains_terminal_runs_and_their_artifacts(app: FastAPI) -> None:
    """FR-013: "preserve completed artifacts". The half a plausible
    implementation gets wrong."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(app, workspace_id, run_id="run_done", status="passed", with_artifact=True)

        # Act
        body = (await visitor.post(f"{WORKSPACE}/reset")).json()

    # Assert
    assert body["runs_cancelled"] == 0
    assert body["runs_purged"] == 0
    async with database.reading() as work:
        runs = await work.fetch_all("SELECT id, status FROM runs")
        artifacts = await work.fetch_all("SELECT id FROM artifacts")
    assert [(row["id"], row["status"]) for row in runs] == [("run_done", "passed")]
    assert [row["id"] for row in artifacts] == ["art_run_done"]


async def test_reset_retains_the_selected_contract(app: FastAPI) -> None:
    """ "so the workspace returns to `ContractReady`" — which it cannot do if
    reset cleared the contract that made it ready."""
    # Arrange
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(
            app, workspace_id, run_id="run_live", status="running", contract_id="con_selected"
        )

        # Act
        reset = await visitor.post(f"{WORKSPACE}/reset")
        after = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert reset.json()["selected_contract_id"] == "con_selected"
    assert after["selected_contract_id"] == "con_selected"
    assert after["active_run"] is None
    assert after["next_action"] == "select_target"


async def test_reset_clears_the_active_run_pointer(app: FastAPI) -> None:
    # Arrange
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(app, workspace_id, run_id="run_live", status="running")

        # Act
        await visitor.post(f"{WORKSPACE}/reset")
        after = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert after["active_run"] is None


# --- reset: purge_completed -------------------------------------------------


async def test_purge_completed_removes_terminal_runs_and_artifacts(app: FastAPI) -> None:
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(app, workspace_id, run_id="run_done", status="passed", with_artifact=True)

        # Act
        body = (await visitor.post(f"{WORKSPACE}/reset", json={"purge_completed": True})).json()

    # Assert
    assert body["runs_purged"] == 1
    assert body["artifacts_purged"] == 1
    async with database.reading() as work:
        assert await work.fetch_all("SELECT id FROM runs") == []
        assert await work.fetch_all("SELECT id FROM artifacts") == []


async def test_purge_completed_preserves_built_in_templates(app: FastAPI) -> None:
    """§15.1 says so. It holds because a template belongs to no workspace, and
    every purge statement is workspace-scoped."""
    # Arrange
    database: Database = app.state.database
    async with database.reading() as work:
        before = len(await work.fetch_all("SELECT id FROM contracts WHERE workspace_id IS NULL"))
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

    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(app, workspace_id, run_id="run_done", status="passed", with_artifact=True)

        # Act
        await visitor.post(f"{WORKSPACE}/reset", json={"purge_completed": True})

    # Assert — the seeded built-ins survive alongside the one this test added.
    async with database.reading() as work:
        templates = await work.fetch_all("SELECT id FROM contracts WHERE workspace_id IS NULL")
    assert "con_template" in {row["id"] for row in templates}
    assert len(templates) == before + 1


async def test_purging_one_workspace_leaves_the_others_evidence(app: FastAPI) -> None:
    """AC-11 applied to the one route that deletes."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as first, client(app) as second:
        first_id = await _workspace_id(first)
        second_id = await _workspace_id(second)
        await _seed(app, first_id, run_id="run_first", status="passed", with_artifact=True)
        await _seed(app, second_id, run_id="run_second", status="passed", with_artifact=True)

        # Act
        await first.post(f"{WORKSPACE}/reset", json={"purge_completed": True})

    # Assert
    async with database.reading() as work:
        runs = await work.fetch_all("SELECT id FROM runs")
        artifacts = await work.fetch_all("SELECT id FROM artifacts")
    assert [row["id"] for row in runs] == ["run_second"]
    assert [row["id"] for row in artifacts] == ["art_run_second"]


async def test_an_unknown_body_field_is_refused_rather_than_ignored(app: FastAPI) -> None:
    """`{"purge": true}` quietly ignored is a user who believes they purged."""
    # Arrange / Act
    async with client(app) as visitor:
        response = await visitor.post(f"{WORKSPACE}/reset", json={"purge": True})

    # Assert
    assert response.status_code == 422


async def test_reset_of_an_untouched_workspace_is_a_no_op(app: FastAPI) -> None:
    # Arrange / Act
    async with client(app) as visitor:
        body = (await visitor.post(f"{WORKSPACE}/reset")).json()

    # Assert
    assert body["runs_cancelled"] == 0
    assert body["confirmations_cancelled"] == 0
    assert body["runs_purged"] == 0


# --- selection before arming ------------------------------------------------


async def test_a_failure_profile_can_be_selected_before_arming(app: FastAPI) -> None:
    # Arrange / Act
    async with client(app) as visitor:
        response = await visitor.put(
            f"{WORKSPACE}/failure-profile",
            json={"failure_profile": "discount_reported_but_not_applied"},
        )
        after = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert response.status_code == 200
    assert after["failure_profile"] == "discount_reported_but_not_applied"


async def test_a_failure_profile_cannot_be_changed_while_a_run_is_in_flight(
    app: FastAPI,
) -> None:
    """FR-012: an armed run's configuration is immutable, and "completed
    evidence is never relabeled"."""
    # Arrange
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(app, workspace_id, run_id="run_live", status="running")

        # Act
        response = await visitor.put(
            f"{WORKSPACE}/failure-profile", json={"failure_profile": "undeclared_side_effect"}
        )

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_IN_PROGRESS"


async def test_a_terminal_run_does_not_block_selection(app: FastAPI) -> None:
    """Only *in-flight* work blocks. A finished run must not lock the workspace."""
    # Arrange
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        await _seed(app, workspace_id, run_id="run_done", status="failed")

        # Act
        response = await visitor.put(
            f"{WORKSPACE}/failure-profile", json={"failure_profile": "none"}
        )

    # Assert
    assert response.status_code == 200


async def test_a_scenario_mode_the_adapter_advertises_is_accepted(app: FastAPI) -> None:
    # Arrange — the workspace names the target whose descriptor is consulted.
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        async with database.transaction() as work:
            await work.execute(
                "UPDATE workspaces SET selected_target_id = 'buggy-store' WHERE id = ?",
                (workspace_id,),
            )

        # Act
        response = await visitor.put(
            f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"}
        )
        after = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert response.status_code == 200
    assert after["scenario_mode"] == "pre_fix"


async def test_a_scenario_mode_the_adapter_does_not_advertise_is_refused(
    app: FastAPI,
) -> None:
    """§9.1: validated against the descriptor, not against a hardcoded pair."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        async with database.transaction() as work:
            await work.execute(
                "UPDATE workspaces SET selected_target_id = 'buggy-store' WHERE id = ?",
                (workspace_id,),
            )

        # Act
        response = await visitor.put(
            f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "external_current"}
        )

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_no_scenario_mode_is_valid_without_a_selected_target(app: FastAPI) -> None:
    """An unselected target advertises nothing, so the check fails closed rather
    than passing by default."""
    # Arrange / Act
    async with client(app) as visitor:
        response = await visitor.put(
            f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"}
        )

    # Assert
    assert response.status_code == 422


async def test_a_second_client_cannot_reset_the_first_ones_workspace(app: FastAPI) -> None:
    """There is no route parameter naming a workspace, which is the point: the
    cookie is the only input, so there is nothing for a second client to aim at."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as first, client(app) as second:
        first_id = await _workspace_id(first)
        await _workspace_id(second)
        await _seed(app, first_id, run_id="run_first", status="running")

        # Act — the second client resets, as hard as it can.
        body = (await second.post(f"{WORKSPACE}/reset", json={"purge_completed": True})).json()

    # Assert — it reset its own empty workspace, and the first one is untouched.
    assert body["runs_cancelled"] == 0
    assert body["runs_purged"] == 0
    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = 'run_first'")
    assert run["status"] == "running"
