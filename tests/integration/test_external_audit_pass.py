"""015 — the audit pass, driven through the API a client actually has.

The feature had every piece except the one that joins them. Authorization
existed, the classifier existed, the report composer existed, and no endpoint
ran them: an operator could assert authorization, get a 201, and then have
nothing to call, while the audit sat in `authorized` holding the workspace's one
live-audit slot until the workspace aged out a day later.

So these tests are deliberately *HTTP-only*. `test_015_exit_gate.py` composes
the adapter, the classifier, and the report composer directly and proves the
server-side arithmetic; that is worth having and it is not this. Nothing below
imports an application module — if the workflow is reachable only by assembling
objects in a test, every assertion here fails, which is the regression that
would otherwise have gone unnoticed for another release.

The storefront being audited is the half-broken fixture: read tools work, the
cart tool answers `{"status":"success"}` and moves nothing, and checkout sits
there reachable and unexercised.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

AUDITS = f"{API_PREFIX}/audits"
ORIGIN = "https://fixture-storefront.example"
STRANGER = "https://someone-elses-brand.example"

CONFIGURED = {
    "HARNESS_ENV": "local",
    "BUGGY_STORE_ENABLED": "false",
    "EXTERNAL_AUDIT_ENABLED": "true",
    "EXTERNAL_AUDIT_ALLOWED_ORIGINS": ORIGIN,
}

#: What the fixture publishes: read tools, a lying cart tool, and checkout
#: sitting there reachable.
ENUMERATED = ["search_catalog", "get_cart", "update_cart", "proceed_to_checkout"]

EMPTY_CART: dict[str, Any] = {
    "items": [],
    "item_count": 0,
    "total_price": 0,
    "items_subtotal_price": 0,
    "currency": "USD",
}
FILLED_CART: dict[str, Any] = {
    **EMPTY_CART,
    "items": [{"variant_id": 111, "quantity": 1, "price": 2599, "line_price": 2599}],
    "item_count": 1,
    "total_price": 2599,
    "items_subtotal_price": 2599,
}

#: The identical cheerful answer whether or not anything moved — the claim the
#: audit exists to check rather than repeat.
CHEERFUL = {
    "update_cart": {
        "summary": '{"status":"success","message":"Added to cart"}',
        "expects_state_change": True,
    }
}


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(
        environ={**CONFIGURED, "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts")},
        database_path=tmp_path / "audit.sqlite3",
    )
    async with application.router.lifespan_context(application):
        yield application


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _authorize(visitor: httpx.AsyncClient, origin: str = ORIGIN) -> str:
    response = await visitor.post(
        AUDITS, json={"origin": origin, "asserted_by": "the shop owner", "authorized": True}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["audit_id"])


def _evidence(*, cart_tool_lies: bool, pack_id: str = "shopify_cart") -> dict[str, Any]:
    return {
        "pack_id": pack_id,
        "enumerated": ENUMERATED,
        "reports": CHEERFUL,
        "observed_before": EMPTY_CART,
        "observed_after": EMPTY_CART if cart_tool_lies else FILLED_CART,
    }


# --- the pass runs end to end ------------------------------------------------


async def test_an_authorized_audit_can_be_run_to_a_report(app: FastAPI) -> None:
    """The gap this file exists for: authorize, submit, receive a report.

    Every call is one a client can make. If the workflow is reachable only from
    inside the process, this fails at the submission.
    """
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        response = await visitor.post(
            f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=True)
        )

    # Assert
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["audit"]["status"] == "completed"
    assert body["report_artifact_id"]
    assert body["content_hash"].startswith("sha256:")
    # `audited_site`, not `authorized_origin`: the report is written for the
    # shop owner (§5), so its keys are the merchant-facing vocabulary and the
    # harness terms stay in `evidence`.
    assert body["report"]["audited_site"] == ORIGIN
    assert body["report"]["checked_using_id"] == "shopify_cart"


async def test_the_report_names_the_tool_that_lied(app: FastAPI) -> None:
    """Criterion 2, through the API: reported success, observed nothing.

    The cart tool answered `success` and the independent read says the cart is
    still empty. That disagreement is the product's entire claim, so it has to
    survive the trip through the endpoint rather than only through the
    classifier.
    """
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        report = (
            await visitor.post(f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=True))
        ).json()["report"]

    # Assert
    rendered = json.dumps(report)
    assert "update_cart" in rendered
    assert "silently_failed" in rendered, "the lying cart tool must not be reported as working"


async def test_an_honest_storefront_is_not_accused(app: FastAPI) -> None:
    """The counterfactual, and the harder half.

    Identical submission except that the cart actually moved. A pass that
    accused every storefront would satisfy the test above and be worthless.
    """
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        report = (
            await visitor.post(f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=False))
        ).json()["report"]

    # Assert
    assert "silently_failed" not in json.dumps(report)


async def test_a_never_invoked_tool_is_reported_but_not_exercised(app: FastAPI) -> None:
    """FR-162: `proceed_to_checkout` is present on the surface and stays untouched.

    It is in `enumerated` and absent from `reports`, and the report has to say
    so — a checkout tool nobody mentions reads as a checkout tool nobody found.
    """
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        report = (
            await visitor.post(f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=True))
        ).json()["report"]

    # Assert
    assert "proceed_to_checkout" in json.dumps(report)


async def test_an_absent_observation_channel_is_not_a_pass(app: FastAPI) -> None:
    """§12.17's `observation_unavailable`, constitution §5's rule.

    With no independent read, the only thing the harness has is the tool's own
    cheerful answer — which is the one thing it may not treat as proof.
    """
    # Arrange
    submission = {**_evidence(cart_tool_lies=True), "observed_before": None, "observed_after": None}
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        report = (await visitor.post(f"{AUDITS}/current/evidence", json=submission)).json()[
            "report"
        ]

    # Assert
    rendered = json.dumps(report)
    assert "unobserved" in rendered
    assert "silently_failed" not in rendered, "an unread channel is not a verdict about the tool"


# --- the report is sealed, not recomposed ------------------------------------


async def test_the_stored_report_is_readable_afterwards(app: FastAPI) -> None:
    """A report an operator can come back to, byte-identical to the sealed one."""
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)
        submitted = (
            await visitor.post(f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=True))
        ).json()

        # Act
        fetched = await visitor.get(f"{AUDITS}/current/report")

    # Assert
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["report"] == submitted["report"]
    assert fetched.json()["report_artifact_id"] == submitted["report_artifact_id"]


async def test_a_tampered_audit_report_is_refused_rather_than_served(
    app: FastAPI, tmp_path: Path
) -> None:
    """Constitution §5: an integrity failure is an explicit non-pass.

    The outcome report has had this guarantee since 005; the audit report is the
    document an operator is most likely to forward to somebody else, so serving
    one that no longer matches its recorded hash would be the worst place to
    make an exception. The refusal must also name neither the path nor the hash,
    which together are what a forger needs.
    """
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)
        submitted = (
            await visitor.post(f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=True))
        ).json()
        stored = next((tmp_path / "artifacts").rglob("audit_report-*.json"))
        stored.write_text(
            '{"audited_site": "https://not-what-was-sealed.example"}', encoding="utf-8"
        )

        # Act
        response = await visitor.get(f"{AUDITS}/current/report")

    # Assert
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "HARNESS_ERROR"
    body = response.text
    assert stored.name not in body
    assert submitted["content_hash"] not in body
    assert "not-what-was-sealed" not in body, "a forged value must not be echoed back"


async def test_a_workspace_with_no_completed_audit_has_no_report(app: FastAPI) -> None:
    """Absence is a 404, not an empty report somebody could screenshot."""
    # Arrange / Act
    async with client(app) as visitor:
        await _authorize(visitor)
        response = await visitor.get(f"{AUDITS}/current/report")

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# --- the lifecycle closes ----------------------------------------------------


async def test_completing_an_audit_frees_the_workspace_for_the_next_one(app: FastAPI) -> None:
    """The blocked-forever half of the gap.

    A second authorization used to be refused until the workspace aged out,
    because nothing ever moved an audit to a terminal status.
    """
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)
        await visitor.post(f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=True))

        # Act
        second = await visitor.post(
            AUDITS, json={"origin": ORIGIN, "asserted_by": "the shop owner", "authorized": True}
        )

    # Assert
    assert second.status_code == 201, "a completed audit must not block the next one"


async def test_an_abandoned_audit_can_be_cancelled(app: FastAPI) -> None:
    """§22's `cancelled`: an audit begun by mistake should not cost a day."""
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        cancelled = await visitor.post(f"{AUDITS}/current/cancel")
        again = await visitor.post(
            AUDITS, json={"origin": ORIGIN, "asserted_by": "the shop owner", "authorized": True}
        )

    # Assert
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["audit"]["status"] == "cancelled"
    assert again.status_code == 201


async def test_a_completed_audit_is_not_still_live(app: FastAPI) -> None:
    """`/current` must stop advertising an audit that has finished."""
    # Arrange
    async with client(app) as visitor:
        await _authorize(visitor)
        await visitor.post(f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=True))

        # Act
        current = await visitor.get(f"{AUDITS}/current")

    # Assert
    assert current.json()["audit"] is None


# --- refusals ----------------------------------------------------------------


async def test_evidence_without_an_authorized_audit_is_refused(app: FastAPI) -> None:
    """FR-160: absent authorization there is no audit — and no pass either."""
    # Arrange / Act
    async with client(app) as visitor:
        response = await visitor.post(
            f"{AUDITS}/current/evidence", json=_evidence(cart_tool_lies=True)
        )

    # Assert
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"


async def test_a_pack_the_surface_does_not_support_is_refused(app: FastAPI) -> None:
    """A cart pack against a surface with no cart tool is a selection error.

    Run anyway, it would report the cart tool "absent" and read as a finding
    about the storefront rather than about the choice.
    """
    # Arrange
    submission = {**_evidence(cart_tool_lies=True), "enumerated": ["search_catalog"]}
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        response = await visitor.post(f"{AUDITS}/current/evidence", json=submission)

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRECONDITION_FAILED"


async def test_an_unknown_pack_is_refused(app: FastAPI) -> None:
    # Arrange
    submission = {**_evidence(cart_tool_lies=True), "pack_id": "not_a_pack"}
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        response = await visitor.post(f"{AUDITS}/current/evidence", json=submission)

    # Assert
    assert response.status_code == 404


async def test_a_malformed_cart_payload_is_refused_rather_than_treated_as_unobserved(
    app: FastAPI,
) -> None:
    """A broken read and an absent channel are different facts.

    Collapsing the first into the second would report a storefront as
    unobservable when the submission was simply wrong.
    """
    # Arrange
    submission = {**_evidence(cart_tool_lies=True), "observed_after": {"items": "not a list"}}
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        response = await visitor.post(f"{AUDITS}/current/evidence", json=submission)

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_an_oversized_submission_is_refused_before_it_is_parsed(app: FastAPI) -> None:
    """FR-117's cap, which existed as a constant nothing consulted.

    The payload is the one part of this request an audited storefront controls
    rather than the operator, so the cap has to precede the JSON parser.
    """
    # Arrange — a valid shape, far over the cap.
    submission = {
        **_evidence(cart_tool_lies=True),
        "observed_after": {**EMPTY_CART, "note": "x" * (256 * 1024 + 1)},
    }
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        response = await visitor.post(f"{AUDITS}/current/evidence", json=submission)

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_the_evidence_endpoint_refuses_an_unknown_field(app: FastAPI) -> None:
    """Boundary models forbid unknown fields; a typo must not be ignored."""
    # Arrange
    submission = {**_evidence(cart_tool_lies=True), "origin": STRANGER}
    async with client(app) as visitor:
        await _authorize(visitor)

        # Act
        response = await visitor.post(f"{AUDITS}/current/evidence", json=submission)

    # Assert
    assert response.status_code == 422, "an audit submission must not carry a second origin"


# --- the pack catalogue ------------------------------------------------------


async def test_the_pack_catalogue_is_offered_without_naming_a_target(app: FastAPI) -> None:
    """FR-161: packs are offered and selected explicitly.

    A static catalogue, so offering one costs no request that names a tool list
    or an origin — there is nothing here for a scanner to use.
    """
    # Arrange / Act
    async with client(app) as visitor:
        response = await visitor.get(f"{AUDITS}/packs")

    # Assert
    assert response.status_code == 200
    packs = {pack["pack_id"]: pack for pack in response.json()["packs"]}
    assert "shopify_cart" in packs
    assert "proceed_to_checkout" in packs["shopify_cart"]["never_invoked"]
