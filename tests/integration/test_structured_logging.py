"""009-T4 — structured request logs that carry identifiers and never payloads.

Spec §21.5 names both halves: a line carries request/run/eval IDs, event type,
tool name, status, duration, and classification; it never carries sensitive
payload values.

The second half is the one that needs a test, and it needs a test that does not
trust the implementation's own idea of what a payload is. So the tests below send
real traffic containing values a reader would recognise as leaked — a discount
code, a credential-shaped string, a filesystem path — and assert on the *emitted
log text*, not on the fields of the model that produced it. A future change that
added an `extra` dict, or logged `repr(request)` somewhere else entirely, would
fail here.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import create_app
from actionwitness_service.api.dependencies import WorkspaceDependency
from actionwitness_service.telemetry import (
    LOGGER_NAME,
    MAX_LOGGED_IDENTIFIER_CHARS,
    TRUNCATION_MARKER,
    UNHANDLED_ERROR_CODE,
    UNHANDLED_STATUS,
    UNMATCHED_ROUTE,
)
from fastapi import APIRouter, FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Declared at module scope, not inside the test. `from __future__ import
#: annotations` turns every annotation into a string and FastAPI resolves those
#: against *module* globals — a function-local `WorkspaceDependency` is invisible
#: there, and the parameter silently degrades into a query argument nobody sends,
#: so the probe answers 422 instead of raising. The store's own `api.py` carries
#: the same warning for the same reason.
probe = APIRouter(prefix="/api/v1/_probe")


@probe.get("/boom")
async def boom(workspace_id: WorkspaceDependency) -> dict[str, str]:
    """An unanticipated failure whose message names an internal path."""
    raise RuntimeError(f"connection to {LOCAL_PATH} refused")


HARNESS_ORIGIN = "https://harness.test"

#: Values planted in requests below. Each is the kind of thing that reaches a log
#: line by accident — a query value, a body value, a header value, a URL secret.
SECRET_DISCOUNT = "SAVE20SECRETCODE"
SECRET_TOKEN = "sk-live-not-a-real-credential-000"
LOCAL_PATH = "/var/lib/actionwitness/harness.sqlite3"


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(
        environ={"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"},
        database_path=tmp_path / "harness.sqlite3",
    )
    async with application.router.lifespan_context(application):
        yield application


def visitor(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=HARNESS_ORIGIN,
    )


def emitted(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Every structured line this request produced, parsed."""
    return [
        json.loads(record.getMessage()) for record in caplog.records if record.name == LOGGER_NAME
    ]


# --- what a line carries (§21.5) --------------------------------------------


async def test_a_request_emits_one_line_with_status_route_and_duration(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    # Act
    async with visitor(app) as client:
        await client.get("/api/v1/workspace")

    # Assert
    lines = emitted(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert line["event"] == "http_request"
    assert line["method"] == "GET"
    assert line["route"] == "/api/v1/workspace"
    assert line["status"] == 200
    assert isinstance(line["duration_ms"], int)
    assert line["duration_ms"] >= 0


async def test_the_line_names_the_workspace_it_acted_in(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """An identifier is what makes a production log useful; it is not a value."""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async with visitor(app) as client:
        response = await client.get("/api/v1/workspace")

    workspace_id = response.json()["workspace_id"]
    assert emitted(caplog)[0]["workspace_id"] == workspace_id


async def test_a_refusal_is_logged_with_its_stable_classification(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """§21.5's "classification". The code, never the message.

    Handlers build messages from specification text today. One that interpolated
    a submitted value tomorrow would put it in the log, so the message is not a
    field at all.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async with visitor(app) as client:
        response = await client.post(
            "/api/v1/contracts",
            headers={"Origin": "https://harness.test.evil.example"},
            json={"anything": SECRET_DISCOUNT},
        )

    assert response.status_code == 403
    line = emitted(caplog)[0]
    assert line["status"] == 403
    assert line["error_code"] == "ORIGIN_NOT_ALLOWED"


async def test_a_route_template_is_logged_rather_than_the_expanded_path(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """`/api/v1/runs/{run_id}`, not `/api/v1/runs/run-0001`.

    Two reasons, and the second is the load-bearing one: templates make lines
    groupable, and a template cannot contain a value by construction.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async with visitor(app) as client:
        await client.get("/api/v1/runs/run-does-not-exist")

    line = emitted(caplog)[0]
    assert "{run_id}" in str(line["route"])
    assert "run-does-not-exist" not in str(line["route"])
    # The identifier still reaches the log — in the field meant for it.
    assert line["run_id"] == "run-does-not-exist"


async def test_the_eval_case_and_tool_name_fields_read_the_routers_own_parameters(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """§21.5 names both, and both were silently empty until the names agreed.

    The failure this pins is not a crash: `eval_case_id` was read from a
    `case_id` parameter no route declares, and `tool_name` from a
    `request.state` attribute nothing assigns, so each logged `None` on exactly
    the requests that should have carried it. A missing field and a misspelled
    one look identical in the output, which is why the agreement needs a test
    rather than a reading.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async with visitor(app) as client:
        await client.get("/api/v1/evals/eval-does-not-exist")
        await client.post(
            "/api/v1/runs/run-does-not-exist/target-tools/get_cart:invoke", json={"arguments": {}}
        )

    by_route = {str(line["route"]): line for line in emitted(caplog)}

    eval_line = next(line for route, line in by_route.items() if "{eval_case_id}" in route)
    assert eval_line["eval_case_id"] == "eval-does-not-exist"

    invoke_line = next(line for route, line in by_route.items() if "{tool_name}" in route)
    assert invoke_line["tool_name"] == "get_cart"


async def test_an_oversized_identifier_is_bounded_before_it_reaches_the_line(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """`path_params` is populated when the pattern matches, not when it validates.

    A request refused with a 422 still emits a line, so the only thing standing
    between a caller and an arbitrarily long log field is this bound.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    oversized = "r" * (MAX_LOGGED_IDENTIFIER_CHARS * 3)

    async with visitor(app) as client:
        await client.get(f"/api/v1/runs/{oversized}")

    logged = str(emitted(caplog)[0]["run_id"])
    assert logged.endswith(TRUNCATION_MARKER)
    assert len(logged) == MAX_LOGGED_IDENTIFIER_CHARS + len(TRUNCATION_MARKER)
    assert oversized not in logged


# --- what a line must never carry (§21.5, §20.3) ----------------------------


@pytest.mark.parametrize(
    "name,send",
    [
        (
            "a body value",
            lambda client: client.post(
                "/api/v1/contracts",
                headers={"Origin": HARNESS_ORIGIN},
                json={"title": SECRET_DISCOUNT, "token": SECRET_TOKEN},
            ),
        ),
        (
            "a query value",
            lambda client: client.get(f"/api/v1/workspace?discount_code={SECRET_DISCOUNT}"),
        ),
        (
            "a header value",
            lambda client: client.get(
                "/api/v1/workspace", headers={"Authorization": f"Bearer {SECRET_TOKEN}"}
            ),
        ),
        (
            "an unmatched path",
            lambda client: client.get(f"/api/v1/no-such-route/{SECRET_DISCOUNT}"),
        ),
    ],
)
async def test_no_submitted_value_reaches_a_log_line(
    app: FastAPI, caplog: pytest.LogCaptureFixture, name: str, send: object
) -> None:
    """The whole point of §21.5's second half, checked against the emitted text.

    Asserted on the raw line rather than on `RequestLog`'s fields, so a leak
    introduced anywhere in the logging path fails here — including one that never
    goes through the model at all.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async with visitor(app) as client:
        await send(client)  # type: ignore[operator]

    raw = "\n".join(record.getMessage() for record in caplog.records if record.name == LOGGER_NAME)
    assert raw, f"{name}: nothing was logged, so this proves nothing"
    assert SECRET_DISCOUNT not in raw, f"{name} leaked a discount code into the log"
    assert SECRET_TOKEN not in raw, f"{name} leaked a credential into the log"


async def test_an_unmatched_path_is_reduced_to_a_sentinel(
    app: FastAPI, caplog: pytest.LogCaptureFixture
) -> None:
    """There is no template to reduce an unmatched path to, so none is logged.

    A matched route's literal segments come from the router and can carry nothing
    a client chose. An unmatched path is the opposite — every segment of it was
    chosen by the caller — so the honest answer is to log that it matched
    nothing. Raw paths for whoever is debugging 404s live in the platform's own
    access log, which has a different audience than this line.
    """
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)

    async with visitor(app) as client:
        response = await client.get(f"/api/v1/no-such-route/{SECRET_TOKEN}")

    assert response.status_code == 404
    assert emitted(caplog)[0]["route"] == UNMATCHED_ROUTE


async def test_an_unhandled_failure_logs_no_internal_detail_in_the_structured_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The traceback belongs on its own logger, not in the §21.5 line.

    §20 forbids an internal detail reaching a *client*; keeping it out of the
    structured line as well is what lets that line be shipped to a log service
    with a different audience than the process's stderr.
    """
    application = create_app(
        environ={"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"},
        database_path=tmp_path / "harness.sqlite3",
    )
    application.include_router(probe)

    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    async with application.router.lifespan_context(application), visitor(application) as client:
        response = await client.get("/api/v1/_probe/boom")

    assert response.status_code == 500
    line = emitted(caplog)[0]
    assert LOCAL_PATH not in json.dumps(line), "the structured line carried a filesystem path"

    # The logging layer states the status and code for a crash rather than reading
    # them off a response it never sees. That is only safe while they agree with
    # what the handler actually sends, which is what this pins.
    assert line["status"] == UNHANDLED_STATUS == response.status_code
    assert line["error_code"] == UNHANDLED_ERROR_CODE == response.json()["error"]["code"]
