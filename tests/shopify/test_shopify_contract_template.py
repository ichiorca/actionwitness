"""`shopify_exact_cart` through the generic contract path (FR-021, FR-023, FR-024).

The template used to exist only privately inside the pairing routes: not listed
by `GET /contracts/templates`, not instantiable by `POST /contracts`. FR-021
describes its scalars in terms of the generic path, FR-023 requires the
template to exist, and FR-024 treats selecting it as a normal selection that
fails with `TARGET_UNAVAILABLE` when the module is off — none of which parses
unless the generic endpoints know the template. These tests hold that door
open, HTTP-only like the rest of the lane.

The property under most of them is FR-110's: **which variant and currency count
as correct is a deployment decision.** The caller's only term is the display
name; the configured values arrive in the expansion from server configuration,
and a caller who tries to supply them is refused by name rather than merged.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from fastapi import FastAPI

pytestmark = [pytest.mark.shopify, pytest.mark.integration]

CONTRACTS = f"{API_PREFIX}/contracts"
WORKSPACE = f"{API_PREFIX}/workspace"
TEMPLATE = "shopify_exact_cart"

HARNESS_ORIGIN = "https://harness.test"


def _template_rows(listing: httpx.Response) -> list[dict[str, Any]]:
    assert listing.status_code == 200, listing.text
    return [row for row in listing.json()["templates"] if row["source_template_id"] == TEMPLATE]


def _detail_paths(refused: httpx.Response) -> list[str]:
    assert refused.status_code == 422, refused.text
    return [str(detail["path"]) for detail in refused.json()["error"]["details"]]


async def test_the_template_is_listed_when_the_module_is_on(ui: httpx.AsyncClient) -> None:
    """FR-023: the seeded template row exists and advertises no caller scalar."""
    rows = _template_rows(await ui.get(f"{CONTRACTS}/templates"))

    assert len(rows) == 1, "the Shopify pack seeds exactly one template"
    # Server-expanded, not form-parameterized (FR-021): variant and currency
    # are deployment configuration, so the row offers no field to fill in.
    assert rows[0]["parameters"] == []


async def test_the_template_is_absent_when_the_module_is_off(
    tmp_path: Any, frozen_clock: Any
) -> None:
    """§21.1: an unconfigured module contributes nothing — and refuses by name."""
    application: FastAPI = create_app(
        environ={
            "HARNESS_ENV": "local",
            "BUGGY_STORE_ENABLED": "false",
            "HARNESS_PUBLIC_ORIGIN": HARNESS_ORIGIN,
            "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        },
        database_path=tmp_path / "off.sqlite3",
        clock=frozen_clock,
    )
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
            base_url=HARNESS_ORIGIN,
        ) as ui,
    ):
        assert _template_rows(await ui.get(f"{CONTRACTS}/templates")) == []

        refused = await ui.post(CONTRACTS, json={"template_id": TEMPLATE})
        assert any(path.endswith("template_id") for path in _detail_paths(refused))


async def test_instantiating_and_selecting_binds_the_development_store(
    ui: httpx.AsyncClient,
) -> None:
    """FR-024: the contract's own target comes with it — never from the request."""
    created = await ui.post(CONTRACTS, json={"template_id": TEMPLATE, "contract_name": "mine"})
    assert created.status_code == 201, created.text
    contract_id = created.json()["contract_id"]

    read = await ui.get(f"{CONTRACTS}/{contract_id}")
    assert read.status_code == 200, read.text
    # The display name is the one term the caller controls (FR-021).
    assert read.json()["name"] == "mine"

    selected = await ui.post(f"{CONTRACTS}/{contract_id}/select")
    assert selected.status_code == 200, selected.text

    workspace = await ui.get(WORKSPACE)
    assert workspace.status_code == 200, workspace.text
    assert workspace.json()["selected_target_id"] == "shopify-development-store"


async def test_the_expansion_carries_the_configured_variant_and_currency(
    ui: httpx.AsyncClient,
) -> None:
    """FR-110: the two configuration-dependent terms come from the server."""
    created = await ui.post(CONTRACTS, json={"template_id": TEMPLATE})
    assert created.status_code == 201, created.text

    read = await ui.get(f"{CONTRACTS}/{created.json()['contract_id']}")
    assert read.status_code == 200, read.text
    values = {
        assertion["id"]: assertion.get("value")
        for assertion in read.json()["document"]["assertions"]
    }

    # The values the conftest configures the deployment with — never the body's.
    assert values["the-configured-test-variant"] == "42"
    assert values["the-expected-currency"] == "USD"


async def test_server_configuration_cannot_be_supplied_by_the_caller(
    ui: httpx.AsyncClient,
) -> None:
    """FR-110: a body naming the locked terms is refused, not merged.

    `extra="forbid"` on the request model is the boundary that fires here; the
    composition root's spread-server-config-last is the second, independent
    layer, exercised by the test above (an expansion with no caller values
    still carries the configured 42/USD).
    """
    refused = await ui.post(
        CONTRACTS,
        json={
            "template_id": TEMPLATE,
            "variant_id": "999-attacker",
            "expected_currency": "EUR",
            "contract_name": "mine",
        },
    )
    paths = _detail_paths(refused)

    assert any(path.endswith("variant_id") for path in paths), paths
    assert any(path.endswith("expected_currency") for path in paths), paths


async def test_the_form_scalars_are_refused_by_name(ui: httpx.AsyncClient) -> None:
    """FR-021: this template allowlists no scalar — each refusal names its field.

    A quantity silently ignored would leave the caller believing they
    constrained a quantity this contract fixes at one for a reason (FR-114).
    """
    for field_name, value in (("quantity", 2), ("discount_code", "SAVE20")):
        refused = await ui.post(CONTRACTS, json={"template_id": TEMPLATE, field_name: value})
        assert refused.status_code == 422, refused.text

        details = refused.json()["error"]["details"]
        assert any(
            str(detail["path"]).endswith(field_name) and "does not accept" in str(detail["message"])
            for detail in details
        ), details
