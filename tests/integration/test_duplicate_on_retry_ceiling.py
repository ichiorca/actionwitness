"""`duplicate_on_retry` at the per-line quantity ceiling (§13.3, App. D.2).

§13.3 fixes the shape of this profile: the duplicated mutation lands in canonical
state "while the tool response stays syntactically valid". Both halves are
load-bearing. A duplicate the response *announces* — a 409, a 500, anything a
caller can branch on — is a different and much easier defect, and it would take
away the only thing this profile demonstrates: that nothing short of independent
observation catches it.

The ceiling is where those two halves collide. `CartLine.quantity` is bounded at
`MAX_LINE_QUANTITY` because Appendix D.2 bounds `update_cart.quantity` there, so
a duplicate of a large-enough quantity asks for a line the canonical model
refuses to hold. The store's answer is to saturate at the ceiling: the response
stays the shape §13.3 requires, and observed state stays wrong in the direction
the fault intends. `test_store_failure_injection.py` covers the profile's
vocabulary and selection; this file covers only the arithmetic at its edge.

The last test in the boundary section records the one case the fault genuinely
cannot demonstrate — a line already at the ceiling, where "duplicated" and
"correct" are the same number. That is a property of the bounded quantity
domain, not a bug to be fixed by letting stored state exceed its own invariant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from buggy_store.api import API_PREFIX, WORKSPACE_HEADER, create_app
from buggy_store.catalog import MAX_LINE_QUANTITY
from buggy_store.failure_injection import FaultProfile

pytestmark = pytest.mark.integration

MUG = "mug-ceramic-001"
FAULT = FaultProfile.DUPLICATE_ON_RETRY.value


#: Appendix D.2 bounds `request_id` at 8..80 characters, so the short labels
#: these tests use are padded rather than sent as-is.
def _request(suffix: str) -> str:
    return f"req-{suffix:>012}"


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The store's own versioned API, with no harness in front of it.

    `raise_app_exceptions=False` is what a real server does: Starlette sends the
    response and then re-raises so the process logs the failure. Only the
    in-process transport surfaces that re-raise to the caller, and these tests
    are about what the *caller* receives — which is exactly the property under
    test here, so the transport must not hide it behind an exception.
    """
    app = create_app(database_path=tmp_path / "store.sqlite3")
    async with (
        app.router.lifespan_context(app),
        httpx.ASGITransport(app=app, raise_app_exceptions=False) as transport,
        httpx.AsyncClient(
            transport=transport,
            base_url="http://buggy-store.test",
            headers={WORKSPACE_HEADER: "ws-1"},
        ) as http,
    ):
        yield http


async def _select(client: httpx.AsyncClient, mode: str) -> None:
    response = await client.post(
        f"{API_PREFIX}/store/scenario",
        json={"scenario_mode": mode, "fault_profile": FAULT},
    )
    assert response.status_code == 200, response.text


async def _mutate(client: httpx.AsyncClient, quantity: int, request_id: str) -> httpx.Response:
    return await client.post(
        f"{API_PREFIX}/store/cart/mutations",
        json={"product_id": MUG, "quantity": quantity, "request_id": request_id},
    )


async def _observed_quantity(client: httpx.AsyncClient) -> int | None:
    """Independent observation of canonical state, not the tool's own report."""
    items = (await client.get(f"{API_PREFIX}/store/cart")).json()["cart"]["items"]
    line = items.get("mug")
    return None if line is None else line["quantity"]


# --- the reproduction (§13.3's "stays syntactically valid") ------------------


async def test_a_duplicate_over_the_ceiling_is_not_an_unhandled_failure(
    client: httpx.AsyncClient,
) -> None:
    """The exact reproduction: quantity 3, one request ID, sent twice.

    3 + 3 is 6, and `CartLine` refuses to hold 6. Before the fix that refusal
    escaped the route as a bare 500 with FastAPI's own body — a response no
    §15.8 client can read, and a defect the caller could see without observing
    anything.
    """
    # Arrange
    await _select(client, "pre_fix")

    # Act
    first = await _mutate(client, 3, _request("dup3"))
    second = await _mutate(client, 3, _request("dup3"))

    # Assert
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["status"] == "success"
    assert set(body) == {"status", "state_version", "cart"}
    assert set(body["cart"]) == {"items", "discount", "subtotal", "total"}


async def test_the_clamped_duplicate_is_still_visible_to_observation(
    client: httpx.AsyncClient,
) -> None:
    """AC-05's second clause survives the clamp.

    The caller asked for 3 twice under one request ID. A retry-safe store ends
    with 3; this one ends with 5. The number is not the doubled 6 the injector
    computes, but it is still not 3 — which is all the harness needs, because it
    compares observed state against the contract's expectation, not against the
    injector's arithmetic.
    """
    # Arrange
    await _select(client, "pre_fix")

    # Act
    await _mutate(client, 3, _request("dup3"))
    await _mutate(client, 3, _request("dup3"))

    # Assert
    assert await _observed_quantity(client) == MAX_LINE_QUANTITY
    assert await _observed_quantity(client) != 3


# --- the boundary: below, exactly at, and above the ceiling ------------------


async def test_a_duplicate_below_the_ceiling_doubles_the_line(
    client: httpx.AsyncClient,
) -> None:
    """2 + 2 is 4, which the model holds — the clamp must not fire early.

    This is the quantity the prebuilt retry contract uses, so a clamp that
    engaged below the ceiling would silently change what AC-05 observes.
    """
    # Arrange
    await _select(client, "pre_fix")

    # Act
    await _mutate(client, 2, _request("dup2"))
    repeated = await _mutate(client, 2, _request("dup2"))

    # Assert
    assert repeated.status_code == 200, repeated.text
    assert await _observed_quantity(client) == 4


async def test_a_duplicate_landing_exactly_on_the_ceiling_is_kept_whole(
    client: httpx.AsyncClient,
) -> None:
    """2 + 3 is exactly 5, so nothing is clamped away.

    Reached by an intervening absolute assignment rather than by a bigger
    repeat: the injector adds the retried quantity to *whatever the line holds
    now*, and an identical retry can only ever produce an even sum.
    """
    # Arrange
    await _select(client, "pre_fix")
    await _mutate(client, 3, _request("dup3"))
    await _mutate(client, 2, _request("set2"))
    assert await _observed_quantity(client) == 2

    # Act
    repeated = await _mutate(client, 3, _request("dup3"))

    # Assert
    assert repeated.status_code == 200, repeated.text
    assert await _observed_quantity(client) == MAX_LINE_QUANTITY
    assert repeated.json()["cart"]["items"]["mug"]["quantity"] == MAX_LINE_QUANTITY


async def test_a_duplicate_above_the_ceiling_saturates_rather_than_refusing(
    client: httpx.AsyncClient,
) -> None:
    """4 + 4 is 8, which is well past the ceiling and still not an error."""
    # Arrange
    await _select(client, "pre_fix")

    # Act
    await _mutate(client, 4, _request("dup4"))
    repeated = await _mutate(client, 4, _request("dup4"))

    # Assert
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["status"] == "success"
    assert await _observed_quantity(client) == MAX_LINE_QUANTITY


async def test_a_line_already_at_the_ceiling_shows_no_duplication(
    client: httpx.AsyncClient,
) -> None:
    """The one case the fault cannot demonstrate, recorded rather than hidden.

    5 + 5 saturates back to 5, so observed state equals what the caller asked
    for and `idempotent_by_request_id` sees nothing. That is honest: the store
    cannot hold six mugs, so there is no duplicate to observe. The alternative —
    letting stored state exceed the invariant Appendix D.2 fixes — would weaken
    a canonical-state rule to make a demo more dramatic, which §5 forbids.
    """
    # Arrange
    await _select(client, "pre_fix")

    # Act
    await _mutate(client, MAX_LINE_QUANTITY, _request("dup5"))
    repeated = await _mutate(client, MAX_LINE_QUANTITY, _request("dup5"))

    # Assert
    assert repeated.status_code == 200, repeated.text
    assert await _observed_quantity(client) == MAX_LINE_QUANTITY


# --- the same journey without the fault (FR-011) -----------------------------


async def test_the_ceiling_journey_is_retry_safe_in_post_fix(
    client: httpx.AsyncClient,
) -> None:
    """The counterpart that makes the tests above attributable to the injector.

    FR-011 keeps the profile recorded and inactive in `post_fix`. A store that
    clamped, doubled, or errored here would mean the behaviour above was a
    property of the store rather than of the fault.
    """
    # Arrange
    await _select(client, "post_fix")

    # Act
    await _mutate(client, 3, _request("dup3"))
    repeated = await _mutate(client, 3, _request("dup3"))

    # Assert
    assert repeated.status_code == 200, repeated.text
    assert await _observed_quantity(client) == 3
