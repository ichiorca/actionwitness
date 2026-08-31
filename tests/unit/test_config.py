"""Feature-flag gates (spec v1.9 §29.1; BUILD_ORDER invariant 12; AC-7).

The requirement is narrow and easy to violate by accident: an absent optional
configuration must disable **only** that module. So the central tests here are
combinatorial — for each module, take a fully configured environment, remove that
module's configuration, and assert every other module is untouched.

The second requirement is that misconfiguration fails *closed and visibly*.
Construction never raises, a rejected module is absent rather than half-enabled,
and it always carries a reason an operator can act on.
"""

import os

import pytest
from actionwitness_service.config import (
    DEFAULT_BUGGY_STORE_BASE_URL,
    MODULE_NAMES,
    ModuleStatus,
    ServiceSettings,
)

FULL_ENV: dict[str, str] = {
    "BUGGY_STORE_ENABLED": "true",
    "BUGGY_STORE_BASE_URL": "http://127.0.0.1:8001/demo/api/v1",
    "EVALUATOR_IMPORT_ENABLED": "true",
    "LIVE_EVALUATOR_ENABLED": "true",
    "LIVE_EVALUATOR_PROVIDER": "google",
    "LIVE_EVALUATOR_MODEL": "gemini-test",
    "LIVE_EVALUATOR_CREDENTIAL_VAR": "GOOGLE_AI",
    "GOOGLE_AI": "test-credential-value",
    "HARNESS_PUBLIC_ORIGIN": "http://localhost:8000",
    "SHOPIFY_STORE_ORIGIN": "https://dev-store.myshopify.com",
    "SHOPIFY_TEST_VARIANT_ID": "1234567890",
    "SHOPIFY_EXPECTED_CURRENCY": "USD",
    "EXTERNAL_AUDIT_ENABLED": "true",
    "EXTERNAL_AUDIT_ALLOWED_ORIGINS": "https://audited.example, https://other.example",
}

MODULE_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "buggy_store": ("BUGGY_STORE_ENABLED", "BUGGY_STORE_BASE_URL"),
    "evaluator_import": ("EVALUATOR_IMPORT_ENABLED",),
    "live_evaluator": (
        "LIVE_EVALUATOR_ENABLED",
        "LIVE_EVALUATOR_PROVIDER",
        "LIVE_EVALUATOR_MODEL",
        "LIVE_EVALUATOR_CREDENTIAL_VAR",
        "GOOGLE_AI",
    ),
    "shopify": (
        "HARNESS_PUBLIC_ORIGIN",
        "SHOPIFY_STORE_ORIGIN",
        "SHOPIFY_TEST_VARIANT_ID",
        "SHOPIFY_EXPECTED_CURRENCY",
    ),
    "external_audit": ("EXTERNAL_AUDIT_ENABLED", "EXTERNAL_AUDIT_ALLOWED_ORIGINS"),
}

#: Modules that are on unless switched off. Removing their configuration leaves
#: them enabled at defaults, which is itself the credential-free path.
DEFAULT_ON = {"buggy_store", "evaluator_import"}


def _without(*keys: str) -> dict[str, str]:
    return {key: value for key, value in FULL_ENV.items() if key not in keys}


# --- the empty environment --------------------------------------------------


@pytest.mark.unit
def test_empty_environment_never_raises() -> None:
    """A missing .env is the common case and must not be a startup failure."""
    settings = ServiceSettings.from_env({})
    assert {state.name for state in settings.modules} == set(MODULE_NAMES)


@pytest.mark.unit
def test_empty_environment_keeps_the_credential_free_path_running() -> None:
    """BUILD_ORDER invariant 12: optional integrations cannot take the demo down."""
    settings = ServiceSettings.from_env({})
    assert settings.is_enabled("buggy_store")
    assert settings.buggy_store is not None
    assert settings.buggy_store.base_url == DEFAULT_BUGGY_STORE_BASE_URL
    assert settings.is_enabled("evaluator_import")


@pytest.mark.unit
@pytest.mark.parametrize("name", ["live_evaluator", "shopify", "external_audit"])
def test_credentialed_modules_are_off_by_default(name: str) -> None:
    """Anything needing a credential or an external origin must opt in explicitly."""
    settings = ServiceSettings.from_env({})
    state = settings.module(name)
    assert state.status is ModuleStatus.DISABLED
    assert state.reason.strip(), f"{name} gives no guidance for why it is off"


@pytest.mark.unit
def test_fully_configured_environment_enables_everything() -> None:
    settings = ServiceSettings.from_env(FULL_ENV)
    disabled = [s.name for s in settings.modules if not s.is_enabled]
    assert disabled == [], f"expected every module enabled, got {disabled} off"


# --- absence disables only its own module -----------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("removed", MODULE_NAMES)
def test_absent_configuration_disables_only_its_own_module(removed: str) -> None:
    settings = ServiceSettings.from_env(_without(*MODULE_ENV_KEYS[removed]))

    others = {name: settings.is_enabled(name) for name in MODULE_NAMES if name != removed}
    assert all(others.values()), f"removing {removed} config also disabled {others}"

    if removed in DEFAULT_ON:
        assert settings.is_enabled(removed), f"{removed} should stay on at its defaults"
    else:
        assert not settings.is_enabled(removed)


@pytest.mark.unit
@pytest.mark.parametrize("removed", MODULE_NAMES)
def test_disabled_module_exposes_no_settings_object(removed: str) -> None:
    """A module that is off must be absent, never present-but-empty."""
    settings = ServiceSettings.from_env(_without(*MODULE_ENV_KEYS[removed]))
    if not settings.is_enabled(removed):
        assert getattr(settings, removed) is None


# --- misconfiguration fails closed and visibly ------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,overrides,expected_in_reason",
    [
        ("buggy_store", {"BUGGY_STORE_ENABLED": "maybe"}, "boolean"),
        ("buggy_store", {"BUGGY_STORE_BASE_URL": "ftp://host"}, "http"),
        ("buggy_store", {"BUGGY_STORE_BASE_URL": "not-a-url"}, "http"),
        ("live_evaluator", {"LIVE_EVALUATOR_MODEL": ""}, "LIVE_EVALUATOR_MODEL"),
        ("live_evaluator", {"GOOGLE_AI": ""}, "not set"),
        ("shopify", {"SHOPIFY_STORE_ORIGIN": "http://dev-store.myshopify.com"}, "https"),
        (
            "shopify",
            {"SHOPIFY_STORE_ORIGIN": "https://dev-store.myshopify.com/cart"},
            "bare origin",
        ),
        ("shopify", {"SHOPIFY_EXPECTED_CURRENCY": "dollars"}, "currency"),
        ("shopify", {"SHOPIFY_TEST_VARIANT_ID": ""}, "SHOPIFY_TEST_VARIANT_ID"),
        ("external_audit", {"EXTERNAL_AUDIT_ALLOWED_ORIGINS": ""}, "empty"),
        ("external_audit", {"EXTERNAL_AUDIT_ALLOWED_ORIGINS": "http://insecure.example"}, "https"),
    ],
)
def test_invalid_configuration_disables_only_that_module_with_a_reason(
    name: str, overrides: dict[str, str], expected_in_reason: str
) -> None:
    settings = ServiceSettings.from_env({**FULL_ENV, **overrides})

    state = settings.module(name)
    assert state.status is ModuleStatus.MISCONFIGURED, (
        f"{name} with {overrides} should be misconfigured, got {state.status}"
    )
    assert expected_in_reason.lower() in state.reason.lower(), (
        f"{name} reason {state.reason!r} does not mention {expected_in_reason!r}"
    )
    assert getattr(settings, name) is None, f"{name} is misconfigured but still exposes settings"

    others = [other for other in MODULE_NAMES if other != name and not settings.is_enabled(other)]
    assert others == [], f"misconfiguring {name} also disabled {others}"


@pytest.mark.unit
def test_misconfiguration_is_distinguished_from_deliberate_absence() -> None:
    """A typo and a cut feature must not look identical to an operator."""
    typo = ServiceSettings.from_env({**FULL_ENV, "SHOPIFY_EXPECTED_CURRENCY": "US"})
    absent = ServiceSettings.from_env(_without(*MODULE_ENV_KEYS["shopify"]))

    assert typo.module("shopify").status is ModuleStatus.MISCONFIGURED
    assert absent.module("shopify").status is ModuleStatus.DISABLED


@pytest.mark.unit
def test_partial_shopify_configuration_is_misconfigured_not_silently_ignored() -> None:
    partial = _without("SHOPIFY_TEST_VARIANT_ID", "SHOPIFY_EXPECTED_CURRENCY")
    state = ServiceSettings.from_env(partial).module("shopify")
    assert state.status is ModuleStatus.MISCONFIGURED
    assert "SHOPIFY_TEST_VARIANT_ID" in state.reason


# --- origin normalization ---------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://dev-store.myshopify.com", "https://dev-store.myshopify.com"),
        ("https://dev-store.myshopify.com/", "https://dev-store.myshopify.com"),
        ("https://Dev-Store.MyShopify.com", "https://dev-store.myshopify.com"),
        ("https://dev-store.myshopify.com:443", "https://dev-store.myshopify.com:443"),
    ],
)
def test_store_origin_is_normalized_to_an_exact_origin(raw: str, expected: str) -> None:
    """Origins are compared by equality for CORS, so normalization is a safety step."""
    settings = ServiceSettings.from_env({**FULL_ENV, "SHOPIFY_STORE_ORIGIN": raw})
    assert settings.shopify is not None
    assert settings.shopify.store_origin == expected


@pytest.mark.unit
def test_audit_allowlist_deduplicates_and_keeps_order() -> None:
    settings = ServiceSettings.from_env(
        {
            **FULL_ENV,
            "EXTERNAL_AUDIT_ALLOWED_ORIGINS": "https://a.example,https://b.example,https://a.example/",
        }
    )
    assert settings.external_audit is not None
    assert settings.external_audit.allowed_origins == ("https://a.example", "https://b.example")


@pytest.mark.unit
def test_origin_with_embedded_credentials_is_rejected() -> None:
    settings = ServiceSettings.from_env(
        {**FULL_ENV, "SHOPIFY_STORE_ORIGIN": "https://user:pass@dev-store.myshopify.com"}
    )
    assert settings.module("shopify").status is ModuleStatus.MISCONFIGURED


# --- secrets stay out -------------------------------------------------------


@pytest.mark.unit
def test_settings_record_the_credential_name_but_never_its_value() -> None:
    """FR-099: settings reach logs and health output; a value there would leak."""
    settings = ServiceSettings.from_env(FULL_ENV)
    assert settings.live_evaluator is not None
    assert settings.live_evaluator.credential_var == "GOOGLE_AI"

    serialized = settings.model_dump_json()
    assert "test-credential-value" not in serialized
    assert "test-credential-value" not in repr(settings)
    assert "GOOGLE_AI" in serialized, "the variable *name* is safe and useful to report"


@pytest.mark.unit
def test_no_module_reason_leaks_a_credential_value() -> None:
    settings = ServiceSettings.from_env(FULL_ENV)
    for state in settings.modules:
        assert "test-credential-value" not in state.reason


# --- determinism ------------------------------------------------------------


@pytest.mark.unit
def test_resolution_reads_only_the_injected_mapping() -> None:
    """No hidden os.environ read, so every combination above is actually isolated."""
    marker = "AW_CONFIG_ISOLATION_PROBE"
    os.environ[marker] = "1"
    try:
        first = ServiceSettings.from_env({})
        os.environ["SHOPIFY_STORE_ORIGIN"] = "https://leaked.example"
        second = ServiceSettings.from_env({})
    finally:
        os.environ.pop(marker, None)
        os.environ.pop("SHOPIFY_STORE_ORIGIN", None)

    assert first == second
    assert second.shopify is None
