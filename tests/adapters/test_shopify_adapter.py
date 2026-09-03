"""011-T5 — the Shopify development-store target (§9.1, §9.3, §12.12, FR-110..FR-118).

Like the audit adapter beside it, this target's job is mostly refusal, so most
of these tests are about what it will not do.

**It never drives Shopify.** `ExternalTargetAdapter` has no `execute` because
§9.1 forbids impersonating an external target's tools "through a second
implementation". The absence is asserted here, because an `execute` added later
would look like a feature.

**It never claims to have watched a tool.** FR-114 says the bridge "cannot
observe Shopify's internal tool trajectory" and AC-18 requires trajectory and
tool execution to stay `not_evaluated`. The adapter publishes no tool surface,
which turns that from a promise into a rule §10.2 enforces on every contract.

**It never fetches.** FR-112 puts the `cart.js` read in the shopper's own
session; the harness holds no session and, under FR-118, no credential that
could create one. `capture` refuses rather than returning an empty cart, because
an empty payload would make every `absent` assertion pass against a store nobody
looked at.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from actionwitness_core.evidence.enums import EvidenceSourceClassification
from actionwitness_core.ports.enums import ExecutionMode
from integrations.shopify.adapter import DESCRIPTOR, TARGET_ID, TARGET_TYPE, ShopifyAdapter
from integrations.shopify.audit import MAX_CART_PAYLOAD_BYTES, PROVENANCE, AuditObservationError
from integrations.shopify.observation import (
    ShopifyCartUnobservable,
    project_cart,
    require_within_payload_bound,
)

pytestmark = pytest.mark.adapters

ORIGIN = "https://dev-store.myshopify.com"
VARIANT = "1234567890"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

MODULE = Path(__file__).resolve().parents[2] / "integrations/shopify/src/integrations/shopify"


def adapter(**over: Any) -> ShopifyAdapter:
    settings: dict[str, Any] = {
        "store_origin": ORIGIN,
        "test_variant_id": VARIANT,
        "expected_currency": "USD",
        "clock": lambda: EPOCH,
    }
    return ShopifyAdapter(**{**settings, **over})


def cart(**over: Any) -> dict[str, Any]:
    """A `cart.js` response holding one of the configured variant, in minor units."""
    return {
        "items": [
            {"variant_id": int(VARIANT), "quantity": 1, "price": 2500, "line_price": 2500},
        ],
        "item_count": 1,
        "items_subtotal_price": 2500,
        "total_price": 2500,
        "total_discount": 0,
        "currency": "USD",
        "page": {"checkout_navigation_observed": False},
        **over,
    }


# --- the descriptor (§9.1) ----------------------------------------------------


def test_the_descriptor_names_the_specs_tier_3_target_type() -> None:
    assert DESCRIPTOR.target_type == TARGET_TYPE == "shopify_development_store"
    assert DESCRIPTOR.target_id == TARGET_ID == "shopify-development-store"
    assert DESCRIPTOR.execution_mode is ExecutionMode.EXTERNAL_WEBMCP


def test_shopify_advertises_only_external_current() -> None:
    """§9.1: Shopify advertises only `external_current`.

    "so the UI disables the pre/post control for Shopify with an explanation."
    There is no fixture to switch between on a store the project does not own,
    and a mode advertised here would put a control on the panel that changed
    nothing.
    """
    assert DESCRIPTOR.supported_scenario_modes == ("external_current",)
    assert not DESCRIPTOR.supports("pre_fix")
    assert not DESCRIPTOR.supports("post_fix")


def test_the_fault_list_says_none_rather_than_saying_nothing() -> None:
    """An empty tuple would permit every profile; `("none",)` permits one.

    `TargetDescriptor.injects` reads an empty list as "this adapter makes no
    claim". FR-162 forbids injecting a fault into an external target at all, so
    silence here would let a run be armed with a defect profile and produce a
    report naming an active fault that nothing in the world produced.
    """
    assert DESCRIPTOR.supported_fault_profiles == ("none",)
    assert DESCRIPTOR.injects("none")
    assert not DESCRIPTOR.injects("discount_reported_but_not_applied")


# --- observed, never driven (§9.1) -------------------------------------------


def test_the_adapter_has_no_execute() -> None:
    """§9.1: an external adapter never impersonates the tools the browser ran."""
    assert not hasattr(adapter(), "execute")
    assert not hasattr(adapter(), "prepare")


def test_the_adapter_publishes_no_tool_surface() -> None:
    """FR-114 / AC-18: the bridge cannot see Shopify's tool trajectory.

    An empty surface is not a gap: §10.2 refuses a contract naming a tool the
    selected adapter does not publish, so this is what makes it impossible to
    author a Shopify contract claiming `proceed_to_checkout` was not called — a
    claim nothing here could witness.
    """
    assert adapter().tool_specs() == ()
    assert adapter().effect_map() == {}


def test_no_shopify_target_module_can_reach_the_network() -> None:
    """FR-112/FR-160a, as an absence of capability rather than a rule.

    The configured origin is a string somebody supplied. A module holding both
    that string and an HTTP client is one edit away from being a crawler, and no
    amount of care in the calling code changes that. The sibling audit modules
    are gated the same way in `tests/architecture/test_audit_guardrails.py`.
    """
    network = {"httpx", "requests", "urllib", "urllib3", "http", "socket", "aiohttp", "subprocess"}
    offenders: list[str] = []
    for name in ("adapter.py", "observation.py", "templates.py"):
        tree = ast.parse((MODULE / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                imported = [node.module or ""]
            else:
                continue
            offenders += [f"{name} -> {mod}" for mod in imported if mod.split(".")[0] in network]

    assert offenders == [], f"a Shopify target module can originate a request: {offenders}"


# --- exact origin (FR-110) ----------------------------------------------------


def test_the_configured_origin_is_accepted() -> None:
    adapter().validate_origin(ORIGIN)


@pytest.mark.parametrize(
    "origin",
    [
        "https://dev-store.myshopify.com.evil.test",
        "https://evil.test/dev-store.myshopify.com",
        "https://sub.dev-store.myshopify.com",
        "http://dev-store.myshopify.com",
        "https://dev-store.myshopify.com/",
        "https://DEV-STORE.myshopify.com",
        "*",
        "null",
        "",
    ],
)
def test_a_non_configured_origin_is_refused(origin: str) -> None:
    """FR-110: "reject redirects or observations from any other origin".

    Equality, so there is no prefix, suffix or subdomain rule here to be talked
    into `https://dev-store.myshopify.com.evil.test`.
    """
    with pytest.raises(AuditObservationError):
        adapter().validate_origin(origin)


def test_a_payload_labelled_with_another_store_is_refused() -> None:
    """A bridge submitting somebody else's cart is the cross-origin case.

    Believing the cart while ignoring the label would be the loosest possible
    reading of FR-110 — and the recorded origin is the server's either way, so
    the mislabelled cart would have been filed under the authorized store.
    """
    payload = cart(
        page={"checkout_navigation_observed": False, "store_origin": "https://other.test"}
    )

    with pytest.raises(AuditObservationError):
        adapter().normalize(payload, PROVENANCE)


def test_the_recorded_origin_comes_from_configuration_not_the_payload() -> None:
    observed = adapter().normalize(cart(), PROVENANCE)

    assert observed.payload["page"]["store_origin"] == ORIGIN


# --- the observation is an observation (§9.3) --------------------------------


def test_a_normalized_cart_is_an_authoritative_observation() -> None:
    observed = adapter().normalize(cart(), PROVENANCE)

    assert observed.provider_id == "shopify_cart_state"
    assert observed.provenance == "platform_session_api"
    assert observed.namespace == "target"
    assert observed.source_classification is EvidenceSourceClassification.AUTHORITATIVE_OBSERVATION
    assert observed.captured_at == EPOCH
    # Shopify's cart carries no monotonic version the harness can trust.
    assert observed.state_version is None


async def test_the_provider_refuses_to_fetch_for_itself() -> None:
    """FR-112: the read happens in the shopper's session, not in the harness.

    Refusing rather than returning an empty observation — constitution §5 makes
    observation failure "an explicit non-pass result; it never degrades to
    success".
    """
    with pytest.raises(ShopifyCartUnobservable):
        await adapter().observation_provider().capture("ws_1")


def test_a_provenance_the_caller_chose_is_refused() -> None:
    """A caller that could label its own payload could label a tool result."""
    with pytest.raises(AuditObservationError):
        adapter().normalize(cart(), "tool_reported")


@pytest.mark.parametrize(
    "marker", ["status", "content", "isError", "is_error", "result", "reported_status"]
)
def test_a_tool_result_is_never_promoted_to_an_observation(marker: str) -> None:
    """The failure this product exists to catch, wearing an observation's clothes."""
    with pytest.raises(AuditObservationError):
        adapter().normalize(cart(**{marker: "success"}), PROVENANCE)


# --- the §13.5 projection -----------------------------------------------------


def test_the_configured_variant_takes_the_stable_line_key() -> None:
    """§13.5's "stable line key `test_variant`".

    The contract can then name a line without naming a number, while the variant
    id stays on the line so the report can still say which one was expected.
    """
    observed = adapter().normalize(cart(), PROVENANCE)
    items = observed.payload["cart"]["items"]

    assert set(items) == {"test_variant"}
    assert items["test_variant"] == {
        "variant_id": VARIANT,
        "quantity": 1,
        "unit_price": "25.00",
        "line_total": "25.00",
    }


def test_an_unexpected_variant_keeps_its_own_key_rather_than_being_refused() -> None:
    """FR-114 makes an unexpected variant "a failed or incomplete trial".

    Failed, which is a finding with evidence attached — so the normalizer records
    what was actually in the cart. Refusing here would turn a demonstrable wrong
    outcome into an unreadable parse error, and the contract could not say what
    the agent had actually done.
    """
    payload = cart(items=[{"variant_id": 999, "quantity": 1, "price": 2500, "line_price": 2500}])

    items = adapter().normalize(payload, PROVENANCE).payload["cart"]["items"]

    assert set(items) == {"999"}
    assert "test_variant" not in items


def test_money_is_exact_decimal_from_minor_units() -> None:
    """FR-113: integer minor units become fixed-scale decimals.

    `2599 / 100` is not `25.99` in binary floating point, and these values are
    compared for equality by contract assertions.
    """
    payload = cart(
        items=[{"variant_id": int(VARIANT), "quantity": 1, "price": 2599, "line_price": 2599}],
        items_subtotal_price=2599,
        total_price=2599,
    )

    cart_state = adapter().normalize(payload, PROVENANCE).payload["cart"]

    assert cart_state["subtotal"] == "25.99"
    assert cart_state["total"] == "25.99"
    assert cart_state["items"]["test_variant"]["unit_price"] == "25.99"


@pytest.mark.parametrize("field", ["price", "line_price", "total_price", "items_subtotal_price"])
def test_a_float_amount_is_refused_rather_than_rounded(field: str) -> None:
    payload = cart()
    if field in {"price", "line_price"}:
        payload["items"][0][field] = 25.0
    else:
        payload[field] = 25.0

    with pytest.raises(AuditObservationError):
        adapter().normalize(payload, PROVENANCE)


def test_a_discount_is_recorded_from_the_carts_own_field() -> None:
    payload = cart(items_subtotal_price=2500, total_price=2000, total_discount=500)

    cart_state = adapter().normalize(payload, PROVENANCE).payload["cart"]

    assert cart_state["discount_total"] == "5.00"
    assert cart_state["totals_consistent"] is True


def test_totals_that_do_not_add_up_are_reported_not_refused() -> None:
    """§13.5's "internal arithmetic consistency", as a finding.

    A cart whose subtotal and total disagree is exactly what an assurance
    harness exists to report. Raising here would delete the evidence and leave a
    run looking like a parse failure instead of a wrong outcome.
    """
    payload = cart(total_price=1)

    cart_state = adapter().normalize(payload, PROVENANCE).payload["cart"]

    assert cart_state["totals_consistent"] is False


def test_a_line_total_that_is_not_price_times_quantity_is_inconsistent() -> None:
    payload = cart(
        items=[{"variant_id": int(VARIANT), "quantity": 2, "price": 2500, "line_price": 2500}],
        item_count=2,
    )

    assert adapter().normalize(payload, PROVENANCE).payload["cart"]["totals_consistent"] is False


# --- refusals (FR-114, 011-T10) ----------------------------------------------


def test_a_cart_that_cannot_say_whether_checkout_was_reached_is_refused() -> None:
    """FR-114 makes checkout navigation a failed trial, never a pass.

    So an observation that cannot speak to it is not evidence for this contract.
    Defaulting to `false` would make "the bridge did not look" indistinguishable
    from "nothing navigated" — the harness asserting, on nobody's authority,
    that no checkout happened.
    """
    payload = cart()
    del payload["page"]

    with pytest.raises(AuditObservationError):
        adapter().normalize(payload, PROVENANCE)


@pytest.mark.parametrize("value", ["false", 0, None, "no"])
def test_a_non_boolean_checkout_fact_is_refused(value: object) -> None:
    with pytest.raises(AuditObservationError):
        adapter().normalize(cart(page={"checkout_navigation_observed": value}), PROVENANCE)


def test_an_observed_checkout_navigation_is_recorded_rather_than_hidden() -> None:
    observed = adapter().normalize(cart(page={"checkout_navigation_observed": True}), PROVENANCE)

    assert observed.payload["page"]["checkout_navigation_observed"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"item_count": 0},
        {"items": []},
        {"items": "not-a-list", "item_count": 0},
        {"items": ["not-an-object"], "item_count": 1},
        {"items": [{"quantity": 1, "price": 1}], "item_count": 1},
    ],
    ids=["empty", "no-items", "no-count", "items-not-a-list", "line-not-an-object", "no-variant"],
)
def test_a_malformed_cart_is_refused(payload: dict[str, Any]) -> None:
    """An unreadable payload is `observation_unavailable`, never a pass."""
    with pytest.raises(AuditObservationError):
        adapter().normalize(payload, PROVENANCE)


def test_a_missing_currency_is_refused() -> None:
    """`/cart.js` is locale-aware, so money without a currency means nothing."""
    payload = cart()
    del payload["currency"]

    with pytest.raises(AuditObservationError):
        adapter().normalize(payload, PROVENANCE)


def test_an_oversized_submission_is_refused_before_it_is_parsed() -> None:
    """FR-117's 256 KiB bound, applied to the raw bytes.

    It has to be the raw bytes: by the time `normalize` sees a payload it is a
    parsed object, and the decode this limit exists to prevent has happened. The
    guard therefore lives beside the normalizer it protects, and the submitting
    route calls it on the body it read.
    """
    oversized = json.dumps(cart(note="x" * MAX_CART_PAYLOAD_BYTES)).encode("utf-8")

    require_within_payload_bound(json.dumps(cart()).encode("utf-8"))
    with pytest.raises(AuditObservationError):
        require_within_payload_bound(oversized)


# --- the locked configuration -------------------------------------------------


def test_the_contract_parameters_come_from_the_adapters_configuration() -> None:
    """Server-controlled by construction (FR-110, project rules).

    A route that gathered the variant and currency from a request body would be
    letting the caller choose which variant counted as correct.
    """
    assert adapter().contract_parameters() == {
        "variant_id": VARIANT,
        "expected_currency": "USD",
    }


def test_the_projection_is_deterministic() -> None:
    """Two identical payloads normalize identically, so the content hash is stable."""
    first = project_cart(cart(), test_variant_id=VARIANT, store_origin=ORIGIN)
    second = project_cart(cart(), test_variant_id=VARIANT, store_origin=ORIGIN)

    assert first == second
