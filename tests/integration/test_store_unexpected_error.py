"""Nothing escapes the store's boundary as a bare 500 (§15.5, §15.8).

`buggy_store.api`'s module docstring has always made two claims: every failure
leaves through one envelope, and "nothing else is allowed to escape a handler".
The first was structural — `StoreError` has a registered handler. The second was
a promise, and a promise is not a boundary: an exception the store never thought
to raise deliberately left as FastAPI's default 500 body, which is a second wire
shape beside §15.8's envelope and may carry whatever text the exception held.

That mattered concretely. `duplicate_on_retry` computed a doubled quantity the
canonical model refuses to hold, and the resulting `ValidationError` reached the
client as exactly such a 500 — see `test_duplicate_on_retry_ceiling.py` for the
arithmetic. The clamp fixes that one path; this file holds the boundary itself,
so the next unanticipated failure is a stable envelope rather than a new bug.

The fault is injected by substituting the service `create_app` already accepts,
so the exception escapes a real route through the real application stack. A test
that called the handler directly would prove only that the function returns a
dict.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from buggy_store.api import API_PREFIX, WORKSPACE_HEADER, create_app
from buggy_store.repository import StoreRepository
from buggy_store.service import MutationOutcome, StoreService

pytestmark = pytest.mark.integration

MUG = "mug-ceramic-001"
REQUEST_ID = "req-unexpected"

#: Deliberately full of things §15.8 forbids reaching a browser tool: a driver
#: name, an absolute server path, and a credential-shaped value. Every one of
#: them is asserted absent from the response below.
INTERNAL_MESSAGE = "sqlite3 connection to /var/lib/buggy-store/store.sqlite3 refused (token=abc123)"

LEAKS = (
    "sqlite3",
    "/var/lib",
    "RuntimeError",
    "Traceback",
    "abc123",
    "store.sqlite3",
)


class ExplodingService(StoreService):
    """A store whose cart mutation fails in a way nobody classified.

    `RuntimeError` rather than a `StoreError` subclass on purpose: the point of
    the boundary is what happens to a failure the store did *not* decide to
    refuse.
    """

    async def update_cart(
        self, workspace_id: str, product_id: str, quantity: int, request_id: str
    ) -> MutationOutcome:
        raise RuntimeError(INTERNAL_MESSAGE)


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        # `raise_app_exceptions=False` is what a real server does: Starlette's
        # `ServerErrorMiddleware` sends the response and *then* re-raises so the
        # process logs the failure. Only the in-process transport surfaces that
        # re-raise to the caller, and this file is about the response.
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),  # type: ignore[arg-type]
        base_url="http://buggy-store.test",
        headers={WORKSPACE_HEADER: "ws-1"},
    )


@pytest.fixture
async def exploding(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    database = tmp_path / "store.sqlite3"
    app = create_app(database_path=database, service=ExplodingService(StoreRepository(database)))
    async with app.router.lifespan_context(app), _client(app) as http:
        yield http


@pytest.fixture
async def honest(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(database_path=tmp_path / "store.sqlite3")
    async with app.router.lifespan_context(app), _client(app) as http:
        yield http


async def _mutate(client: httpx.AsyncClient, product_id: str = MUG) -> httpx.Response:
    return await client.post(
        f"{API_PREFIX}/store/cart/mutations",
        json={"product_id": product_id, "quantity": 1, "request_id": REQUEST_ID},
    )


# --- the envelope -----------------------------------------------------------


async def test_an_unanticipated_failure_returns_the_store_envelope(
    exploding: httpx.AsyncClient,
) -> None:
    """§15.8's one wire shape, with the store's own terminal code."""
    # Arrange / Act
    response = await _mutate(exploding)

    # Assert
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "STORE_ERROR"
    assert error["retryable"] is False
    assert error["details"] == {}


async def test_an_unanticipated_failure_leaks_nothing_about_its_cause(
    exploding: httpx.AsyncClient,
) -> None:
    """§15.8: no traceback, no exception text, no class name, no file path.

    Asserted over the whole serialized body rather than the message alone, so a
    later handler that helpfully tucked the cause into `details` would fail here
    instead of shipping.
    """
    # Arrange / Act
    response = await _mutate(exploding)

    # Assert
    body = json.dumps(response.json())
    for leak in LEAKS:
        assert leak not in body, f"{leak!r} reached the client"


async def test_an_unanticipated_failure_is_logged_rather_than_swallowed(
    exploding: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The detail belongs in the server log, which is not the response.

    An envelope that discarded the cause entirely would trade one failure mode
    for another: a store nobody can debug. Errors are handled explicitly at
    every layer, never silently swallowed.
    """
    # Arrange
    caplog.set_level(logging.ERROR, logger="buggy_store.unhandled")

    # Act
    await _mutate(exploding)

    # Assert
    records = [record for record in caplog.records if record.name == "buggy_store.unhandled"]
    assert len(records) == 1
    assert records[0].exc_info is not None


# --- the catch-all did not flatten the deliberate refusals -------------------


async def test_a_deliberate_refusal_keeps_its_own_code(honest: httpx.AsyncClient) -> None:
    """The specific handler still wins.

    Starlette matches the most specific registered handler first, but "still"
    is the kind of property that quietly stops holding, and a boundary that
    reported every refusal as `STORE_ERROR` would take the adapter's branches
    with it.
    """
    # Arrange / Act
    response = await _mutate(honest, product_id="not-a-seeded-product")

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PRODUCT_NOT_FOUND"


async def test_a_valid_request_is_untouched_by_the_boundary(honest: httpx.AsyncClient) -> None:
    """The empty case for a catch-all: a handler that caught too much would
    turn ordinary successes into 500s, and nothing else here would notice."""
    # Arrange / Act
    response = await _mutate(honest)

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"


async def test_a_classified_and_an_unclassified_failure_share_one_envelope(
    exploding: httpx.AsyncClient, honest: httpx.AsyncClient
) -> None:
    """One shape means one client parser. A second shape is a second branch,
    and the branch written first silently mishandles the other.

    Both halves are asserted in one test because the property is a comparison:
    neither response is interesting on its own.
    """
    # Arrange / Act
    unclassified = await _mutate(exploding)
    classified = await _mutate(honest, product_id="not-a-seeded-product")

    # Assert
    for response in (unclassified, classified):
        assert set(response.json()) == {"error"}
        assert set(response.json()["error"]) == {"code", "message", "retryable", "details"}
