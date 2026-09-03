"""Fixtures for the Shopify bridge lane (§26.6, Tier 3).

Everything here builds the **real application** and talks to it over HTTP. That
is the point of the lane rather than a stylistic preference: §15.7's whole
subject is which caller may do what, and an authorization boundary assembled
from objects inside a test is a boundary nobody has to cross.

Two clients, and the separation is itself an assertion. `ui` carries the
anonymous workspace cookie the harness issues; `bridge` is a *different* client
that never sees it, exactly as a theme running on the storefront origin never
sees it — `SameSite=Strict` guarantees that in a browser, and a separate cookie
jar guarantees it here. Every bridge call below authorizes with a bearer
credential and an `Origin` header and nothing else, so a bridge route that
started reading the cookie would find the *wrong* workspace and fail loudly
rather than pass quietly.

Nothing here reads the wall clock. The application is built with the suite's
frozen clock, which is what lets a test age a pairing past FR-111's fifteen
minutes without sleeping.

The helpers are hung off one `trial` fixture rather than exported as module
functions: `tests/` is deliberately not a package, so a sibling test module
cannot import from this file, and pytest's fixture wiring is the supported way
to share them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from fastapi import FastAPI

#: The one authorized development store. Every origin comparison in this lane is
#: equality against this exact string (FR-110).
STORE_ORIGIN = "https://dev-store.myshopify.com"

#: Somebody else's storefront. Never granted anything, anywhere.
STRANGER_ORIGIN = "https://someone-elses-store.myshopify.com"

HARNESS_ORIGIN = "https://harness.test"

#: Server-controlled. The tests read it from here for the same reason the route
#: reads it from settings: a variant a caller could choose would be a caller
#: choosing which cart counted as correct.
TEST_VARIANT = "42"

SHOPIFY_ENV: Mapping[str, str] = {
    "HARNESS_ENV": "local",
    "BUGGY_STORE_ENABLED": "false",
    "HARNESS_PUBLIC_ORIGIN": HARNESS_ORIGIN,
    "SHOPIFY_STORE_ORIGIN": STORE_ORIGIN,
    "SHOPIFY_TEST_VARIANT_ID": TEST_VARIANT,
    "SHOPIFY_EXPECTED_CURRENCY": "USD",
}

PAIRINGS = f"{API_PREFIX}/shopify/pairings"


class Trial:
    """One Shopify trial, driven the way the two real callers drive it.

    Every method is an HTTP call. The constants and the cart builder hang here
    too so a test module can reach them without importing this file.
    """

    STORE = STORE_ORIGIN
    STRANGER = STRANGER_ORIGIN
    VARIANT = TEST_VARIANT
    PAIRINGS = PAIRINGS

    def __init__(self, ui: httpx.AsyncClient, bridge: httpx.AsyncClient) -> None:
        self.ui = ui
        self.bridge = bridge

    @staticmethod
    def cart(
        *,
        variant: str | None = None,
        quantity: int = 1,
        unit_price: int = 2500,
        currency: str = "USD",
        checkout_navigated: bool = False,
    ) -> dict[str, Any]:
        """One `cart.js` body, as a storefront session would return it.

        Money is integer minor units because that is what Shopify sends; the
        integration converts it to exact decimals. `variant=None` is an empty
        cart, which is FR-116's required starting state rather than a special
        case.
        """
        lines = (
            []
            if variant is None
            else [
                {
                    "variant_id": int(variant),
                    "quantity": quantity,
                    "price": unit_price,
                    "line_price": unit_price * quantity,
                }
            ]
        )
        subtotal = sum(int(line["line_price"]) for line in lines)
        return {
            "items": lines,
            "item_count": sum(int(line["quantity"]) for line in lines),
            "items_subtotal_price": subtotal,
            "total_price": subtotal,
            "total_discount": 0,
            "currency": currency,
            # Required, never defaulted: FR-114 makes checkout navigation a
            # failed trial, so an observation that cannot speak to it is not
            # evidence for this contract.
            "page": {"checkout_navigation_observed": checkout_navigated},
        }

    @staticmethod
    def credential_in(launch_url: str) -> str:
        """The one-time credential, read out of FR-111's URL fragment.

        Parsed rather than returned as a field, because the response has no
        field: the fragment is the only place the credential travels, and a test
        that could read it from a key would be testing a route that had one.
        """
        return launch_url.split("#actionwitness=", 1)[1].split(".", 1)[1]

    # -- the harness UI, cookie-authorized ---------------------------------

    async def create(self, **body: Any) -> httpx.Response:
        return await self.ui.post(PAIRINGS, json=dict(body))

    async def status(self, pairing_id: str) -> httpx.Response:
        return await self.ui.get(f"{PAIRINGS}/{pairing_id}")

    # -- the theme bridge, credential- and origin-authorized ---------------

    def _headers(self, credential: str | None, origin: str | None) -> dict[str, str]:
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        return headers

    async def redeem(
        self,
        pairing_id: str,
        credential: str | None,
        *,
        origin: str | None = STORE_ORIGIN,
        bridge_version: str = "1.0.0",
        theme_build_id: str | None = "theme-build-7",
    ) -> httpx.Response:
        return await self.bridge.post(
            f"{PAIRINGS}/{pairing_id}/redeem",
            json={"bridge_version": bridge_version, "theme_build_id": theme_build_id},
            headers=self._headers(credential, origin),
        )

    async def before(
        self,
        pairing_id: str,
        session: str | None,
        payload: Mapping[str, Any],
        *,
        capture_path: str = "/cart.js",
        origin: str | None = STORE_ORIGIN,
    ) -> httpx.Response:
        return await self.bridge.post(
            f"{PAIRINGS}/{pairing_id}/observations/before",
            json={"capture_path": capture_path, "cart": dict(payload)},
            headers=self._headers(session, origin),
        )

    async def verify(
        self,
        pairing_id: str,
        session: str | None,
        payload: Mapping[str, Any],
        *,
        capture_path: str = "/cart.js",
        origin: str | None = STORE_ORIGIN,
    ) -> httpx.Response:
        return await self.bridge.post(
            f"{PAIRINGS}/{pairing_id}/verify",
            json={"capture_path": capture_path, "cart": dict(payload)},
            headers=self._headers(session, origin),
        )

    # -- the two steps almost every test needs before its own ---------------

    async def paired(self, **body: Any) -> tuple[str, str]:
        """Create a pairing and redeem it; return `(pairing_id, session)`.

        Both calls are asserted here rather than in each test: a test about
        expiry that silently ran against a pairing that was never created would
        pass for the wrong reason.
        """
        created = await self.create(**body)
        assert created.status_code == 201, created.text
        pairing_id = created.json()["pairing_id"]

        redeemed = await self.redeem(pairing_id, self.credential_in(created.json()["launch_url"]))
        assert redeemed.status_code == 200, redeemed.text
        return pairing_id, redeemed.json()["bridge_session_credential"]

    async def armed(self, **body: Any) -> tuple[str, str]:
        """A redeemed pairing with its empty-cart baseline accepted."""
        pairing_id, session = await self.paired(**body)
        captured = await self.before(pairing_id, session, self.cart())
        assert captured.status_code == 201, captured.text
        return pairing_id, session


@pytest.fixture
async def app(tmp_path: Path, frozen_clock: Any) -> AsyncIterator[FastAPI]:
    """The composed harness with one configured development store."""
    application = create_app(
        environ={**SHOPIFY_ENV, "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts")},
        database_path=tmp_path / "harness.sqlite3",
        clock=frozen_clock,
    )
    async with application.router.lifespan_context(application):
        yield application


def http_client(app: FastAPI) -> httpx.AsyncClient:
    """A client whose refusals arrive as responses rather than as exceptions."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=HARNESS_ORIGIN,
    )


@pytest.fixture
async def ui(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The harness UI: one workspace cookie, and never a credential."""
    async with http_client(app) as client:
        yield client


@pytest.fixture
async def bridge(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """The theme bridge: a separate client, so it cannot borrow the cookie."""
    async with http_client(app) as client:
        yield client


@pytest.fixture
def trial(ui: httpx.AsyncClient, bridge: httpx.AsyncClient) -> Trial:
    """One trial driven through both callers' real entry points."""
    return Trial(ui, bridge)


@pytest.fixture
async def demo_ui(tmp_path: Path, frozen_clock: Any) -> AsyncIterator[httpx.AsyncClient]:
    """A harness with the demo pack seeded *and* a development store configured.

    Only one test needs it, and it needs it for a specific reason: the seeded
    Buggy Store templates are the repository's real contracts that name
    `proceed_to_checkout`, so FR-114's forbidden-scope refusal can be exercised
    against a contract the product ships rather than one a test invented.
    """
    application = create_app(
        environ={
            **SHOPIFY_ENV,
            "BUGGY_STORE_ENABLED": "true",
            "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        },
        database_path=tmp_path / "demo.sqlite3",
        clock=frozen_clock,
    )
    async with (
        application.router.lifespan_context(application),
        http_client(application) as client,
    ):
        yield client
