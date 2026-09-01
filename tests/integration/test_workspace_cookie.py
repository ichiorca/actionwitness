"""004-T5 — the anonymous workspace cookie (FR-005, §20.1).

Asserted through the real application over ASGI, because the interesting parts
are cookie *attributes* — and an attribute is exactly the kind of thing a unit
test on the store would never see.

FR-005 permits omitting **only** `Secure` for documented local HTTP. So the
local-mode test does not merely check that `Secure` is absent; it checks that
`HttpOnly` and `SameSite=Strict` are still there, which is the half of the
sentence a "relax the cookie for local dev" change would quietly drop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import create_app
from actionwitness_service.application.workspaces import WORKSPACE_COOKIE_NAME
from actionwitness_service.persistence.database import Database
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

LOCAL_ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}
PRODUCTION_ENV = {"HARNESS_ENV": "production", "BUGGY_STORE_ENABLED": "false"}


class _SteppingClock:
    """A deterministic clock that advances one second per read.

    Injected rather than patched, so `last_seen_at` moving is a fact about the
    application's own clock seam (constitution §1) instead of a fact about how
    fast the test machine happens to be. Wall-clock resolution on Windows is
    coarse enough that two adjacent requests can share a timestamp.
    """

    def __init__(self) -> None:
        self._reads = 0

    def __call__(self) -> datetime:
        self._reads += 1
        return datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=self._reads)


@pytest.fixture
async def local_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = create_app(
        environ=LOCAL_ENV,
        database_path=tmp_path / "harness.sqlite3",
        clock=_SteppingClock(),
    )
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def production_app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = create_app(
        environ=PRODUCTION_ENV,
        database_path=tmp_path / "harness.sqlite3",
        clock=_SteppingClock(),
    )
    async with app.router.lifespan_context(app):
        yield app


def client(app: FastAPI) -> httpx.AsyncClient:
    """A fresh client — its own cookie jar, which is what makes it a fresh visitor."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://harness.test"
    )


def cookie_attributes(response: httpx.Response) -> SimpleCookie:
    jar = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    return jar


async def test_first_access_creates_a_workspace_and_issues_a_cookie(
    local_app: FastAPI,
) -> None:
    # Arrange / Act
    async with client(local_app) as visitor:
        response = await visitor.get("/api/v1/workspace")

    # Assert
    jar = cookie_attributes(response)
    assert WORKSPACE_COOKIE_NAME in jar
    assert jar[WORKSPACE_COOKIE_NAME].value.startswith("ws_")


async def test_the_cookie_is_http_only_and_same_site_strict_even_locally(
    local_app: FastAPI,
) -> None:
    """FR-005 permits omitting *only* `Secure`."""
    # Arrange / Act
    async with client(local_app) as visitor:
        response = await visitor.get("/api/v1/workspace")

    # Assert
    morsel = cookie_attributes(response)[WORKSPACE_COOKIE_NAME]
    assert morsel["httponly"]
    assert morsel["samesite"].lower() == "strict"
    assert not morsel["secure"]


async def test_the_cookie_is_secure_in_production(production_app: FastAPI) -> None:
    # Arrange / Act
    async with client(production_app) as visitor:
        response = await visitor.get("/api/v1/workspace")

    # Assert
    morsel = cookie_attributes(response)[WORKSPACE_COOKIE_NAME]
    assert morsel["secure"]
    assert morsel["httponly"]
    assert morsel["samesite"].lower() == "strict"


async def test_two_clients_receive_different_workspaces(local_app: FastAPI) -> None:
    """FR-005: "concurrent visitors cannot affect each other." Two clients, two jars."""
    # Arrange / Act
    async with client(local_app) as first, client(local_app) as second:
        first_response = await first.get("/api/v1/workspace")
        second_response = await second.get("/api/v1/workspace")

    # Assert
    first_id = cookie_attributes(first_response)[WORKSPACE_COOKIE_NAME].value
    second_id = cookie_attributes(second_response)[WORKSPACE_COOKIE_NAME].value
    assert first_id != second_id


async def test_a_returning_client_keeps_its_workspace_and_gets_no_new_cookie(
    local_app: FastAPI,
) -> None:
    """Re-issuing an unchanged cookie on every response is noise."""
    # Arrange
    async with client(local_app) as visitor:
        first = await visitor.get("/api/v1/workspace")
        issued = cookie_attributes(first)[WORKSPACE_COOKIE_NAME].value

        # Act — the client's jar replays the cookie.
        second = await visitor.get("/api/v1/workspace")

    # Assert
    assert visitor.cookies.get(WORKSPACE_COOKIE_NAME) == issued
    assert second.headers.get_list("set-cookie") == []


async def test_a_forged_cookie_is_not_adopted(local_app: FastAPI, tmp_path: Path) -> None:
    """A client that could name its own workspace could name somebody else's.

    The presented value is discarded and a server-issued one takes its place,
    so the cookie stays a bearer token for a name the server chose.
    """
    # Arrange
    chosen = "ws_chosen_by_the_client"

    # Act
    async with client(local_app) as visitor:
        visitor.cookies.set(WORKSPACE_COOKIE_NAME, chosen, domain="harness.test")
        response = await visitor.get("/api/v1/workspace")

    # Assert
    issued = cookie_attributes(response)[WORKSPACE_COOKIE_NAME].value
    assert issued != chosen
    assert issued.startswith("ws_")


async def test_a_stale_cookie_starts_a_new_workspace_rather_than_failing(
    local_app: FastAPI,
) -> None:
    """FR-009 cleans up stale workspaces; a returning visitor must not be stranded."""
    # Arrange
    async with client(local_app) as visitor:
        first = await visitor.get("/api/v1/workspace")
        stale = cookie_attributes(first)[WORKSPACE_COOKIE_NAME].value

        database: Database = local_app.state.database
        async with database.transaction() as work:
            await work.execute("DELETE FROM workspaces WHERE id = ?", (stale,))

        # Act
        second = await visitor.get("/api/v1/workspace")

    # Assert
    assert second.status_code < 500
    reissued = cookie_attributes(second)[WORKSPACE_COOKIE_NAME].value
    assert reissued != stale


async def test_last_seen_advances_on_every_access(local_app: FastAPI) -> None:
    """FR-009's staleness scan reads `last_seen_at`; a workspace in daily use
    must never look abandoned."""
    # Arrange
    database: Database = local_app.state.database
    async with client(local_app) as visitor:
        await visitor.get("/api/v1/workspace")
        workspace_id = visitor.cookies.get(WORKSPACE_COOKIE_NAME)
        async with database.reading() as work:
            first = await work.fetch_one(
                "SELECT last_seen_at FROM workspaces WHERE id = ?", (workspace_id,)
            )

        # Act — a later request. The injected clock has already moved on.
        await visitor.get("/api/v1/workspace")

    async with database.reading() as work:
        second = await work.fetch_one(
            "SELECT last_seen_at, created_at FROM workspaces WHERE id = ?", (workspace_id,)
        )

    # Assert
    assert second["last_seen_at"] > first["last_seen_at"]
    assert second["created_at"] < second["last_seen_at"]


async def test_the_health_check_never_creates_a_workspace(local_app: FastAPI) -> None:
    """A liveness probe would otherwise fill the table with rows no human visits."""
    # Arrange
    database: Database = local_app.state.database

    # Act
    async with client(local_app) as probe:
        for _ in range(3):
            response = await probe.get("/healthz")

    # Assert
    assert response.status_code == 200
    assert response.headers.get_list("set-cookie") == []
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id FROM workspaces")
    assert rows == []
