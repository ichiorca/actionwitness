"""015-T1 — authorizing an external-surface audit (§12.17, FR-160).

FR-160: "An external audit shall run only against an exact HTTPS origin the
operator supplies and explicitly asserts authorization for... Absent
authorization there is no audit." The origin "must additionally appear in a
deployment-configured allowlist".

**Two locks, and the tests check that each one alone is not enough.** A
deployment allowlist without an operator assertion means configuration silently
authorizes whoever finds the workspace; an assertion without an allowlist means
an anonymous visitor points the harness at a stranger by typing a URL. The
second is the crawler this product refuses to be, and §29.1 ships the public
deployment with the module off for exactly that reason.

The refusals are deliberately indistinguishable from one another. A caller told
"that origin is well-formed but not allowed" can enumerate the allowlist one
guess at a time, so a well-formed stranger and a malformed string get the same
answer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

AUDITS = f"{API_PREFIX}/audits"
ALLOWED = "https://shop.example"
OTHER = "https://not-allowed.example"

CONFIGURED = {
    "HARNESS_ENV": "local",
    "BUGGY_STORE_ENABLED": "false",
    "EXTERNAL_AUDIT_ENABLED": "true",
    "EXTERNAL_AUDIT_ALLOWED_ORIGINS": f"{ALLOWED},https://second.example",
}
UNCONFIGURED = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}


def _app(tmp_path: Path, environ: dict[str, str], name: str) -> FastAPI:
    return create_app(environ=environ, database_path=tmp_path / f"{name}.sqlite3")


@pytest.fixture
async def configured(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = _app(tmp_path, CONFIGURED, "configured")
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def unconfigured(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = _app(tmp_path, UNCONFIGURED, "unconfigured")
    async with app.router.lifespan_context(app):
        yield app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


def body(**over: Any) -> dict[str, Any]:
    return {"origin": ALLOWED, "asserted_by": "operator@example", "authorized": True, **over}


# --- the module gate (criterion 1) -------------------------------------------


async def test_an_unconfigured_deployment_authorizes_nothing(unconfigured: FastAPI) -> None:
    """§29.1: the public deployment ships with the audit disabled, so "an
    anonymous visitor can never direct it at a third party"."""
    async with client(unconfigured) as visitor:
        response = await visitor.post(
            AUDITS, json=body(), headers={"Origin": "https://harness.test"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"


async def test_an_unconfigured_deployment_still_answers_rather_than_404s(
    unconfigured: FastAPI,
) -> None:
    """§21.1: an absent module is a named unavailable state.

    A 404 would read as a wrong URL and send an operator looking for a typo
    instead of at their configuration.
    """
    async with client(unconfigured) as visitor:
        response = await visitor.get(f"{AUDITS}/current")

    assert response.status_code == 200
    assert response.json() == {"audit": None}


async def test_the_capability_surface_reports_the_module_as_off(
    unconfigured: FastAPI,
) -> None:
    """Criterion 1, through the 009-T12 mechanism: visibly disabled, not absent."""
    async with client(unconfigured) as visitor:
        modules = (await visitor.get(f"{API_PREFIX}/workspace")).json()["modules"]

    assert modules["external_audit"]["status"] == "disabled"
    assert modules["external_audit"]["reason"].strip()


# --- the two locks (criterion 3) ---------------------------------------------


async def test_an_allowlisted_origin_with_an_assertion_is_authorized(
    configured: FastAPI,
) -> None:
    """The guard on every refusal below: the honest path has to work."""
    async with client(configured) as visitor:
        response = await visitor.post(
            AUDITS, json=body(), headers={"Origin": "https://harness.test"}
        )

    assert response.status_code == 201, response.text
    document = response.json()
    assert document["authorized_origin"] == ALLOWED
    assert document["status"] == "authorized"
    assert document["authorization_asserted_by"] == "operator@example"
    assert document["authorization_asserted_at"]


async def test_an_origin_outside_the_allowlist_is_refused(configured: FastAPI) -> None:
    """The lock that stops an anonymous visitor pointing this at a stranger."""
    async with client(configured) as visitor:
        response = await visitor.post(
            AUDITS, json=body(origin=OTHER), headers={"Origin": "https://harness.test"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"


async def test_an_allowlisted_origin_without_an_assertion_is_refused(
    configured: FastAPI,
) -> None:
    """The other lock. Configuration alone must not authorize anything."""
    async with client(configured) as visitor:
        response = await visitor.post(
            AUDITS, json=body(authorized=False), headers={"Origin": "https://harness.test"}
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"


async def test_a_refused_assertion_records_no_audit(configured: FastAPI) -> None:
    """FR-160: "Absent authorization there is no audit." Not a row, not a draft."""
    async with client(configured) as visitor:
        await visitor.post(
            AUDITS, json=body(origin=OTHER), headers={"Origin": "https://harness.test"}
        )
        current = await visitor.get(f"{AUDITS}/current")

    assert current.json() == {"audit": None}


@pytest.mark.parametrize(
    "origin",
    [
        "http://shop.example",  # not HTTPS
        "https://shop.example/cart",  # carries a path
        "https://shop.example?a=1",  # carries a query
        "https://user:pw@shop.example",  # embeds credentials
        "https://shop.example.evil.test",  # the near-miss the allowlist exists for
        "shop.example",  # not a URL at all
        "",  # nothing
    ],
)
async def test_an_origin_that_is_not_exact_is_refused(configured: FastAPI, origin: str) -> None:
    """Exactness is the whole comparison.

    `https://shop.example` matching `https://shop.example.evil.test` is the
    vulnerability, and a path or a credential in an "origin" means the caller is
    describing something other than an origin.
    """
    async with client(configured) as visitor:
        response = await visitor.post(
            AUDITS, json=body(origin=origin), headers={"Origin": "https://harness.test"}
        )

    assert response.status_code in {403, 422}


async def test_a_refusal_does_not_reveal_whether_the_origin_was_well_formed(
    configured: FastAPI,
) -> None:
    """Otherwise the allowlist can be enumerated one guess at a time."""
    async with client(configured) as visitor:
        stranger = await visitor.post(
            AUDITS, json=body(origin=OTHER), headers={"Origin": "https://harness.test"}
        )
        malformed = await visitor.post(
            AUDITS,
            json=body(origin="https://shop.example/cart"),
            headers={"Origin": "https://harness.test"},
        )

    assert stranger.status_code == malformed.status_code == 403
    assert stranger.json()["error"]["code"] == malformed.json()["error"]["code"]


# --- one audit at a time -----------------------------------------------------


async def test_a_workspace_holds_one_live_audit(configured: FastAPI) -> None:
    """§22: "At most one nonterminal audit may exist per interactive workspace."

    Two live audits would mean a second origin authorized under the first one's
    assertion, which is the thing the assertion exists to prevent.
    """
    async with client(configured) as visitor:
        first = await visitor.post(AUDITS, json=body(), headers={"Origin": "https://harness.test"})
        second = await visitor.post(
            AUDITS,
            json=body(origin="https://second.example"),
            headers={"Origin": "https://harness.test"},
        )

    assert first.status_code == 201
    assert second.status_code == 409


async def test_an_audit_belongs_to_the_workspace_that_asserted_it(
    configured: FastAPI,
) -> None:
    """A second visitor must not inherit somebody else's authorization."""
    async with client(configured) as owner:
        await owner.post(AUDITS, json=body(), headers={"Origin": "https://harness.test"})

    async with client(configured) as stranger:
        current = await stranger.get(f"{AUDITS}/current")

    assert current.json() == {"audit": None}


# --- never a crawler ---------------------------------------------------------


async def test_the_api_offers_no_way_to_submit_more_than_one_origin(
    configured: FastAPI,
) -> None:
    """The guardrail as a shape, not a promise.

    A list of origins is a scan queue with a friendlier name, so the request
    model refuses one outright rather than accepting and using the first.
    """
    async with client(configured) as visitor:
        response = await visitor.post(
            AUDITS,
            json={
                "origins": [ALLOWED, OTHER],
                "asserted_by": "operator@example",
                "authorized": True,
            },
            headers={"Origin": "https://harness.test"},
        )

    assert response.status_code == 422


async def test_no_audit_route_accepts_an_unknown_field(configured: FastAPI) -> None:
    """Unknown fields are refused, not ignored: `{"crawl": true}` silently
    dropped is an operator who believes they asked for something."""
    async with client(configured) as visitor:
        response = await visitor.post(
            AUDITS,
            json=body(crawl=True),
            headers={"Origin": "https://harness.test"},
        )

    assert response.status_code == 422
