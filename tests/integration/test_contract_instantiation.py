"""012-T5 — `POST /contracts` through the real boundary (FR-021, FR-022, §15.2).

The unit tests in `tests/unit/test_template_expansion.py` pin the expansion
arithmetic. These pin what a *client* can reach: that the route refuses what
FR-021 says it must, that a rejection names the offending control, and that the
contract it creates is immutable, workspace-scoped, and armable.

The one worth reading twice is
`test_a_body_carrying_contract_terms_is_refused_outright`. FR-021 says the
declarative form "shall never accept nested assertions, policies, paths, or
arbitrary JSON", and the strong form of that is not "extra keys are ignored" —
it is that the request fails. A caller whose `assertions` array was quietly
dropped would be told their contract was created and would reasonably believe
it contained what they sent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"
CHECKOUT = "confirmed_checkout_only"


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    store = create_store(database_path=tmp_path / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ={
                "HARNESS_ENV": "local",
                "BUGGY_STORE_ENABLED": "true",
                "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            },
            database_path=tmp_path / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            yield harness


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


def _paths(response: httpx.Response) -> set[str]:
    return {detail["path"] for detail in response.json()["error"]["details"]}


# --- the journey §6.3 describes ----------------------------------------------


async def test_the_listing_publishes_the_controls_each_template_accepts(
    visitor: httpx.AsyncClient,
) -> None:
    """Step 2: the person "supplies only flat scalar parameters".

    Which ones depends on the template, so the chooser has to be told. A form
    offering a discount field for a contract with no discount term would invite
    a rejection the person could have been spared.
    """
    # Arrange / Act
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    by_template = {item["source_template_id"]: item["parameters"] for item in templates}

    # Assert
    assert by_template[CANONICAL] == ["quantity", "discount_code"]
    assert by_template[CHECKOUT] == []


async def test_a_flat_submission_creates_one_immutable_contract(
    visitor: httpx.AsyncClient,
) -> None:
    """Steps 3–5: submit flat scalars, and the expanded contract is readable.

    The response is not taken at its word. The contract is read back and its
    terms checked, because "the endpoint returned 201" is a self-report and the
    stored document is the thing a run will actually be judged against.
    """
    # Arrange / Act
    created = await visitor.post(
        CONTRACTS,
        json={"template_id": CANONICAL, "quantity": 3, "contract_name": "Three mugs"},
    )
    assert created.status_code == 201, created.text
    contract_id = created.json()["contract_id"]

    # Assert
    stored = (await visitor.get(f"{CONTRACTS}/{contract_id}")).json()
    assert stored["name"] == "Three mugs"
    assert stored["source_template_id"] == CANONICAL
    assert stored["is_built_in"] is False
    values = {item["id"]: item.get("value") for item in stored["document"]["assertions"]}
    assert values["mug-quantity"] == 3
    assert values["discounted-total"] == "60.00"


async def test_the_created_contract_can_be_selected_and_armed(
    visitor: httpx.AsyncClient,
) -> None:
    """Steps 6–7: "state-dependent tools for arming ... become registered".

    A contract that cannot be armed is a form that appears to work. This is the
    end of the journey the declarative form exists to start.
    """
    # Arrange
    contract_id = (
        await visitor.post(CONTRACTS, json={"template_id": CANONICAL, "quantity": 2})
    ).json()["contract_id"]

    # Act
    selected = await visitor.post(f"{CONTRACTS}/{contract_id}/select")
    armed = await visitor.post(RUNS)

    # Assert
    assert selected.status_code == 200, selected.text
    assert armed.status_code in {200, 201}, armed.text


async def test_two_submissions_of_the_same_form_are_two_contracts(
    visitor: httpx.AsyncClient,
) -> None:
    """§17.1: contracts are insert-only, and instantiation never updates one.

    Identical terms hash identically — that is what a content hash is for — but
    each submission is its own immutable record, so a run armed against the
    first is never relabelled by the second (FR-012, FR-023).
    """
    # Arrange
    body = {"template_id": CANONICAL, "quantity": 2}

    # Act
    first = (await visitor.post(CONTRACTS, json=body)).json()
    second = (await visitor.post(CONTRACTS, json=body)).json()

    # Assert
    assert first["contract_id"] != second["contract_id"]
    assert first["content_hash"] == second["content_hash"]


# --- FR-021's refusals --------------------------------------------------------


async def test_a_body_carrying_contract_terms_is_refused_outright(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-021: "never accept nested assertions, policies, paths, or arbitrary
    JSON".

    Refused, not filtered. A caller whose `assertions` array was quietly dropped
    would be told their contract was created and would reasonably believe it
    contained what they sent — a false belief manufactured by the harness.
    """
    # Arrange / Act
    refused = await visitor.post(
        CONTRACTS,
        json={
            "template_id": CANONICAL,
            "assertions": [{"id": "x", "path": "target.cart.total", "operator": "exists"}],
        },
    )

    # Assert
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_a_scalar_the_template_does_not_allowlist_is_refused_by_name(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-022: "structured field errors".

    `confirmed_checkout_only` says nothing about quantity. The refusal names
    `quantity` so a person can see which control to clear, rather than being
    told the submission was invalid and left to guess.
    """
    # Arrange / Act
    refused = await visitor.post(CONTRACTS, json={"template_id": CHECKOUT, "quantity": 2})

    # Assert
    assert refused.status_code == 422, refused.text
    assert _paths(refused) == {"quantity"}


async def test_every_bad_field_is_named_in_one_response(visitor: httpx.AsyncClient) -> None:
    """One round trip per mistake makes a form miserable to fill in."""
    # Arrange / Act
    refused = await visitor.post(
        CONTRACTS,
        json={"template_id": CANONICAL, "quantity": 99, "discount_code": "SAVE99"},
    )

    # Assert
    assert _paths(refused) == {"quantity", "discount_code"}


async def test_an_unknown_template_is_refused(visitor: httpx.AsyncClient) -> None:
    """`template_id` is a choice from a published set, never a lookup key a
    caller can steer."""
    # Arrange / Act
    refused = await visitor.post(CONTRACTS, json={"template_id": "../../etc/passwd"})

    # Assert
    assert refused.status_code == 422, refused.text
    assert _paths(refused) == {"template_id"}


async def test_the_created_contract_belongs_to_the_submitting_workspace(
    stack: FastAPI,
) -> None:
    """FR-006: a contract created in one workspace is invisible in another.

    Instantiation is the first route that writes a contract a *client* named, so
    it is the first place the isolation boundary could be crossed by one. Read
    from a second workspace it is a 404 rather than a 403 — a 403 would confirm
    the identifier names something real.
    """
    # Arrange — two clients, each with its own server-issued workspace cookie.
    transport = httpx.ASGITransport(app=stack, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://harness.test") as author,
        httpx.AsyncClient(transport=transport, base_url="https://harness.test") as stranger,
    ):
        contract_id = (await author.post(CONTRACTS, json={"template_id": CANONICAL})).json()[
            "contract_id"
        ]

        # Act
        seen = await stranger.get(f"{CONTRACTS}/{contract_id}")

    # Assert
    assert seen.status_code == 404, seen.text
