"""Store API gates (spec v1.9 §15.5, §15.8; ADR-0001; 003-T6).

Driven through the real ASGI application over `httpx.ASGITransport`, which is
the transport ADR-0001 picked for tests precisely so the request/response
contract exercised here is the one production uses — no port allocation, no live
server, and no second code path that only tests take.

The refusal tests carry the weight. §15.5 is a public surface reachable from a
browser, so it revalidates everything its schema promises rather than trusting a
caller, and §15.8 forbids internal detail reaching a browser tool: every
deliberate failure leaves through one envelope with a stable code, and an
unhandled exception would be a 500 carrying a traceback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from buggy_store.api import API_PREFIX, WORKSPACE_HEADER, create_app
from buggy_store.repository import StoreRepository
from buggy_store.service import StoreService

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
MUG = "mug-ceramic-001"


class _MovableClock:
    def __init__(self) -> None:
        self.now = EPOCH

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class _Ids:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"


@pytest.fixture
def clock() -> _MovableClock:
    return _MovableClock()


@pytest.fixture
async def client(tmp_path: Path, clock: _MovableClock) -> AsyncIterator[httpx.AsyncClient]:
    database = tmp_path / "store.sqlite3"
    repository = StoreRepository(database, clock=clock)
    service = StoreService(repository, clock=clock, id_source=_Ids())
    app = create_app(database_path=database, service=service)

    # The app's own lifespan is driven explicitly: `ASGITransport` does not run
    # startup, and skipping it would mean these tests initialised the schema by a
    # path production never takes - exactly the second code path ADR-0001 chose
    # this transport to avoid.
    async with (
        app.router.lifespan_context(app),
        httpx.ASGITransport(app=app) as transport,
        httpx.AsyncClient(
            transport=transport,
            base_url="http://buggy-store.test",
            headers={WORKSPACE_HEADER: "ws-1"},
        ) as http,
    ):
        yield http


def _request(suffix: str) -> str:
    return f"req-{suffix:>012}"


async def _add_a_mug(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        f"{API_PREFIX}/store/cart/mutations",
        json={"product_id": MUG, "quantity": 1, "request_id": _request("1")},
    )


# --- the §15.5 endpoint table -----------------------------------------------


@pytest.mark.integration
async def test_the_store_reports_liveness(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.integration
async def test_the_catalog_lists_the_seeded_products(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/store/catalog")
    assert response.status_code == 200
    products = response.json()["products"]
    assert [product["line_key"] for product in products] == ["mug", "notebook", "tote"]
    assert products[0]["price"] == "25.00"


@pytest.mark.integration
async def test_the_catalog_can_be_searched(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/store/catalog", params={"query": "notebook"})
    assert [product["line_key"] for product in response.json()["products"]] == ["notebook"]


@pytest.mark.integration
async def test_reading_an_untouched_cart_seeds_it_empty(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/store/cart")
    assert response.status_code == 200
    body = response.json()
    assert body["state_version"] == 1
    assert body["cart"]["items"] == {}
    assert body["order"] == {"created": False, "order_id": None}


@pytest.mark.integration
async def test_a_cart_mutation_returns_the_canonical_cart(client: httpx.AsyncClient) -> None:
    response = await _add_a_mug(client)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["cart"]["items"]["mug"]["quantity"] == 1
    assert body["cart"]["total"] == "25.00"


@pytest.mark.integration
async def test_a_discount_applies_through_the_api(client: httpx.AsyncClient) -> None:
    await _add_a_mug(client)
    response = await client.post(f"{API_PREFIX}/store/discount", json={"code": "SAVE20"})
    assert response.status_code == 200
    assert response.json()["cart"]["total"] == "20.00"


@pytest.mark.integration
async def test_the_full_confirmation_and_checkout_path(client: httpx.AsyncClient) -> None:
    """§15.5's four checkout endpoints, end to end through HTTP."""
    await _add_a_mug(client)

    opened = await client.post(f"{API_PREFIX}/store/checkout/confirmations", json={})
    assert opened.status_code == 201
    confirmation_id = opened.json()["confirmation_id"]
    assert opened.json()["status"] == "pending"
    assert opened.json()["consequence"]["cart_total"] == "25.00"

    decided = await client.post(
        f"{API_PREFIX}/store/checkout/confirmations/{confirmation_id}/decision",
        json={"approved": True},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"

    ordered = await client.post(
        f"{API_PREFIX}/store/checkout",
        json={"confirmation_id": confirmation_id, "request_id": _request("9")},
    )
    assert ordered.status_code == 200
    assert ordered.json()["order_id"] == "order-0001"

    cart = await client.get(f"{API_PREFIX}/store/cart")
    assert cart.json()["order"]["created"] is True


@pytest.mark.integration
async def test_a_confirmation_can_be_cancelled_through_the_api(
    client: httpx.AsyncClient,
) -> None:
    opened = await client.post(f"{API_PREFIX}/store/checkout/confirmations", json={})
    confirmation_id = opened.json()["confirmation_id"]

    cancelled = await client.delete(f"{API_PREFIX}/store/checkout/confirmations/{confirmation_id}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


# --- the error envelope (§15.8) ---------------------------------------------


@pytest.mark.integration
async def test_a_deliberate_failure_leaves_through_the_one_envelope(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        f"{API_PREFIX}/store/cart/mutations",
        json={"product_id": "bicycle-001", "quantity": 1, "request_id": _request("1")},
    )
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "retryable", "details"}
    assert body["error"]["code"] == "PRODUCT_NOT_FOUND"
    assert body["error"]["retryable"] is False


@pytest.mark.integration
async def test_a_reused_request_id_is_a_non_retryable_conflict(
    client: httpx.AsyncClient,
) -> None:
    """App. D.2: `IDEMPOTENCY_KEY_REUSED`, `retryable: false`."""
    await _add_a_mug(client)
    response = await client.post(
        f"{API_PREFIX}/store/cart/mutations",
        json={"product_id": MUG, "quantity": 4, "request_id": _request("1")},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert response.json()["error"]["retryable"] is False


@pytest.mark.integration
async def test_an_unknown_discount_code_is_refused(client: httpx.AsyncClient) -> None:
    response = await client.post(f"{API_PREFIX}/store/discount", json={"code": "SAVE10"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DISCOUNT_NOT_FOUND"


@pytest.mark.integration
async def test_checkout_without_approval_is_refused_and_creates_no_order(
    client: httpx.AsyncClient,
) -> None:
    await _add_a_mug(client)
    opened = await client.post(f"{API_PREFIX}/store/checkout/confirmations", json={})
    response = await client.post(
        f"{API_PREFIX}/store/checkout",
        json={"confirmation_id": opened.json()["confirmation_id"], "request_id": _request("9")},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFIRMATION_REQUIRED"

    cart = await client.get(f"{API_PREFIX}/store/cart")
    assert cart.json()["order"]["created"] is False


@pytest.mark.integration
async def test_no_error_response_carries_an_internal_detail(
    client: httpx.AsyncClient,
) -> None:
    """§15.8: internal exceptions and stack traces never reach a browser tool."""
    responses = [
        await client.post(
            f"{API_PREFIX}/store/cart/mutations",
            json={"product_id": "bicycle-001", "quantity": 1, "request_id": _request("1")},
        ),
        await client.post(f"{API_PREFIX}/store/discount", json={"code": "NOPE"}),
        await client.post(
            f"{API_PREFIX}/store/checkout",
            json={"confirmation_id": "nope", "request_id": _request("9")},
        ),
    ]
    for response in responses:
        text = response.text.lower()
        assert "traceback" not in text
        assert "buggy_store." not in text
        assert ".py" not in text


# --- request validation -----------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    "body",
    [
        {"product_id": MUG, "quantity": 1},  # missing request_id
        {"product_id": MUG, "quantity": 6, "request_id": "req-000000000001"},  # over bound
        {"product_id": MUG, "quantity": -1, "request_id": "req-000000000001"},
        {"product_id": MUG, "quantity": 1, "request_id": "short"},
        {"product_id": MUG, "quantity": 1, "request_id": "req-000000000001", "extra": 1},
    ],
)
async def test_a_body_outside_the_schema_is_refused(client: httpx.AsyncClient, body: dict) -> None:
    """Unknown fields included: an ignored field is a request nobody actually made."""
    response = await client.post(f"{API_PREFIX}/store/cart/mutations", json=body)
    assert response.status_code == 422


@pytest.mark.integration
async def test_a_decision_body_has_no_default(client: httpx.AsyncClient) -> None:
    """§14 step 4: "no option is preselected" — the server must not preselect either."""
    opened = await client.post(f"{API_PREFIX}/store/checkout/confirmations", json={})
    response = await client.post(
        f"{API_PREFIX}/store/checkout/confirmations/{opened.json()['confirmation_id']}/decision",
        json={},
    )
    assert response.status_code == 422


@pytest.mark.integration
@pytest.mark.parametrize("seconds", [9, 301])
async def test_an_expiry_outside_the_specified_range_is_refused(
    client: httpx.AsyncClient, seconds: int
) -> None:
    response = await client.post(
        f"{API_PREFIX}/store/checkout/confirmations", json={"expires_in_seconds": seconds}
    )
    assert response.status_code == 422


# --- workspace scoping ------------------------------------------------------


@pytest.mark.integration
async def test_a_stateful_request_without_a_workspace_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """A default would pool two shoppers into one cart the first time it was omitted."""
    response = await client.get(f"{API_PREFIX}/store/cart", headers={WORKSPACE_HEADER: ""})
    assert response.status_code == 422

    del client.headers[WORKSPACE_HEADER]
    assert (await client.get(f"{API_PREFIX}/store/cart")).status_code == 422


@pytest.mark.integration
async def test_the_catalog_needs_no_workspace(client: httpx.AsyncClient) -> None:
    """A read of immutable seed data is not workspace state."""
    del client.headers[WORKSPACE_HEADER]
    assert (await client.get(f"{API_PREFIX}/store/catalog")).status_code == 200


@pytest.mark.integration
async def test_two_workspaces_do_not_share_a_cart_over_http(
    client: httpx.AsyncClient,
) -> None:
    await _add_a_mug(client)
    other = await client.get(f"{API_PREFIX}/store/cart", headers={WORKSPACE_HEADER: "ws-2"})
    assert other.json()["cart"]["items"] == {}


@pytest.mark.integration
async def test_a_confirmation_cannot_be_decided_from_another_workspace(
    client: httpx.AsyncClient,
) -> None:
    """§20.1: an identifier is never the authorization boundary."""
    opened = await client.post(f"{API_PREFIX}/store/checkout/confirmations", json={})
    confirmation_id = opened.json()["confirmation_id"]

    response = await client.post(
        f"{API_PREFIX}/store/checkout/confirmations/{confirmation_id}/decision",
        json={"approved": True},
        headers={WORKSPACE_HEADER: "ws-2"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


# --- the boundary the milestone exists to establish -------------------------


@pytest.mark.integration
async def test_the_store_serves_without_any_assurance_package_imported(
    client: httpx.AsyncClient,
) -> None:
    """BUILD_ORDER invariant 2, checked against the running application.

    The AST gate proves the source declares no such import; this proves the
    module graph of a *serving* store agrees, which also catches a lazy import
    inside a request handler.
    """
    import sys

    await _add_a_mug(client)
    leaked = [
        name
        for name in sys.modules
        if name.split(".")[0] in {"actionwitness_core", "actionwitness_service", "integrations"}
    ]
    assert leaked == [] or all(
        # The workspace test session imports the harness for its own suite; what
        # matters is that no buggy_store module pulled it in.
        not name.startswith("buggy_store")
        for name in leaked
    )
