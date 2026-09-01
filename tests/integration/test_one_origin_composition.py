"""009-T3 — the harness, its API, and the demo target behind one origin.

Spec §29.1: "One-origin event routing may expose the harness at `/`, the harness
API at `/api/v1`, and Buggy Store at `/demo` plus `/demo/api/v1`; process
co-location shall not bypass the versioned target API or adapter boundary."

The last clause is the one worth testing, and it does not test itself. Co-locating
two applications in one container makes it *easier*, not harder, to reach past
the boundary: an `import buggy_store` in the service would work, would be faster,
and would silently turn the independent observation into a function call against
the same process that produced the tool response. So the assertions below are
about what crosses the seam — that the storefront's request reaches the store as
an HTTP request on the store's own versioned path, carrying its own workspace
identity and nothing of the harness's.

`ASGITransport` stands in for the loopback socket the container uses. What it
faithfully preserves is the thing under test: request/response shape, headers,
and the fact that the only interface between the two applications is the HTTP
one. Recorded through a wrapping transport, because a proxy that quietly added or
dropped a header would pass a test that only compared response bodies.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import create_app
from actionwitness_service.api.composition import MAX_PROXIED_BODY_BYTES
from buggy_store.api import create_app as create_store_app
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

STORE_ORIGIN = "http://127.0.0.1:8001"
HARNESS_ORIGIN = "https://harness.test"
WORKSPACE = "store-workspace-1"


class RecordingTransport(httpx.AsyncBaseTransport):
    """Records every request the harness process sent to the store process."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)


def _static_tree(root: Path) -> Path:
    """A stand-in for what the Dockerfile's frontend stage copies in.

    The two applications get separate directories with separate contents (§29.1
    step 4), so a test that found the wrong file would fail rather than pass on a
    coincidence.
    """
    (root / "harness" / "assets").mkdir(parents=True)
    (root / "demo" / "assets").mkdir(parents=True)
    (root / "harness" / "index.html").write_text("<title>harness</title>", encoding="utf-8")
    (root / "harness" / "assets" / "app.js").write_text("// harness", encoding="utf-8")
    (root / "demo" / "index.html").write_text("<title>storefront</title>", encoding="utf-8")
    (root / "demo" / "assets" / "store.js").write_text("// storefront", encoding="utf-8")
    return root


@pytest.fixture
async def composed(tmp_path: Path) -> AsyncIterator[tuple[FastAPI, RecordingTransport]]:
    """The composed deployment: two applications, one origin, one seam."""
    store = create_store_app(database_path=tmp_path / "store.sqlite3")
    async with store.router.lifespan_context(store):
        recorder = RecordingTransport(httpx.ASGITransport(app=store))
        async with httpx.AsyncClient(transport=recorder, base_url=STORE_ORIGIN) as to_store:
            harness = create_app(
                environ={
                    "HARNESS_ENV": "local",
                    "HARNESS_STATIC_ROOT": str(_static_tree(tmp_path / "static")),
                },
                database_path=tmp_path / "harness.sqlite3",
                target_client=to_store,
            )
            async with harness.router.lifespan_context(harness):
                yield harness, recorder


def visitor(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=HARNESS_ORIGIN,
    )


# --- §29.1's four mount points ----------------------------------------------


async def test_each_application_is_served_from_its_own_static_directory(
    composed: tuple[FastAPI, RecordingTransport],
) -> None:
    """`/` is the harness and `/demo` is the storefront — never each other."""
    app, _ = composed

    async with visitor(app) as client:
        harness_page = await client.get("/")
        storefront_page = await client.get("/demo")
        harness_asset = await client.get("/assets/app.js")
        storefront_asset = await client.get("/demo/assets/store.js")

    assert harness_page.status_code == 200
    assert "harness" in harness_page.text
    assert storefront_page.status_code == 200
    assert "storefront" in storefront_page.text
    assert harness_asset.text == "// harness"
    assert storefront_asset.text == "// storefront"


async def test_the_harness_api_is_not_shadowed_by_an_asset_mount(
    composed: tuple[FastAPI, RecordingTransport],
) -> None:
    """A static mount registered over `/api/v1` would take the whole API down.

    The composition is registered after the routers for exactly this reason, and
    "we registered them in the right order" is not a claim a reader can check
    without this.
    """
    app, _ = composed

    async with visitor(app) as client:
        response = await client.get("/api/v1/workspace")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


# --- the seam ---------------------------------------------------------------


async def test_a_storefront_call_reaches_the_store_over_its_versioned_http_api(
    composed: tuple[FastAPI, RecordingTransport],
) -> None:
    """§25.11: co-location must not bypass the versioned target API."""
    app, recorder = composed

    async with visitor(app) as client:
        response = await client.get(
            "/demo/api/v1/store/state", headers={"X-Workspace-Id": WORKSPACE}
        )

    assert response.status_code == 200
    assert "cart" in response.json()["target_state"]

    # Act's real subject: what crossed the seam.
    assert len(recorder.requests) == 1
    forwarded = recorder.requests[0]
    assert forwarded.url.path == "/demo/api/v1/store/state", (
        "the store must be addressed on its own versioned path, unrewritten"
    )
    assert forwarded.headers["x-workspace-id"] == WORKSPACE


async def test_the_proxy_passes_the_workspace_header_through_untouched(
    composed: tuple[FastAPI, RecordingTransport],
) -> None:
    """The storefront's isolation identity is its own (§15.5).

    A proxy that substituted the harness workspace would merge two isolation
    scopes that the specification keeps separate — and it would do it silently,
    because both are opaque strings and the response would look correct.
    """
    app, recorder = composed

    async with visitor(app) as client:
        first = await client.post(
            "/demo/api/v1/store/cart/mutations",
            headers={"X-Workspace-Id": "shopper-a", "Origin": HARNESS_ORIGIN},
            json={"product_id": "mug-ceramic-001", "quantity": 2, "request_id": "req-0001"},
        )
        mutated = await client.get(
            "/demo/api/v1/store/state", headers={"X-Workspace-Id": "shopper-a"}
        )
        other = await client.get(
            "/demo/api/v1/store/state", headers={"X-Workspace-Id": "shopper-b"}
        )

    assert first.status_code == 200
    assert [request.headers["x-workspace-id"] for request in recorder.requests] == [
        "shopper-a",
        "shopper-a",
        "shopper-b",
    ]
    # Two identities in, two carts out: the header decided, not the connection.
    # Both halves are asserted — "b's cart is empty" proves nothing on its own if
    # the mutation never landed anywhere.
    assert mutated.json()["target_state"]["cart"]["items"] != {}
    assert other.json()["target_state"]["cart"]["items"] == {}


async def test_the_harness_workspace_cookie_is_never_forwarded_to_the_store(
    composed: tuple[FastAPI, RecordingTransport],
) -> None:
    """The cookie is ambient authority for `/api/v1` and nothing else (§20.1)."""
    app, recorder = composed

    async with visitor(app) as client:
        await client.get("/api/v1/workspace")  # mints the harness cookie
        assert client.cookies, "the harness must have issued a workspace cookie"
        await client.get("/demo/api/v1/store/state", headers={"X-Workspace-Id": WORKSPACE})

    forwarded = recorder.requests[-1]
    assert "cookie" not in {name.lower() for name in forwarded.headers}


async def test_the_storefront_path_mints_no_harness_workspace(
    composed: tuple[FastAPI, RecordingTransport],
) -> None:
    """A visitor who only ever opens the storefront is not a harness user.

    Minting one would attach a `Secure; HttpOnly` credential for `/api/v1` to a
    page required to work with no harness at all (AC-09), and would fill the
    workspace table with rows nobody created.
    """
    app, _ = composed

    async with visitor(app) as client:
        await client.get("/demo")
        await client.get("/demo/api/v1/store/state", headers={"X-Workspace-Id": WORKSPACE})
        assert dict(client.cookies) == {}


# --- refusals ---------------------------------------------------------------


async def test_an_unreachable_store_is_a_named_refusal_not_a_pass(
    tmp_path: Path,
) -> None:
    """Constitution §5: a failed observation produces an explicit non-pass.

    The store process can die while the harness stays up — that is the whole
    reason they are separate processes — so this path is reachable in production
    and must not degrade into anything a client could mistake for success.
    """

    class DeadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=DeadTransport(), base_url=STORE_ORIGIN) as to_store:
        app = create_app(
            environ={"HARNESS_ENV": "local"},
            database_path=tmp_path / "harness.sqlite3",
            target_client=to_store,
        )
        async with app.router.lifespan_context(app), visitor(app) as client:
            response = await client.get(
                "/demo/api/v1/store/state", headers={"X-Workspace-Id": WORKSPACE}
            )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TARGET_UNAVAILABLE"
    assert response.json()["error"]["retryable"] is False, (
        "a proxied mutation that failed in transit has an ambiguous outcome (§20.2)"
    )


async def test_a_disabled_demo_target_answers_with_its_module_state(
    tmp_path: Path,
) -> None:
    """Not a 404: the path exists, the module is off (§21.1)."""
    app = create_app(
        environ={"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"},
        database_path=tmp_path / "harness.sqlite3",
    )
    async with app.router.lifespan_context(app), visitor(app) as client:
        response = await client.get(
            "/demo/api/v1/store/state", headers={"X-Workspace-Id": WORKSPACE}
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TARGET_UNAVAILABLE"


async def test_an_oversized_body_is_refused_before_the_store_sees_it(
    composed: tuple[FastAPI, RecordingTransport],
) -> None:
    """§20.2 bounds frontend-submitted payloads; the proxy buffers, so it must too."""
    app, recorder = composed
    oversized = {"product_id": "x" * (MAX_PROXIED_BODY_BYTES + 1), "quantity": 1}

    async with visitor(app) as client:
        response = await client.post(
            "/demo/api/v1/store/cart/mutations",
            headers={"X-Workspace-Id": WORKSPACE, "Origin": HARNESS_ORIGIN},
            content=json.dumps(oversized),
        )

    assert response.status_code == 422
    assert recorder.requests == [], "the oversized body must never reach the store"


async def test_a_storefront_mutation_from_another_origin_is_still_refused(
    composed: tuple[FastAPI, RecordingTransport],
) -> None:
    """`/demo` is on the harness origin, so it inherits the harness origin policy.

    Worth asserting rather than assuming: the storefront takes no harness cookie,
    and it would be easy to conclude from that it needs no origin check either.
    The store holds real state, and §20.1's rule is about mutating requests, not
    about which cookie they carry.
    """
    app, recorder = composed

    async with visitor(app) as client:
        response = await client.post(
            "/demo/api/v1/store/cart/mutations",
            headers={"X-Workspace-Id": WORKSPACE, "Origin": "https://harness.test.evil.example"},
            json={"product_id": "mug-ceramic-001", "quantity": 1, "request_id": "req-0002"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    assert recorder.requests == [], "a refused mutation must not reach the store"
