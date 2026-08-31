"""Adapter-conformance lane (spec §26.7).

The lane's whole premise is that adapters are reachable through target-neutral
protocols, so the precondition worth asserting now is that the ports package is
importable with no integration, demo, or commerce package involved. If that ever
stops being true, every adapter conformance test built on it is compromised.
"""

import importlib

import pytest

TARGET_SPECIFIC_MODULES = (
    "integrations",
    "buggy_store",
    "shopify",
    "fastapi",
    "httpx",
)


@pytest.mark.adapters
def test_core_ports_import_without_any_integration_present() -> None:
    ports = importlib.import_module("actionwitness_core.ports")
    assert ports is not None


@pytest.mark.adapters
def test_importing_core_ports_pulls_in_no_target_specific_module(reimported_core) -> None:
    """Importing the protocol surface must not drag in a target or a web framework.

    The AST gate in tests/architecture proves the *source* declares no such
    import; this proves the *runtime* graph agrees, which also catches a lazy
    import hidden inside a function body.
    """
    import sys

    reimported_core("actionwitness_core.ports")

    leaked = [
        module
        for module in TARGET_SPECIFIC_MODULES
        if any(name == module or name.startswith(f"{module}.") for name in sys.modules)
    ]
    assert leaked == [], f"importing core ports pulled in {leaked}"


@pytest.mark.adapters
def test_the_adapter_lane_has_a_deterministic_identifier_source(id_sequence) -> None:
    """Adapter conformance replays must not depend on random correlation IDs."""
    assert id_sequence.next("correlation") == "correlation-0001"
