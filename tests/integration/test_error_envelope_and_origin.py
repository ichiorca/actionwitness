"""004-T7 — `Origin` validation and the single §15.8 error envelope.

§20.1 requires validating `Origin` on mutating API requests; §15.8 fixes one
envelope for every refusal; §16 requires an invalid non-reset transition to be a
409; §20 forbids an internal detail reaching a client.

The near-miss origins matter more than the obvious ones. A rule written as
"starts with the configured host" accepts `https://harness.test.evil.example`,
and a rule written as "ends with the configured host" accepts
`https://evilharness.test`. Both are in the table below.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.journeys.enums import RunState
from actionwitness_core.journeys.transitions import validate_run_transition
from actionwitness_core.kernel import ContractError, CoreErrorCode, ErrorDetail
from actionwitness_service.api.app import create_app
from actionwitness_service.api.dependencies import WorkspaceDependency
from fastapi import APIRouter, FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LOCAL_ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}
CONFIGURED_ENV = {
    "HARNESS_ENV": "production",
    "HARNESS_PUBLIC_ORIGIN": "https://harness.test",
    "BUGGY_STORE_ENABLED": "false",
}

probe = APIRouter(prefix="/api/v1/_probe")


@probe.post("/mutate")
async def mutate(workspace_id: WorkspaceDependency) -> dict[str, str]:
    return {"workspace_id": workspace_id}


@probe.get("/read")
async def read(workspace_id: WorkspaceDependency) -> dict[str, str]:
    return {"workspace_id": workspace_id}


@probe.post("/invalid-transition")
async def invalid_transition(workspace_id: WorkspaceDependency) -> dict[str, str]:
    """§16: a run that already passed cannot start running again."""
    validate_run_transition(RunState.PASSED, RunState.RUNNING)
    return {"unreachable": workspace_id}


@probe.post("/rejected-contract")
async def rejected_contract(workspace_id: WorkspaceDependency) -> dict[str, str]:
    raise ContractError(
        "the contract names an unknown operator",
        code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
        details=(ErrorDetail(location="assertions[0].operator", message="unknown operator"),),
    )


@probe.post("/boom")
async def boom(workspace_id: WorkspaceDependency) -> dict[str, str]:
    """An unanticipated failure whose message names something internal."""
    raise RuntimeError("connection to /var/lib/actionwitness/harness.sqlite3 refused")


def _app(tmp_path: Path, environ: dict[str, str]) -> FastAPI:
    application = create_app(environ=environ, database_path=tmp_path / "harness.sqlite3")
    application.include_router(probe)
    return application


@pytest.fixture
async def local_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = _app(tmp_path, LOCAL_ENV)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def configured_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = _app(tmp_path, CONFIGURED_ENV)
    async with application.router.lifespan_context(application):
        yield application


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        # `raise_app_exceptions=False` is what a real server does: Starlette's
        # `ServerErrorMiddleware` sends the 500 envelope and *then* re-raises so
        # the process logs the failure. Only the in-process transport surfaces
        # that re-raise to the caller, and these tests are about the response.
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


# --- Origin validation ------------------------------------------------------


async def test_a_mutation_from_the_configured_origin_is_allowed(
    configured_app: FastAPI,
) -> None:
    # Arrange / Act
    async with client(configured_app) as visitor:
        response = await visitor.post(
            "/api/v1/_probe/mutate", headers={"Origin": "https://harness.test"}
        )

    # Assert
    assert response.status_code == 200


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("https://harness.test.evil.example", id="suffix-appended"),
        pytest.param("https://evilharness.test", id="prefix-prepended"),
        pytest.param("http://harness.test", id="scheme-downgraded"),
        pytest.param("https://harness.test:8443", id="port-differs"),
        pytest.param("null", id="opaque-origin"),
        pytest.param("*", id="wildcard"),
        pytest.param("", id="empty"),
        # `urlsplit` parses lazily: these three split without complaint and only
        # raise when the port is *read*. An `Origin` header is attacker-supplied,
        # so a value that cannot be parsed has to arrive at the same refusal as
        # `null` and `*` rather than at a traceback.
        pytest.param("https://harness.test:99999", id="port-out-of-range"),
        pytest.param("https://harness.test:notaport", id="port-not-numeric"),
        pytest.param("https://[not:a:v6:addr/", id="malformed-ipv6-authority"),
    ],
)
async def test_a_mutation_from_a_near_miss_origin_is_refused(
    configured_app: FastAPI, origin: str
) -> None:
    """Equality after normalization — never prefix, suffix, or host containment."""
    # Arrange / Act
    async with client(configured_app) as visitor:
        response = await visitor.post("/api/v1/_probe/mutate", headers={"Origin": origin})

    # Assert
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"


async def test_a_refused_origin_never_creates_a_workspace(
    configured_app: FastAPI,
) -> None:
    """Otherwise a hostile page could fill the table by being rejected repeatedly."""
    # Arrange
    database = configured_app.state.database

    # Act
    async with client(configured_app) as visitor:
        for _ in range(3):
            await visitor.post("/api/v1/_probe/mutate", headers={"Origin": "https://evil.example"})

    # Assert
    async with database.reading() as work:
        assert await work.fetch_all("SELECT id FROM workspaces") == []


async def test_a_read_is_not_origin_checked(configured_app: FastAPI) -> None:
    """§20.1 says "mutating API requests"; a GET that changed state is the bug."""
    # Arrange / Act
    async with client(configured_app) as visitor:
        response = await visitor.get(
            "/api/v1/_probe/read", headers={"Origin": "https://evil.example"}
        )

    # Assert
    assert response.status_code == 200


async def test_a_mutation_with_no_origin_header_is_allowed(
    configured_app: FastAPI,
) -> None:
    """Browsers always send `Origin` on a mutation, so absence means the request
    did not come from a page. Refusing it would break the documented CLI without
    closing anything — an attacker who can set headers can also omit one."""
    # Arrange / Act
    async with client(configured_app) as visitor:
        response = await visitor.post("/api/v1/_probe/mutate")

    # Assert
    assert response.status_code == 200


async def test_without_a_configured_origin_the_requests_own_origin_is_used(
    local_app: FastAPI,
) -> None:
    """The documented local case: served same-origin, so a legitimate page's
    `Origin` equals what it is posting to."""
    # Arrange / Act
    async with client(local_app) as visitor:
        same_origin = await visitor.post(
            "/api/v1/_probe/mutate", headers={"Origin": "https://harness.test"}
        )
        cross_origin = await visitor.post(
            "/api/v1/_probe/mutate", headers={"Origin": "https://evil.example"}
        )

    # Assert
    assert same_origin.status_code == 200
    assert cross_origin.status_code == 403


# --- the §15.8 envelope -----------------------------------------------------


async def test_an_invalid_transition_is_409_with_the_envelope(local_app: FastAPI) -> None:
    """§16: "invalid non-reset state transitions shall return HTTP 409"."""
    # Arrange / Act
    async with client(local_app) as visitor:
        response = await visitor.post("/api/v1/_probe/invalid-transition")

    # Assert
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "RUN_IN_PROGRESS"
    assert body["retryable"] is False


async def test_a_core_rejection_keeps_its_code_and_gains_a_status(
    local_app: FastAPI,
) -> None:
    """The core carries no HTTP status — it has to install alone — so this seam
    is where a domain failure acquires one."""
    # Arrange / Act
    async with client(local_app) as visitor:
        response = await visitor.post("/api/v1/_probe/rejected-contract")

    # Assert
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "CONTRACT_VALIDATION_FAILED"
    # §15.8's `details` name the offending field, so a caller can fix every
    # problem in one round trip rather than one per response.
    assert body["details"] == [{"path": "assertions[0].operator", "message": "unknown operator"}]


async def test_an_unhandled_failure_leaks_nothing(local_app: FastAPI) -> None:
    """§20: no traceback, no exception text, no class name, no file path."""
    # Arrange / Act
    async with client(local_app) as visitor:
        response = await visitor.post("/api/v1/_probe/boom")

    # Assert
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "HARNESS_ERROR"
    assert body["retryable"] is False
    message = body["message"]
    for leak in ("sqlite3", "/var/lib", "RuntimeError", "Traceback", "connection"):
        assert leak not in message


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/_probe/invalid-transition",
        "/api/v1/_probe/rejected-contract",
        "/api/v1/_probe/boom",
    ],
)
async def test_every_refusal_uses_the_same_envelope_shape(local_app: FastAPI, path: str) -> None:
    """§15.8 fixes one wire shape; a second one is a second client branch."""
    # Arrange / Act
    async with client(local_app) as visitor:
        response = await visitor.post(path)

    # Assert
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert isinstance(body["error"]["details"], list)
