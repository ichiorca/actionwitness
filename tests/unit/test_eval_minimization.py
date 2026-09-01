"""007-T3 — safe minimization (§24.2 steps 2–4).

Minimization is where a regression case quietly stops testing what it was cut
for, so these tests are mostly about what is *kept*. The cost of keeping too
much is a bigger file; the cost of dropping too much is a case that passes for a
reason nobody intended — worse than having no case.

`test_a_no_undeclared_changes_contract_keeps_the_whole_state` is the sharpest:
that policy is defined over paths the contract never names, so a pruned fixture
would make it pass vacuously on every replay, forever, while looking checked.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.models import OutcomeContract
from actionwitness_core.evals.minimize import (
    minimize_fixture,
    prune_trajectory,
    referenced_roots,
    requires_complete_state,
)

pytestmark = pytest.mark.unit

STATE = {
    "cart": {"items": {}, "total": "0.00"},
    "order": {"created": False},
    "preferences": {"currency": "USD"},
}

READ_ONLY = frozenset({"search_catalog", "get_cart"})


def _contract(**overrides: object) -> OutcomeContract:
    document: dict[str, object] = {
        "schema_version": "1.0",
        "name": "one-mug",
        "target_id": "buggy-store",
        "intent": "Add one mug and apply SAVE20 without creating an order.",
        "assertions": [
            {
                "id": "total",
                "path": "target.cart.total",
                "operator": "equals",
                "value": "20.00",
                "severity": "critical",
            }
        ],
    }
    document.update(overrides)
    return OutcomeContract.model_validate(document)


# --- the fixture -------------------------------------------------------------


def test_the_fixture_keeps_the_subtrees_the_contract_names() -> None:
    # Arrange
    contract = _contract()

    # Act
    kept, complete = minimize_fixture(STATE, contract)

    # Assert
    assert set(kept) == {"cart"}
    assert complete is False


def test_preconditions_count_as_references() -> None:
    """A replay whose precondition cannot be evaluated cannot start, and would
    fail for a reason unrelated to the regression."""
    # Arrange
    contract = _contract(
        preconditions=[{"path": "target.order.created", "operator": "equals", "value": False}]
    )

    # Act
    kept, _ = minimize_fixture(STATE, contract)

    # Assert
    assert set(kept) == {"cart", "order"}


def test_a_no_undeclared_changes_contract_keeps_the_whole_state() -> None:
    """§24.2 step 2's exception.

    The policy is defined over paths the contract does not name, so a pruned
    fixture would make it pass vacuously on every replay while looking checked.
    """
    # Arrange
    contract = _contract(policies=[{"type": "no_undeclared_changes"}])

    # Act
    kept, complete = minimize_fixture(STATE, contract)

    # Assert
    assert kept == STATE
    assert complete is True
    assert requires_complete_state(contract) is True


def test_a_contract_naming_no_target_paths_keeps_everything() -> None:
    """ "The contract named no paths" is not the same fact as "the target starts
    empty", and a fixture pruned to nothing would restore no starting point."""
    # Arrange — a policy-only contract, which §10 permits.
    contract = _contract(
        assertions=[], policies=[{"type": "idempotent_by_request_id", "tool": "update_cart"}]
    )

    # Act
    kept, complete = minimize_fixture(STATE, contract)

    # Assert
    assert kept == STATE
    assert complete is True
    assert referenced_roots(contract) == frozenset()


# --- the trajectory ----------------------------------------------------------


def test_a_read_only_call_before_a_mutation_is_kept() -> None:
    """§24.2 step 3 allows dropping one only when its presence, output *and*
    ordering are irrelevant. A search that produced the id a later cart change
    used is none of those, and nothing here can prove otherwise from recorded
    arguments — so it stays.
    """
    # Arrange
    steps = [
        (1, "search_catalog", {"query": "mug"}),
        (2, "update_cart", {"product_id": "mug-ceramic-001", "quantity": 1}),
    ]

    # Act
    kept = prune_trajectory(steps, _contract(), READ_ONLY)

    # Assert
    assert [tool for _, tool, _ in kept] == ["search_catalog", "update_cart"]


def test_a_trailing_read_only_call_is_dropped() -> None:
    """Nothing after it could have consumed its output, nothing asserts on it,
    and no policy names it."""
    # Arrange
    steps = [
        (1, "update_cart", {"product_id": "mug-ceramic-001", "quantity": 1}),
        (2, "get_cart", {}),
    ]

    # Act
    kept = prune_trajectory(steps, _contract(), READ_ONLY)

    # Assert
    assert [tool for _, tool, _ in kept] == ["update_cart"]


def test_a_read_only_call_the_contract_expects_is_kept() -> None:
    """`expected_tools` makes the call part of the judged trajectory, so
    dropping it would change the verdict the case reproduces."""
    # Arrange
    contract = _contract(expected_tools={"ordered": False, "calls": ["get_cart"]})
    steps = [(1, "update_cart", {"quantity": 1}), (2, "get_cart", {})]

    # Act
    kept = prune_trajectory(steps, contract, READ_ONLY)

    # Assert
    assert [tool for _, tool, _ in kept] == ["update_cart", "get_cart"]


def test_a_mutation_is_never_dropped() -> None:
    # Arrange
    steps = [(1, "update_cart", {"quantity": 1}), (2, "apply_discount", {"code": "SAVE20"})]

    # Act
    kept = prune_trajectory(steps, _contract(), READ_ONLY)

    # Assert
    assert len(kept) == 2


def test_repeated_request_ids_survive() -> None:
    """§24.2 step 4. An idempotency failure *is* a repeated request id;
    minimizing it away deletes the bug the case exists to reproduce."""
    # Arrange
    steps = [
        (1, "update_cart", {"quantity": 1, "request_id": "req_same"}),
        (2, "update_cart", {"quantity": 1, "request_id": "req_same"}),
    ]

    # Act
    kept = prune_trajectory(steps, _contract(), READ_ONLY)

    # Assert
    assert [arguments["request_id"] for _, _, arguments in kept] == ["req_same", "req_same"]


def test_surviving_steps_are_renumbered_densely() -> None:
    """The case model requires 1..n with no gaps, and a gap would itself be
    evidence of a step nobody meant to drop."""
    # Arrange
    steps = [
        (1, "update_cart", {"quantity": 1}),
        (2, "get_cart", {}),
        (3, "get_cart", {}),
    ]

    # Act
    kept = prune_trajectory(steps, _contract(), READ_ONLY)

    # Assert
    assert [sequence for sequence, _, _ in kept] == [1]


def test_nothing_is_dropped_when_the_adapter_declares_no_read_only_tools() -> None:
    """The safe direction when adapter metadata is unavailable: a trajectory
    kept whole always replays."""
    # Arrange
    steps = [(1, "update_cart", {"quantity": 1}), (2, "get_cart", {})]

    # Act
    kept = prune_trajectory(steps, _contract(), frozenset())

    # Assert
    assert len(kept) == 2
