"""004-T10 — a missing optional target is a bounded state, not a crash.

§21.1 and BUILD_ORDER invariant 12: "optional integrations fail closed and
cannot prevent the credential-free Buggy Store path from running", and the
harness must start with the Buggy Store package **absent from the environment
entirely**, not merely switched off.

The important test is `test_the_service_starts_with_the_integration_uninstalled`.
Every other test here checks a configuration flag, which a service could honour
while still importing the package at startup and dying if it were missing. That
test removes the module and asserts the application still serves — which is the
only version of §21.1 that is actually about deployment.
"""

from __future__ import annotations

import builtins
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import create_app
from actionwitness_service.api.errors import ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry, TargetUnavailable
from actionwitness_service.config import ModuleStatus, ServiceSettings
from fastapi import FastAPI

from integrations.buggy_store import ADAPTER_ID

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENABLED = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"}
DISABLED = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}
MISCONFIGURED = {
    "HARNESS_ENV": "local",
    "BUGGY_STORE_ENABLED": "true",
    "BUGGY_STORE_BASE_URL": "not-a-url",
}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url="http://127.0.0.1:8001")


async def test_an_enabled_target_yields_an_adapter() -> None:
    # Arrange
    settings = ServiceSettings.from_env(ENABLED)

    # Act
    async with _client() as client:
        registry = AdapterRegistry(settings, client=client)

        # Assert
        assert registry.is_available("buggy_store")
        adapter = registry.adapter("buggy_store")
        assert adapter.adapter_id == ADAPTER_ID


async def test_a_disabled_target_is_unavailable_with_a_reason() -> None:
    """ "Not an error" — `disabled` is a deliberate choice, and the report says so."""
    # Arrange
    settings = ServiceSettings.from_env(DISABLED)

    # Act
    async with _client() as client:
        registry = AdapterRegistry(settings, client=client)

        # Assert
        assert not registry.is_available("buggy_store")
        assert registry.slot("buggy_store").state.status is ModuleStatus.DISABLED
        with pytest.raises(TargetUnavailable) as caught:
            registry.adapter("buggy_store")

    assert caught.value.code is ApiErrorCode.TARGET_UNAVAILABLE
    assert "off" in caught.value.message.lower()


async def test_a_misconfigured_target_is_distinguished_from_a_disabled_one() -> None:
    """An operator who mistyped a base URL needs to see a mistake, not an
    absence. Collapsing the two is how a broken config gets read as a cut
    feature."""
    # Arrange
    settings = ServiceSettings.from_env(MISCONFIGURED)

    # Act
    async with _client() as client:
        registry = AdapterRegistry(settings, client=client)

    # Assert
    slot = registry.slot("buggy_store")
    assert slot.state.status is ModuleStatus.MISCONFIGURED
    assert not slot.is_available
    assert "BUGGY_STORE_BASE_URL" in slot.state.reason


async def test_the_refusal_is_the_standard_envelope_not_a_traceback() -> None:
    """§26.7: "a missing adapter yields a clear unavailable state, not a process
    failure." An `ApiError` is what makes that reach a client as §15.8's shape."""
    # Arrange
    settings = ServiceSettings.from_env(DISABLED)
    registry = AdapterRegistry(settings)

    # Act
    with pytest.raises(TargetUnavailable) as caught:
        registry.adapter("buggy_store")

    # Assert
    envelope = caught.value.as_envelope()
    assert envelope["error"]["code"] == "TARGET_UNAVAILABLE"
    assert envelope["error"]["retryable"] is False


async def test_an_unknown_target_refuses_rather_than_raising_a_key_error() -> None:
    # Arrange
    registry = AdapterRegistry(ServiceSettings.from_env(DISABLED))

    # Act / Assert
    with pytest.raises(TargetUnavailable):
        registry.adapter("a_target_nobody_registered")
    assert not registry.is_available("a_target_nobody_registered")


async def test_the_capability_report_lists_unavailable_targets_too() -> None:
    """A bar showing only what works makes a misconfiguration look like a
    feature that was never built (§29.1)."""
    # Arrange
    registry = AdapterRegistry(ServiceSettings.from_env(DISABLED))

    # Act
    report = registry.capability_report()

    # Assert
    assert "buggy_store" in report
    assert report["buggy_store"]["status"] == "disabled"
    assert report["buggy_store"]["reason"]


# --- the deployment case §21.1 actually names -------------------------------


@pytest.fixture
def integration_uninstalled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import integrations.buggy_store` fail, as an absent package does.

    The already-imported modules are removed too — pytest imports them during
    collection, so leaving them in `sys.modules` would let the import succeed
    from cache and the test would pass without ever exercising the branch.
    """
    for name in list(sys.modules):
        if name == "integrations" or name.startswith("integrations."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    real_import: Callable[..., Any] = builtins.__import__

    def refusing_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "integrations" or name.startswith("integrations."):
            raise ImportError(f"No module named {name!r}", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refusing_import)


async def test_a_missing_package_is_reported_as_not_installed(
    integration_uninstalled: None,
) -> None:
    """The import failure must be a status, not a traceback."""
    # Arrange
    settings = ServiceSettings.from_env(ENABLED)

    # Act
    async with _client() as client:
        registry = AdapterRegistry(settings, client=client)

    # Assert
    slot = registry.slot("buggy_store")
    assert slot.state.status is ModuleStatus.DISABLED
    assert "not installed" in slot.state.reason
    assert not slot.is_available


@pytest.fixture
async def app_without_the_integration(
    tmp_path: Path, integration_uninstalled: None
) -> AsyncIterator[FastAPI]:
    application = create_app(environ=ENABLED, database_path=tmp_path / "harness.sqlite3")
    async with application.router.lifespan_context(application):
        yield application


async def test_the_service_starts_with_the_integration_uninstalled(
    app_without_the_integration: FastAPI,
) -> None:
    """§21.1's real requirement, and the one a configuration flag cannot prove.

    The package is genuinely unimportable here. The application must still
    complete its lifespan, run its migrations, serve requests, and issue
    workspaces.
    """
    # Arrange
    app = app_without_the_integration
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)

    # Act
    async with httpx.AsyncClient(transport=transport, base_url="https://harness.test") as visitor:
        health = await visitor.get("/healthz")

    # Assert
    assert health.status_code == 200
    assert app.state.schema_version >= 1
    assert not app.state.adapters.is_available("buggy_store")


async def test_one_failed_integration_does_not_disable_the_others() -> None:
    """§21.1. Registration is guarded per entry, not once around the loop.

    Every registered target still has a slot after one of them fails, and the
    settings for the others are untouched — which is what "one failed
    integration never disables the others" means when only one target exists so
    far.
    """
    # Arrange — an extra registration that raises, added to a real registry.
    settings = ServiceSettings.from_env(ENABLED)

    async with _client() as client:
        registry = AdapterRegistry(settings, client=client)

        def explode() -> Callable[[], Any]:
            raise RuntimeError("this integration is broken")

        # Act — `evaluator_import` is a real module that is enabled by default,
        # standing in for a second target whose preparation fails.
        registry._register("evaluator_import", explode)
        registry._register("buggy_store", registry._build_buggy_store)

        # Assert — the broken one is bounded, the working one still works.
        assert registry.slot("evaluator_import").state.status is ModuleStatus.MISCONFIGURED
        assert registry.is_available("buggy_store")
        assert registry.adapter("buggy_store").adapter_id == ADAPTER_ID


async def test_a_broken_integrations_reason_names_no_internal_detail() -> None:
    """§20: an exception's text is where a path or a credential leaks."""
    # Arrange
    registry = AdapterRegistry(ServiceSettings.from_env(DISABLED))

    def explode() -> Callable[[], Any]:
        raise RuntimeError("failed to reach https://user:hunter2@internal.example/db")

    # Act
    registry._register("evaluator_import", explode)

    # Assert — the type is named, the message is not.
    reason = registry.slot("evaluator_import").state.reason
    assert "RuntimeError" in reason
    assert "hunter2" not in reason
    assert "internal.example" not in reason
