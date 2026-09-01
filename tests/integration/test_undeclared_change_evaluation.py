"""013-T3 — `no_undeclared_changes` actually evaluates.

Until now the policy honestly reported `not_evaluated`: the partition existed,
the classification existed, the report block existed, and nothing produced the
changed-path set they all needed. This is the wiring that closes that, and these
tests are about the two ways wiring like this goes wrong.

The first is that it silently keeps not evaluating. `not_evaluated` is a
*correct* answer for a run with no snapshots, so a policy that never evaluates
anything looks exactly like a policy that is behaving properly — which is why
every test below asserts the status it expects rather than merely asserting the
run's overall result.

The second is that the report and the finding disagree. The block is derived
from the finding and the same diff the finding judged, never from a second diff
computed at report time, and `test_the_block_and_the_finding_never_disagree`
is what holds that.

These drive `_evaluate` and `_compose` with real contracts, a real adapter and
real observation documents. The full journey through the HTTP API is 013-T7's
exit gate, which needs the store template 013-T5 adds — there is no endpoint for
submitting an arbitrary contract, by design.
"""

from __future__ import annotations

from typing import Any

import pytest
from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_service.application.verification_service import (
    _evaluate,
    _undeclared_changes_block,
)

from integrations.buggy_store import TARGET_ID, BuggyStoreAdapter

pytestmark = pytest.mark.integration


def _contract(*, policies: list[dict[str, Any]], assertions: list[dict[str, Any]]) -> Any:
    return parse_contract(
        {
            "schema_version": "1.0",
            "name": "undeclared-change-probe",
            "description": "A contract that cares about blast radius.",
            "target_id": TARGET_ID,
            "intent": "Add one mug and change nothing else.",
            "assertions": assertions,
            "policies": policies,
        }
    )


CART_ASSERTION: list[dict[str, Any]] = [
    {
        "id": "mug-quantity",
        "path": "target.cart.items.mug.quantity",
        "operator": "equals",
        "value": 1,
    }
]

POLICY: list[dict[str, Any]] = [{"type": "no_undeclared_changes"}]


def _state(*, quantity: int, note: str | None = None) -> dict[str, Any]:
    """An evaluation context shaped like the store's canonical document (§13.2)."""
    preferences: dict[str, Any] = {}
    if note is not None:
        preferences["delivery_note"] = note
    return {
        "target": {
            "cart": {
                "items": {"mug": {"product_id": "mug-ceramic-001", "quantity": quantity}},
                "subtotal": "25.00",
                "total": "25.00",
            },
            "order": {"created": False, "order_id": None},
            "preferences": preferences,
        }
    }


def _adapter() -> BuggyStoreAdapter:
    """The real adapter, for its real §13.4 effect map.

    A stub would let the test choose the prefixes, which is the one input whose
    accuracy the partition depends on.
    """
    return BuggyStoreAdapter(client=None)  # type: ignore[arg-type]


# --- the policy now evaluates ------------------------------------------------


def test_a_declared_only_run_passes_the_policy() -> None:
    """The contracted change happened and nothing else did."""
    evaluation = _evaluate(
        _contract(policies=POLICY, assertions=CART_ASSERTION),
        _adapter(),
        events=(),
        initial=_state(quantity=0),
        final=_state(quantity=1),
    )

    policy = _policy_finding(evaluation)
    assert policy.status is CheckStatus.PASSED, policy.evidence
    assert policy.evidence["changed_paths"] >= 1, "the diff must actually have found the change"


def test_an_unnamed_path_fails_the_policy_and_names_itself() -> None:
    """Exit-gate criterion 2, at the evaluation seam.

    The cart assertion still passes; the run fails on a path no contract term and
    no executed tool's declared effect mentions.
    """
    evaluation = _evaluate(
        _contract(policies=POLICY, assertions=CART_ASSERTION),
        _adapter(),
        events=(),
        initial=_state(quantity=0),
        final=_state(quantity=1, note="leave with the neighbour"),
    )

    assert all(finding.status is CheckStatus.PASSED for finding in evaluation.assertions), (
        "every named assertion must still pass, or this proves something else"
    )
    policy = _policy_finding(evaluation)
    assert policy.status is CheckStatus.FAILED
    assert policy.classification is FailureClassification.UNDECLARED_STATE_CHANGE
    assert [str(path) for path in policy.paths] == ["target.preferences.delivery_note"]


def test_a_missing_snapshot_still_leaves_the_policy_unevaluated() -> None:
    """§16.1: the one case `not_evaluated` survives, and it must survive.

    "Nothing changed" and "we could not tell" are different answers. A run whose
    initial observation is absent has to produce the second, or an unobserved run
    would read as a clean one.
    """
    evaluation = _evaluate(
        _contract(policies=POLICY, assertions=CART_ASSERTION),
        _adapter(),
        events=(),
        initial=None,
        final=_state(quantity=1),
    )

    assert evaluation.changes is None
    assert _policy_finding(evaluation).status is CheckStatus.NOT_EVALUATED


def test_a_waiver_admits_churn_without_widening_anything_else() -> None:
    """Exit-gate criterion 3."""
    waived = [{"type": "no_undeclared_changes", "allow_paths": ["target.preferences"]}]

    evaluation = _evaluate(
        _contract(policies=waived, assertions=CART_ASSERTION),
        _adapter(),
        events=(),
        initial=_state(quantity=0),
        final=_state(quantity=1, note="leave with the neighbour"),
    )

    policy = _policy_finding(evaluation)
    assert policy.status is CheckStatus.PASSED
    assert [str(path) for path in policy.applied_exemptions] == ["target.preferences"]


# --- the report block --------------------------------------------------------


def test_the_block_and_the_finding_never_disagree() -> None:
    """The block is a projection of the finding, not a second computation."""
    evaluation = _evaluate(
        _contract(policies=POLICY, assertions=CART_ASSERTION),
        _adapter(),
        events=(),
        initial=_state(quantity=0),
        final=_state(quantity=1, note="leave with the neighbour"),
    )

    block = _undeclared_changes_block(evaluation)
    policy = _policy_finding(evaluation)

    assert block is not None
    assert block.paths == policy.paths
    assert block.undeclared == len(policy.paths)
    assert block.changed_paths == len(evaluation.changes or ())
    assert block.declared == block.changed_paths - block.undeclared
    assert block.effect_metadata_published is True, "the store adapter publishes an effect map"


def test_an_unevaluated_policy_reports_no_block_rather_than_a_block_of_zeros() -> None:
    """A block reading "0 changed, 0 undeclared" would say "nothing changed".

    The truth in that case is "nothing was compared", and §16.1 is explicit that
    the two must stay distinguishable.
    """
    evaluation = _evaluate(
        _contract(policies=POLICY, assertions=CART_ASSERTION),
        _adapter(),
        events=(),
        initial=None,
        final=_state(quantity=1),
    )

    assert _undeclared_changes_block(evaluation) is None


def test_a_contract_without_the_policy_carries_no_block() -> None:
    """A run that never asked about undeclared change must not appear to have."""
    evaluation = _evaluate(
        _contract(policies=[], assertions=CART_ASSERTION),
        _adapter(),
        events=(),
        initial=_state(quantity=0),
        final=_state(quantity=1, note="leave with the neighbour"),
    )

    assert evaluation.changes is not None, "the diff still runs; only the block is absent"
    assert _undeclared_changes_block(evaluation) is None


# --- determinism -------------------------------------------------------------


def test_the_same_snapshots_produce_an_identical_partition() -> None:
    """Exit-gate criterion 1, through the evaluation path rather than the diff."""
    contract = _contract(policies=POLICY, assertions=CART_ASSERTION)
    before, after = _state(quantity=0), _state(quantity=1, note="leave with the neighbour")

    first = _evaluate(contract, _adapter(), events=(), initial=before, final=after)
    second = _evaluate(contract, _adapter(), events=(), initial=before, final=after)

    assert first.changes == second.changes
    assert _policy_finding(first).paths == _policy_finding(second).paths
    assert _undeclared_changes_block(first) == _undeclared_changes_block(second)


def _policy_finding(evaluation: Any) -> Any:
    return next(
        finding for finding in evaluation.policies if finding.check_id == "no_undeclared_changes"
    )
