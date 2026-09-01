"""004-T6 — workspace authorization, proved with two clients (FR-006, AC-11).

FR-006: reject access to another workspace's resource "**even when its
identifier is known**." So every test here has two clients, the second one is
*handed* the first one's identifier, and the assertion is that it still gets
nothing. A single-client test would prove the route works and say nothing about
the property under test.

The routes §15 defines for runs, confirmations, and artifacts belong to M4 and
M5. Rather than inventing them early — and canonising route shapes a later
milestone owns — these tests mount a probe router on the **real** application.
Everything the request passes through is production code: the cookie
middleware, the `WorkspaceDependency`, the `WorkspaceScope`, the `ApiError`
handler, and the §15.8 envelope. Only the leaf handler is the test's, and it
does nothing but return what the scope resolved. When those routes arrive, they
call the same scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import create_app
from actionwitness_service.api.dependencies import DatabaseDependency, WorkspaceDependency
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.application.workspaces import WORKSPACE_COOKIE_NAME
from actionwitness_service.persistence.database import Database
from fastapi import APIRouter, FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}

probe = APIRouter(prefix="/api/v1/_probe")


@probe.get("/runs/{run_id}")
async def read_run(
    run_id: str, workspace_id: WorkspaceDependency, database: DatabaseDependency
) -> dict[str, object]:
    async with database.reading() as work:
        return dict(await WorkspaceScope(work, workspace_id).run(run_id))


@probe.get("/contracts/{contract_id}")
async def read_contract(
    contract_id: str, workspace_id: WorkspaceDependency, database: DatabaseDependency
) -> dict[str, object]:
    async with database.reading() as work:
        return dict(await WorkspaceScope(work, workspace_id).contract(contract_id))


@probe.get("/confirmations/{confirmation_id}")
async def read_confirmation(
    confirmation_id: str, workspace_id: WorkspaceDependency, database: DatabaseDependency
) -> dict[str, object]:
    async with database.reading() as work:
        return dict(await WorkspaceScope(work, workspace_id).confirmation(confirmation_id))


@probe.get("/artifacts/{artifact_id}")
async def read_artifact(
    artifact_id: str, workspace_id: WorkspaceDependency, database: DatabaseDependency
) -> dict[str, object]:
    async with database.reading() as work:
        return dict(await WorkspaceScope(work, workspace_id).artifact(artifact_id))


@probe.get("/whoami")
async def whoami(workspace_id: WorkspaceDependency) -> dict[str, str]:
    return {"workspace_id": workspace_id}


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(environ=ENV, database_path=tmp_path / "harness.sqlite3")
    application.include_router(probe)
    async with application.router.lifespan_context(application):
        yield application


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://harness.test"
    )


async def _workspace_of(visitor: httpx.AsyncClient) -> str:
    """Drive one request so the middleware issues this client its workspace."""
    response = await visitor.get("/api/v1/_probe/whoami")
    assert response.status_code == 200
    return str(response.json()["workspace_id"])


async def _seed_owned_resources(app: FastAPI, workspace_id: str) -> dict[str, str]:
    """One row of each workspace-owned kind, plus a global contract template."""
    database: Database = app.state.database
    ids = {
        "run": "run_owned",
        "contract": "con_owned",
        "confirmation": "cnf_owned",
        "artifact": "art_owned",
        "template": "con_template",
    }
    async with database.transaction() as work:
        await work.execute(
            """
            INSERT INTO runs (
                id, workspace_id, target_id, target_adapter_id,
                implementation_version, status, started_at
            ) VALUES (?, ?, 'buggy_store', 'buggy_store', '0.1.0', 'armed', ?)
            """,
            (ids["run"], workspace_id, work.now()),
        )
        for contract_id, owner in ((ids["contract"], workspace_id), (ids["template"], None)):
            await work.execute(
                """
                INSERT INTO contracts (
                    id, workspace_id, content_hash, name, schema_version,
                    document_json, created_at
                ) VALUES (?, ?, 'sha256:' || ?, 'a contract', '1.0.0', '{}', ?)
                """,
                (contract_id, owner, "0" * 64, work.now()),
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
            ) VALUES (?, ?, ?, 'outcome_report', '1.0.0', 'sha256:x', '{}', 'a.json', 12, ?)
            """,
            (ids["artifact"], workspace_id, ids["run"], work.now()),
        )
    return ids


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("run", "runs"),
        ("contract", "contracts"),
        ("confirmation", "confirmations"),
        ("artifact", "artifacts"),
    ],
)
async def test_a_second_client_cannot_read_the_first_ones_resource(
    app: FastAPI, kind: str, path: str
) -> None:
    """The exit gate, one resource kind at a time.

    The intruder is *given* the identifier — the point is that knowing it
    changes nothing.
    """
    # Arrange
    async with client(app) as owner, client(app) as intruder:
        owner_workspace = await _workspace_of(owner)
        await _workspace_of(intruder)
        ids = await _seed_owned_resources(app, owner_workspace)
        resource_id = ids[kind]

        # Act
        as_owner = await owner.get(f"/api/v1/_probe/{path}/{resource_id}")
        as_intruder = await intruder.get(f"/api/v1/_probe/{path}/{resource_id}")

    # Assert
    assert as_owner.status_code == 200
    assert as_owner.json()["id"] == resource_id
    assert as_intruder.status_code == 404
    assert as_intruder.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.parametrize("path", ["runs", "contracts", "confirmations", "artifacts"])
async def test_someone_elses_resource_is_indistinguishable_from_a_missing_one(
    app: FastAPI, path: str
) -> None:
    """A 403 would confirm the identifier names something real.

    The two responses must be byte-identical, message included: a wording
    difference is a working oracle, and that is how this leak usually survives
    review.
    """
    # Arrange
    async with client(app) as owner, client(app) as intruder:
        owner_workspace = await _workspace_of(owner)
        await _workspace_of(intruder)
        ids = await _seed_owned_resources(app, owner_workspace)
        real = {
            "runs": ids["run"],
            "contracts": ids["contract"],
            "confirmations": ids["confirmation"],
            "artifacts": ids["artifact"],
        }[path]

        # Act
        someone_elses = await intruder.get(f"/api/v1/_probe/{path}/{real}")
        never_existed = await intruder.get(f"/api/v1/_probe/{path}/nothing_by_this_name")

    # Assert
    assert someone_elses.status_code == never_existed.status_code == 404
    assert someone_elses.json() == never_existed.json()


async def test_a_global_contract_template_is_readable_by_any_workspace(
    app: FastAPI,
) -> None:
    """The one deliberate exception: a template is owned by nobody (§17.1, FR-009)."""
    # Arrange
    async with client(app) as owner, client(app) as other:
        owner_workspace = await _workspace_of(owner)
        await _workspace_of(other)
        ids = await _seed_owned_resources(app, owner_workspace)

        # Act
        response = await other.get(f"/api/v1/_probe/contracts/{ids['template']}")

    # Assert
    assert response.status_code == 200
    assert response.json()["workspace_id"] is None


async def test_replaying_another_clients_cookie_is_the_only_way_in(app: FastAPI) -> None:
    """States the boundary honestly: the cookie *is* the credential.

    A stolen cookie is a stolen session — that is what `HttpOnly` and
    `SameSite=Strict` are defending. What FR-006 promises is narrower and is
    what the other tests check: knowing an *identifier* grants nothing.
    """
    # Arrange
    async with client(app) as owner, client(app) as thief:
        owner_workspace = await _workspace_of(owner)
        ids = await _seed_owned_resources(app, owner_workspace)
        stolen = owner.cookies.get(WORKSPACE_COOKIE_NAME)

        # Act
        thief.cookies.set(WORKSPACE_COOKIE_NAME, str(stolen), domain="harness.test")
        response = await thief.get(f"/api/v1/_probe/runs/{ids['run']}")

    # Assert
    assert response.status_code == 200


async def test_the_refusal_envelope_leaks_no_internal_detail(app: FastAPI) -> None:
    """§15.8 / §20: no table name, no SQL, no traceback."""
    # Arrange
    async with client(app) as intruder:
        await _workspace_of(intruder)

        # Act
        response = await intruder.get("/api/v1/_probe/runs/run_nothing")

    # Assert
    body = response.json()
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert body["error"]["retryable"] is False
    assert "SELECT" not in body["error"]["message"]
    assert "runs" not in body["error"]["message"]


async def test_the_scope_refuses_a_table_it_was_not_built_for() -> None:
    """A table name is not bindable, so it must never be caller-supplied."""
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="not a workspace-scoped table"):
        await WorkspaceScope(None, "ws_a")._optional("sqlite_master; --", "x")
