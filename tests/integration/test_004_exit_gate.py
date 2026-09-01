"""004-T13 — the milestone's exit gate, exercised end to end.

`specs/004-workspace-persistence/spec.md` lists five criteria. The per-task
suites cover each of them at the layer that owns it; this file asserts them
again through the assembled application, because a gate that passes only when
each piece is tested in isolation is a gate on the pieces and not on the
milestone.

1. Two independent clients cannot read or mutate one another's state even with
   known IDs.
2. Cross-workspace run, contract, confirmation, artifact, and reset attempts
   fail.
3. Resource, rate, and lock failures leave no partial target or evidence state.
4. Reset cancels nonterminal state and unresolved confirmations while retaining
   terminal artifacts and the selected contract.
5. The service starts with Buggy Store disabled and reports the adapter as
   unavailable.

Every client here is a *separate* `httpx.AsyncClient` with its own cookie jar,
which is what makes it a separate visitor. A single client switching cookies
would test the cookie, not the isolation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.application import limits as fr008
from actionwitness_service.application import rate_limits as fr009
from actionwitness_service.persistence.database import Database
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENABLED = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"}
DISABLED = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}
WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(
        environ={**ENABLED, "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts")},
        database_path=tmp_path / "harness.sqlite3",
    )
    async with application.router.lifespan_context(application):
        yield application


def client(app: FastAPI) -> httpx.AsyncClient:
    """One visitor: its own cookie jar, and therefore its own workspace."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _workspace_id(visitor: httpx.AsyncClient) -> str:
    return str((await visitor.get(WORKSPACE)).json()["workspace_id"])


async def _seed_everything(app: FastAPI, workspace_id: str, tag: str) -> dict[str, str]:
    """One row of every workspace-owned kind, so nothing is checked by proxy."""
    database: Database = app.state.database
    ids = {
        "run": f"run_{tag}",
        "contract": f"con_{tag}",
        "confirmation": f"cnf_{tag}",
        "artifact": f"art_{tag}",
    }
    async with database.transaction() as work:
        await work.execute(
            """
            INSERT INTO runs (
                id, workspace_id, target_id, target_adapter_id,
                implementation_version, status, started_at
            ) VALUES (?, ?, 'buggy-store', 'integrations.buggy_store', '0.1.0', 'running', ?)
            """,
            (ids["run"], workspace_id, work.now()),
        )
        await work.execute(
            """
            INSERT INTO contracts (
                id, workspace_id, content_hash, name, schema_version,
                document_json, created_at
            ) VALUES (?, ?, 'sha256:x', 'mine', '1.0.0', '{}', ?)
            """,
            (ids["contract"], workspace_id, work.now()),
        )
        await work.execute(
            """
            INSERT INTO confirmation_requests (
                id, workspace_id, run_id, correlation_id, tool_name,
                state_binding_hash, consequence_summary_json, status,
                expires_at, created_at
            ) VALUES (?, ?, ?, 'corr_1', 'checkout', 'sha256:x', '{}', 'pending', ?, ?)
            """,
            (ids["confirmation"], workspace_id, ids["run"], work.now(), work.now()),
        )
        await work.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, run_id, artifact_type, schema_version,
                content_hash, metadata_json, relative_path, byte_size, created_at
            ) VALUES (?, ?, ?, 'outcome_report', '1.0.0', 'sha256:x', '{}', ?, 8, ?)
            """,
            (ids["artifact"], workspace_id, ids["run"], f"{tag}.json", work.now()),
        )
        await work.execute(
            "UPDATE workspaces SET active_run_id = ?, selected_contract_id = ? WHERE id = ?",
            (ids["run"], ids["contract"], workspace_id),
        )
    return ids


# --- gate 1 and 2: two clients, known identifiers, nothing granted ----------


async def test_gate_1_two_clients_get_separate_workspaces_and_separate_state(
    app: FastAPI,
) -> None:
    # Arrange
    async with client(app) as alice, client(app) as bob:
        alice_id = await _workspace_id(alice)
        bob_id = await _workspace_id(bob)
        await _seed_everything(app, alice_id, "alice")

        # Act
        alice_view = (await alice.get(WORKSPACE)).json()
        bob_view = (await bob.get(WORKSPACE)).json()

    # Assert
    assert alice_id != bob_id
    assert alice_view["active_run"]["id"] == "run_alice"
    assert bob_view["active_run"] is None
    assert bob_view["selected_contract_id"] is None


@pytest.mark.parametrize(
    ("kind", "path_template"),
    [
        ("contract", f"{CONTRACTS}/{{id}}"),
    ],
)
async def test_gate_2_a_known_identifier_grants_no_read(
    app: FastAPI, kind: str, path_template: str
) -> None:
    """The routes that exist today. Runs, confirmations, and artifacts are
    covered at the same `WorkspaceScope` by `test_workspace_authorization.py`,
    which is the guard every M4/M5 route will call."""
    # Arrange
    async with client(app) as alice, client(app) as bob:
        alice_id = await _workspace_id(alice)
        await _workspace_id(bob)
        ids = await _seed_everything(app, alice_id, "alice")

        # Act — Bob is handed the identifier outright.
        response = await bob.get(path_template.format(id=ids[kind]))

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_gate_2_a_known_identifier_grants_no_mutation(app: FastAPI) -> None:
    """Selecting somebody else's contract must not land in either workspace."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as alice, client(app) as bob:
        alice_id = await _workspace_id(alice)
        bob_id = await _workspace_id(bob)
        ids = await _seed_everything(app, alice_id, "alice")

        # Act
        response = await bob.post(f"{CONTRACTS}/{ids['contract']}/select")

    # Assert
    assert response.status_code == 404
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id, selected_contract_id FROM workspaces ORDER BY id")
    selected = {row["id"]: row["selected_contract_id"] for row in rows}
    assert selected[alice_id] == ids["contract"]
    assert selected[bob_id] is None


async def test_gate_2_a_cross_workspace_reset_attempt_changes_nothing(
    app: FastAPI,
) -> None:
    """Reset takes no workspace parameter, so the hardest available attempt is
    to reset with `purge_completed` and hope it reaches further than the cookie."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as alice, client(app) as bob:
        alice_id = await _workspace_id(alice)
        await _workspace_id(bob)
        ids = await _seed_everything(app, alice_id, "alice")

        # Act
        body = (await bob.post(f"{WORKSPACE}/reset", json={"purge_completed": True})).json()

    # Assert
    assert body["runs_cancelled"] == 0
    assert body["runs_purged"] == 0
    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (ids["run"],))
        confirmation = await work.fetch_one(
            "SELECT status FROM confirmation_requests WHERE id = ?", (ids["confirmation"],)
        )
        artifact = await work.fetch_one("SELECT id FROM artifacts WHERE id = ?", (ids["artifact"],))
    assert run["status"] == "running"
    assert confirmation["status"] == "pending"
    assert artifact is not None


# --- gate 3: no partial state under resource, rate, or lock failure ---------


async def test_gate_3_a_resource_ceiling_refusal_leaves_no_partial_state(
    app: FastAPI,
) -> None:
    """FR-008's ceiling, checked inside the unit of work that would write."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
    async with database.transaction() as work:
        for index in range(fr008.OUTCOME_RUNS_PER_WORKSPACE):
            await work.execute(
                """
                INSERT INTO runs (
                    id, workspace_id, target_id, target_adapter_id,
                    implementation_version, status, started_at
                ) VALUES (?, ?, 'buggy-store', 'a', '0.1.0', 'passed', ?)
                """,
                (f"run_{index}", workspace_id, work.now()),
            )

    from actionwitness_service.api.errors import ApiError
    from actionwitness_service.application.limits import WorkspaceCeilings

    # Act — a unit of work that writes first and is refused afterwards.
    with pytest.raises(ApiError):
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO runs (
                    id, workspace_id, target_id, target_adapter_id,
                    implementation_version, status, started_at
                ) VALUES ('run_over', ?, 'buggy-store', 'a', '0.1.0', 'armed', ?)
                """,
                (workspace_id, work.now()),
            )
            await WorkspaceCeilings(work, workspace_id).guard_new_run()

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id FROM runs WHERE id = 'run_over'")
    assert rows == []


async def test_gate_3_a_rate_limit_refusal_leaves_no_partial_state(
    app: FastAPI,
) -> None:
    """FR-009: "shall never partially commit a mutation."

    The refused request is a real mutating route, and the assertion is that the
    workspace it would have changed is unchanged.
    """
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        for _ in range(fr009.REQUEST_BURST + 2):
            await visitor.get(WORKSPACE)

        # Act
        refused = await visitor.put(
            f"{WORKSPACE}/failure-profile", json={"failure_profile": "undeclared_side_effect"}
        )

    # Assert
    assert refused.status_code == 429
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT failure_profile FROM workspaces WHERE id = ?", (workspace_id,)
        )
    assert row["failure_profile"] is None


async def test_gate_3_a_lock_timeout_leaves_no_partial_state(app: FastAPI) -> None:
    """ADR-0003's admission control, refusing rather than interleaving.

    One holder occupies the workspace lock while a second mutation arrives. The
    second is refused with the stable retryable code and writes nothing.
    """
    # Arrange
    from actionwitness_service.api.errors import ApiError, ApiErrorCode
    from actionwitness_service.persistence.locks import WorkspaceLocks

    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)

    locks = WorkspaceLocks(timeout_seconds=0.01)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with locks.hold(workspace_id):
            entered.set()
            await release.wait()

    holding = asyncio.create_task(holder())
    await entered.wait()

    # Act
    with pytest.raises(ApiError) as caught:
        async with locks.hold(workspace_id), database.transaction() as work:
            await work.execute(
                "UPDATE workspaces SET failure_profile = 'never' WHERE id = ?",
                (workspace_id,),
            )

    release.set()
    await holding

    # Assert
    assert caught.value.code is ApiErrorCode.WORKSPACE_LOCK_TIMEOUT
    assert caught.value.as_envelope()["error"]["retryable"] is True
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT failure_profile FROM workspaces WHERE id = ?", (workspace_id,)
        )
    assert row["failure_profile"] is None


# --- gate 4: reset semantics ------------------------------------------------


async def test_gate_4_reset_cancels_in_flight_work_and_retains_the_rest(
    app: FastAPI,
) -> None:
    """Both halves of FR-013 in one assertion block, because the requirement is
    one sentence and an implementation that satisfies only the first half would
    pass a test written for the first half alone."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        workspace_id = await _workspace_id(visitor)
        ids = await _seed_everything(app, workspace_id, "solo")
        # A second, already-finished run whose artifact must survive.
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO runs (
                    id, workspace_id, target_id, target_adapter_id,
                    implementation_version, status, started_at, completed_at
                ) VALUES ('run_done', ?, 'buggy-store', 'a', '0.1.0', 'passed', ?, ?)
                """,
                (workspace_id, work.now(), work.now()),
            )
            await work.execute(
                """
                INSERT INTO artifacts (
                    id, workspace_id, run_id, artifact_type, schema_version,
                    content_hash, metadata_json, relative_path, byte_size, created_at
                ) VALUES ('art_done', ?, 'run_done', 'outcome_report', '1.0.0',
                          'sha256:x', '{}', 'done.json', 8, ?)
                """,
                (workspace_id, work.now()),
            )

        # Act
        body = (await visitor.post(f"{WORKSPACE}/reset")).json()
        after = (await visitor.get(WORKSPACE)).json()

    # Assert — cancelled.
    assert body["runs_cancelled"] == 1
    assert body["confirmations_cancelled"] == 1
    async with database.reading() as work:
        cancelled = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (ids["run"],))
        confirmation = await work.fetch_one(
            "SELECT status FROM confirmation_requests WHERE id = ?", (ids["confirmation"],)
        )
        finished = await work.fetch_one("SELECT status FROM runs WHERE id = 'run_done'")
        artifacts = await work.fetch_all("SELECT id FROM artifacts ORDER BY id")
        events = await work.fetch_all(
            "SELECT event_type FROM events WHERE run_id = ? ORDER BY sequence_number",
            (ids["run"],),
        )
    assert cancelled["status"] == "cancelled"
    assert confirmation["status"] == "cancelled"
    # FR-013: "append the appropriate cancellation events".
    assert {"run_cancelled", "confirmation_cancelled"} <= {row["event_type"] for row in events}

    # Assert — retained. This is the half a simpler implementation drops.
    assert finished["status"] == "passed"
    assert [row["id"] for row in artifacts] == ["art_done", ids["artifact"]]
    assert after["selected_contract_id"] == ids["contract"]
    assert after["active_run"] is None


# --- gate 5: a clean start with the target disabled ------------------------


async def test_gate_5_the_service_starts_with_the_buggy_store_disabled(
    tmp_path: Path,
) -> None:
    """The service must come up, migrate, serve, and *say* the target is off."""
    # Arrange
    application = create_app(environ=DISABLED, database_path=tmp_path / "harness.sqlite3")

    # Act
    async with application.router.lifespan_context(application), client(application) as visitor:
        health = await visitor.get("/healthz")
        workspace = await visitor.get(WORKSPACE)
        templates = await visitor.get(f"{CONTRACTS}/templates")

    # Assert
    assert health.status_code == 200
    assert application.state.schema_version >= 1
    assert workspace.status_code == 200

    capability = workspace.json()["capabilities"]["buggy_store"]
    assert capability["status"] == "disabled"
    # "reports the adapter as unavailable" means a reason a human can act on,
    # not merely an absence.
    assert capability["reason"]

    # No target, so no templates — and that is a served empty list, not a 500.
    assert templates.status_code == 200
    assert templates.json()["templates"] == []


async def test_gate_5_selecting_a_contract_for_a_disabled_target_is_bounded(
    tmp_path: Path,
) -> None:
    """FR-024's own example. The refusal is a 409 with a reason, not a crash."""
    # Arrange — seed while enabled, then restart with the target off.
    seeding = create_app(environ=ENABLED, database_path=tmp_path / "harness.sqlite3")
    async with seeding.router.lifespan_context(seeding):
        pass

    application = create_app(environ=DISABLED, database_path=tmp_path / "harness.sqlite3")
    async with application.router.lifespan_context(application):
        database: Database = application.state.database
        async with database.reading() as work:
            rows = await work.fetch_all(
                "SELECT id FROM contracts WHERE workspace_id IS NULL ORDER BY id"
            )

        # Act
        async with client(application) as visitor:
            response = await visitor.post(f"{CONTRACTS}/{rows[0]['id']}/select")
            after = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TARGET_UNAVAILABLE"
    assert after["selected_contract_id"] is None
    assert after["selected_target_id"] is None
