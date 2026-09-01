"""005-T4 — declared target-effect evidence and bounding (FR-032, §13.4, §12.2).

FR-032 records these values "so idempotency and false-success evidence do not
depend on tool-return text or later actions". Two distinctions carry the whole
module, and both are the kind that a plausible implementation collapses:

* **absent is not null.** A path that does not resolve is a question the
  observation cannot answer; a path resolving to `null` is an answer.
* **unknown is not unchanged.** An observation that could not be taken makes
  every path unknowable, and reporting `changed: false` for it would claim the
  harness watched something it never saw.
"""

from __future__ import annotations

import pytest
from actionwitness_core.evidence.effects import (
    TRUNCATION_MARKER,
    bounded,
    effect_evidence,
)
from actionwitness_core.security.limits import MAX_FINDING_VALUE_CHARS
from actionwitness_core.security.redaction import REDACTED, RedactionPolicy

pytestmark = [pytest.mark.unit]

BEFORE = {"target": {"cart": {"total": "25.00", "discount": None, "items": {"mug": 1}}}}
AFTER = {
    "target": {"cart": {"total": "20.00", "discount": {"code": "SAVE20"}, "items": {"mug": 1}}}
}


# --- what moved -------------------------------------------------------------


def test_a_path_that_moved_is_reported_with_both_values() -> None:
    # Arrange / Act
    evidence = effect_evidence(["target.cart.total"], before=BEFORE, after=AFTER)

    # Assert
    assert evidence["target.cart.total"] == {
        "before": "25.00",
        "after": "20.00",
        "before_present": True,
        "after_present": True,
        "changed": True,
    }


def test_a_path_that_did_not_move_is_reported_as_unchanged() -> None:
    """The false-success case: the value is identical either side of the call."""
    # Arrange / Act
    evidence = effect_evidence(["target.cart.items"], before=BEFORE, after=AFTER)

    # Assert
    assert evidence["target.cart.items"]["changed"] is False


def test_a_present_null_is_an_answer_not_an_absence() -> None:
    """§9.4 makes this load-bearing elsewhere in the core; it holds here too."""
    # Arrange / Act
    evidence = effect_evidence(["target.cart.discount"], before=BEFORE, after=AFTER)

    # Assert
    assert evidence["target.cart.discount"]["before"] is None
    assert evidence["target.cart.discount"]["before_present"] is True
    assert evidence["target.cart.discount"]["changed"] is True


def test_a_path_neither_observation_has_is_unknown_rather_than_unchanged() -> None:
    """Saying `false` would claim the harness watched a path the target never
    had, which §12.2 forbids."""
    # Arrange / Act
    evidence = effect_evidence(["target.order.created"], before=BEFORE, after=AFTER)

    # Assert
    assert evidence["target.order.created"]["before_present"] is False
    assert evidence["target.order.created"]["after_present"] is False
    assert evidence["target.order.created"]["changed"] is None


def test_a_path_that_appeared_counts_as_changed() -> None:
    # Arrange
    before = {"target": {"cart": {}}}
    after = {"target": {"cart": {"total": "20.00"}}}

    # Act
    evidence = effect_evidence(["target.cart.total"], before=before, after=after)

    # Assert
    assert evidence["target.cart.total"]["before_present"] is False
    assert evidence["target.cart.total"]["after_present"] is True
    assert evidence["target.cart.total"]["changed"] is True


# --- unknowable -------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [(BEFORE, None), (None, AFTER), (None, None)],
    ids=["after-unobserved", "before-unobserved", "neither-observed"],
)
def test_an_unobserved_side_makes_every_path_unknowable(
    before: dict | None, after: dict | None
) -> None:
    """The distinction a plausible implementation collapses into `changed: true`.

    An observation that could not be taken is not evidence that something moved.
    """
    # Arrange / Act
    evidence = effect_evidence(["target.cart.total"], before=before, after=after)

    # Assert
    assert evidence["target.cart.total"]["changed"] is None


# --- the declaration is the boundary ----------------------------------------


def test_an_adapter_declaring_no_effect_paths_gets_an_empty_mapping() -> None:
    """§12.2: "missing effect metadata disables only causal false-success
    attribution" — it never licenses the harness to guess from a whole-state
    diff."""
    # Arrange / Act
    evidence = effect_evidence([], before=BEFORE, after=AFTER)

    # Assert
    assert evidence == {}


def test_only_declared_paths_are_recorded() -> None:
    """`target.cart.discount` moved too, but nothing declared it."""
    # Arrange / Act
    evidence = effect_evidence(["target.cart.total"], before=BEFORE, after=AFTER)

    # Assert
    assert set(evidence) == {"target.cart.total"}


# --- redaction and bounding -------------------------------------------------


def test_values_are_redacted_before_they_are_recorded() -> None:
    """§20.3: "before persistence, hashing, or export"."""
    # Arrange
    before = {"target": {"account": {"email": "someone@example.com"}}}
    after = {"target": {"account": {"email": "someone@example.com"}}}

    # Act
    evidence = effect_evidence(["target.account"], before=before, after=after)

    # Assert
    assert evidence["target.account"]["before"] == {"email": REDACTED}
    assert "example.com" not in str(evidence)


def test_a_contract_can_widen_redaction() -> None:
    """§20.3 applies contract paths "in addition to defaults"."""
    # Arrange
    policy = RedactionPolicy.from_paths(["**.delivery_note"])
    context = {"target": {"cart": {"delivery_note": "leave at reception"}}}

    # Act
    evidence = effect_evidence(["target.cart"], before=context, after=context, policy=policy)

    # Assert
    assert evidence["target.cart"]["before"] == {"delivery_note": REDACTED}


def test_a_long_string_is_truncated_with_an_explicit_marker() -> None:
    """§11.4 requires the marker so a reader can tell short from shortened."""
    # Arrange
    long_value = "x" * (MAX_FINDING_VALUE_CHARS + 50)

    # Act
    result = bounded(long_value)

    # Assert
    assert isinstance(result, str)
    assert result.endswith(TRUNCATION_MARKER)
    assert len(result) == MAX_FINDING_VALUE_CHARS + len(TRUNCATION_MARKER)


def test_a_short_string_is_left_exactly_as_it_was() -> None:
    # Arrange / Act / Assert
    assert bounded("25.00") == "25.00"


def test_bounding_does_not_change_a_values_type() -> None:
    """Rewriting a number as a string would show a reader a difference between
    `1` and `"1"` that never happened."""
    # Arrange / Act / Assert
    assert bounded(1) == 1
    assert bounded(True) is True
    assert bounded(None) is None
    assert bounded(2.5) == 2.5


def test_bounding_preserves_container_shape() -> None:
    # Arrange
    value = {"items": ["short", "y" * 500], "count": 2}

    # Act
    result = bounded(value)

    # Assert
    assert isinstance(result, dict)
    assert result["count"] == 2
    assert result["items"][0] == "short"
    assert result["items"][1].endswith(TRUNCATION_MARKER)


def test_changed_is_decided_on_the_values_that_were_stored() -> None:
    """Two values that differ only beyond the bound compare equal, because the
    stored evidence cannot distinguish them and a reader must be able to
    re-derive the answer from what was kept."""
    # Arrange
    before = {"target": {"note": "z" * MAX_FINDING_VALUE_CHARS + "aaa"}}
    after = {"target": {"note": "z" * MAX_FINDING_VALUE_CHARS + "bbb"}}

    # Act
    evidence = effect_evidence(["target.note"], before=before, after=after)

    # Assert
    assert evidence["target.note"]["changed"] is False
    assert evidence["target.note"]["before"] == evidence["target.note"]["after"]
