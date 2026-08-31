"""Shopify lane (spec §26.6, Tier 3).

Everything about this module is narrow by design: one exact authorized
development-store origin, cart-only, no Admin credential. The parts assertable
before M10 are the fail-closed ones, and they are the parts that matter most —
an over-permissive origin check is a security defect, not a missing feature.
"""

import pytest
from actionwitness_service.config import ModuleStatus

CONFIGURED = {
    "HARNESS_PUBLIC_ORIGIN": "http://localhost:8000",
    "SHOPIFY_STORE_ORIGIN": "https://dev-store.myshopify.com",
    "SHOPIFY_TEST_VARIANT_ID": "42",
    "SHOPIFY_EXPECTED_CURRENCY": "USD",
}


@pytest.mark.shopify
def test_the_module_is_absent_rather_than_half_configured(build_settings) -> None:
    settings = build_settings({})
    assert settings.module("shopify").status is ModuleStatus.DISABLED
    assert settings.shopify is None


@pytest.mark.shopify
def test_an_unavailable_store_never_disables_the_buggy_store_path(build_settings) -> None:
    """Locked decision 26 and BUILD_ORDER invariant 12: Tier 3 cannot take Tier 1 down."""
    settings = build_settings({**CONFIGURED, "SHOPIFY_STORE_ORIGIN": "not-an-origin"})
    assert settings.module("shopify").status is ModuleStatus.MISCONFIGURED
    assert settings.is_enabled("buggy_store")
    assert settings.is_enabled("evaluator_import")


@pytest.mark.shopify
@pytest.mark.parametrize(
    "origin",
    [
        "http://dev-store.myshopify.com",  # not HTTPS
        "https://dev-store.myshopify.com/cart",  # carries a path
        "https://dev-store.myshopify.com?x=1",  # carries a query
        "*",  # wildcard
        "null",  # the literal null origin
        "https://user:pass@dev-store.myshopify.com",  # embedded credentials
    ],
)
def test_unconfigured_wildcard_and_null_origins_are_all_rejected(
    build_settings, origin: str
) -> None:
    """§26.6 requires rejecting each of these; equality-compared CORS depends on it."""
    settings = build_settings({**CONFIGURED, "SHOPIFY_STORE_ORIGIN": origin})
    assert settings.module("shopify").status is ModuleStatus.MISCONFIGURED
    assert settings.shopify is None


@pytest.mark.shopify
def test_the_configured_origin_is_stored_exactly_and_server_side(build_settings) -> None:
    """Store origin, variant, and currency are server-controlled, never client-supplied."""
    settings = build_settings(CONFIGURED)
    assert settings.shopify is not None
    assert settings.shopify.store_origin == "https://dev-store.myshopify.com"
    assert settings.shopify.test_variant_id == "42"
    assert settings.shopify.expected_currency == "USD"
