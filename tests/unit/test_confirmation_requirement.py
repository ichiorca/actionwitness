"""006-T1 — which actions need a human is read from the contract (§14, FR-060).

The integration suite cannot express the negative case: every built-in Buggy
Store template protects `proceed_to_checkout`, so "a contract that does not
protect this tool" is not a state the shipped templates can reach. That is
exactly the case worth pinning, because without it a harness that gated a
hardcoded list of tool names would pass every end-to-end test while making the
contract's `requires_confirmation` policy decorative.
"""

from __future__ import annotations

import pytest
from actionwitness_service.application.confirmation_service import confirmation_requirement

pytestmark = pytest.mark.unit

CHECKOUT = "proceed_to_checkout"


def _contract(*policies: dict) -> dict:
    return {"policies": list(policies)}


def test_a_protected_tool_yields_its_configured_timeout() -> None:
    # Arrange
    document = _contract({"type": "requires_confirmation", "tool": CHECKOUT, "timeout_seconds": 45})

    # Act
    requirement = confirmation_requirement(document, CHECKOUT)

    # Assert
    assert requirement is not None
    assert requirement.tool == CHECKOUT
    assert requirement.timeout_seconds == 45


def test_a_policy_naming_another_tool_does_not_protect_this_one() -> None:
    """The failure a hardcoded tool list would hide."""
    # Arrange
    document = _contract({"type": "requires_confirmation", "tool": "update_cart"})

    # Act / Assert
    assert confirmation_requirement(document, CHECKOUT) is None


def test_a_contract_with_no_confirmation_policy_protects_nothing() -> None:
    # Arrange
    document = _contract({"type": "idempotent_by_request_id", "tool": CHECKOUT})

    # Act / Assert
    assert confirmation_requirement(document, CHECKOUT) is None


@pytest.mark.parametrize("document", [None, {}, {"policies": []}, {"policies": None}])
def test_an_absent_contract_or_policy_list_protects_nothing(document: dict | None) -> None:
    """A run can be armed without a contract; it must not crash the gate check,
    and it must not invent a gate either."""
    # Act / Assert
    assert confirmation_requirement(document, CHECKOUT) is None


def test_the_default_timeout_applies_when_the_policy_omits_one() -> None:
    """FR-062 fixes the default at 60 seconds."""
    # Arrange
    document = _contract({"type": "requires_confirmation", "tool": CHECKOUT})

    # Act
    requirement = confirmation_requirement(document, CHECKOUT)

    # Assert
    assert requirement is not None and requirement.timeout_seconds == 60


def test_a_malformed_policy_entry_is_skipped_rather_than_crashing() -> None:
    """Persisted JSON is untrusted input (constitution §5), and a contract row
    that somehow held a string in its policy list must not take the invocation
    path down with it."""
    # Arrange
    document = {"policies": ["not-a-policy", {"type": "requires_confirmation", "tool": CHECKOUT}]}

    # Act
    requirement = confirmation_requirement(document, CHECKOUT)

    # Assert
    assert requirement is not None
