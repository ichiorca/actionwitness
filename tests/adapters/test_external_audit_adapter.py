"""015-T2 — the external-audit target (§12.17, §9.3, FR-160a).

This adapter's job is mostly refusal, so most of these tests are about what it
will not do.

**It never promotes a self-report to an observation.** §12.17 exists because a
tool's own success message is the channel under test, and the sites this feature
is aimed at are the ones that report success and change nothing. An adapter that
accepted a tool result as evidence would agree with whatever the site claimed —
which is the failure, wearing the costume of a feature.

**It never invents a channel.** §9.3 names exactly one Tier 3 provider, the
Shopify `cart.js` session read. An origin whose cart cannot be read that way is
unobservable, and `observation_unavailable` is a finding rather than a gap.

**It never drives anything.** `ExternalTargetAdapter` has no `execute` because
§9.1 forbids the adapter from impersonating an external target's tools "through
a second implementation". The absence is asserted here, because an `execute`
added later would look like a feature.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from actionwitness_core.evidence.enums import EvidenceSourceClassification
from actionwitness_core.ports.enums import ExecutionMode
from integrations.shopify.audit import (
    PROVENANCE,
    PROVIDER_ID,
    AuditObservationError,
    ExternalAuditAdapter,
    normalize_cart,
)

pytestmark = pytest.mark.adapters

ORIGIN = "https://shop.example"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def adapter(origin: str = ORIGIN) -> ExternalAuditAdapter:
    return ExternalAuditAdapter(origin, clock=lambda: EPOCH)


def cart(**over: Any) -> dict[str, Any]:
    """A Shopify `cart.js` response, in its documented shape (minor units)."""
    return {
        "items": [
            {"variant_id": 111, "quantity": 1, "price": 2599, "line_price": 2599},
        ],
        "item_count": 1,
        "total_price": 2599,
        "items_subtotal_price": 2599,
        "currency": "USD",
        **over,
    }


# --- normalization into §9.3's namespace -------------------------------------


def test_a_cart_read_becomes_the_conventional_target_namespace() -> None:
    """A contract author writes `target.cart.total` against an audited storefront
    exactly as they would against the demo target."""
    observation = adapter().normalize(cart(), PROVENANCE)

    assert observation.namespace == "target"
    assert observation.provider_id == PROVIDER_ID
    assert observation.as_context()["target"]["cart"]["total"] == "25.99"


def test_money_is_converted_exactly_rather_than_through_a_float() -> None:
    """Shopify reports integer minor units; the harness stores exact decimals.

    `2599 / 100` is not `25.99` in binary floating point, and this value is
    compared for equality by a contract assertion — a run would fail for a
    reason no reader could see.
    """
    observation = adapter().normalize(cart(total_price=2599), PROVENANCE)

    assert observation.payload["cart"]["total"] == "25.99"
    assert observation.payload["cart"]["items"]["111"]["unit_price"] == "25.99"


def test_line_keys_are_variant_ids_rather_than_array_positions() -> None:
    """An unrelated reordering must not look like every line changing.

    Under `no_undeclared_changes` that is a critical failure caused by
    presentation, and Shopify offers no stable per-line key of its own.
    """
    two = cart(
        items=[
            {"variant_id": 111, "quantity": 1, "price": 2599, "line_price": 2599},
            {"variant_id": 222, "quantity": 2, "price": 500, "line_price": 1000},
        ],
        item_count=3,
    )
    reordered = cart(items=list(reversed(two["items"])), item_count=3)

    assert adapter().normalize(two, PROVENANCE).payload == (
        adapter().normalize(reordered, PROVENANCE).payload
    )


def test_the_observation_is_authoritative_and_says_where_it_came_from() -> None:
    """§9.3: provenance `platform_session_api` — independent of tool-return text,
    and deliberately not described as direct database evidence."""
    observation = adapter().normalize(cart(), PROVENANCE)

    assert observation.provenance == PROVENANCE
    assert (
        observation.source_classification is EvidenceSourceClassification.AUTHORITATIVE_OBSERVATION
    )


def test_no_state_version_is_invented() -> None:
    """Shopify's cart carries no monotonic version the harness can trust.

    Inventing one would let a comparison claim state moved when nothing said so;
    FR-032's change detection falls back to the content hash.
    """
    assert adapter().normalize(cart(), PROVENANCE).state_version is None


def test_an_order_path_exists_and_is_false() -> None:
    """FR-162 forbids order creation against an external target.

    Recorded rather than omitted: a contract asserting "no order was created"
    needs a path to assert on, and an absent path makes that assertion
    unevaluable instead of true.
    """
    assert adapter().normalize(cart(), PROVENANCE).payload["order"] == {
        "created": False,
        "order_id": None,
    }


# --- what it refuses ----------------------------------------------------------


@pytest.mark.parametrize(
    "marker",
    ["status", "content", "isError", "result", "reported_status"],
)
def test_a_tool_result_is_never_promoted_to_an_observation(marker: str) -> None:
    """The failure this whole module exists to prevent.

    A payload carrying tool-result fields is a self-report wearing an
    observation's clothes, and accepting one would make the audit agree with
    whatever the site claimed.
    """
    with pytest.raises(AuditObservationError, match="self-report"):
        adapter().normalize({**cart(), marker: "success"}, PROVENANCE)


def test_a_payload_that_is_not_a_cart_read_is_refused() -> None:
    """§9.3 names one channel. An origin whose cart cannot be read that way is
    unobservable, and that is a finding rather than something to paper over."""
    with pytest.raises(AuditObservationError, match=r"not a cart.js payload"):
        adapter().normalize({"greeting": "hello"}, PROVENANCE)


def test_a_mislabelled_provenance_is_refused() -> None:
    """Checked rather than recorded.

    A caller that could label its own payload could label a tool result
    `platform_session_api`, and the independence claim would rest on a string
    the browser chose.
    """
    with pytest.raises(AuditObservationError, match="platform_session_api"):
        adapter().normalize(cart(), "tool_result")


@pytest.mark.parametrize(
    "broken",
    [
        {"total_price": "25.99"},  # a string where minor units belong
        {"total_price": 25.99},  # a float where minor units belong
        {"item_count": -1},  # a negative count
        {"item_count": True},  # a bool, which is an int in Python
        {"currency": "dollars"},  # not a three-letter code
        {"items": {"111": {}}},  # an object where a list belongs
    ],
)
def test_a_malformed_cart_is_refused_rather_than_coerced(broken: dict[str, Any]) -> None:
    """A coerced value is a number nobody observed."""
    with pytest.raises(AuditObservationError):
        adapter().normalize(cart(**broken), PROVENANCE)


def test_a_line_without_a_variant_identifier_is_refused() -> None:
    """It could not be keyed, and keying it by position is the bug above."""
    with pytest.raises(AuditObservationError, match="variant identifier"):
        adapter().normalize(cart(items=[{"quantity": 1, "price": 100}]), PROVENANCE)


# --- the origin lock ----------------------------------------------------------


def test_only_the_exact_authorized_origin_is_accepted() -> None:
    adapter().validate_origin(ORIGIN)


@pytest.mark.parametrize(
    "origin",
    [
        "https://shop.example.evil.test",
        "https://evil.shop.example",
        "http://shop.example",
        "https://shop.example/",
        "https://shop.example:443",
    ],
)
def test_every_other_origin_is_refused(origin: str) -> None:
    """§12.17 forbids following "a redirect, a link, or a navigation beyond" the
    authorized origin, so there is no looser rule here to be talked into."""
    with pytest.raises(AuditObservationError, match="origin"):
        adapter().validate_origin(origin)


# --- the shape of the interface ------------------------------------------------


def test_the_adapter_cannot_drive_the_target() -> None:
    """§9.1: an external target runs its own tools, and the adapter must not
    impersonate them "through a second implementation".

    Asserted because an `execute` added later would look like a feature rather
    than like the boundary violation it is.
    """
    assert not hasattr(adapter(), "execute")
    assert not hasattr(adapter(), "prepare")


def test_the_descriptor_offers_no_injectable_scenario() -> None:
    """FR-162: "Injected fault profiles shall never be available against an
    external target." A `pre_fix` mode here would offer a control that must
    never work."""
    descriptor = adapter().descriptor

    assert descriptor.execution_mode is ExecutionMode.EXTERNAL_WEBMCP
    assert "pre_fix" not in descriptor.supported_scenario_modes
    assert descriptor.supported_scenario_modes == ("external_current",)


def test_normalization_is_deterministic() -> None:
    """Two reads of the same cart must produce the same evidence."""
    assert normalize_cart(cart()) == normalize_cart(cart())
