"""Assertion-operator gates (spec v1.9 §9.4, §12.6, §22, FR-051/53/54; 002-T8).

Table-driven, because §9.4 is a table: eight operators, each with one sentence of
exact meaning, and the cheapest way for one of them to drift is for nobody to
have written down the case that distinguishes it from its neighbour.

The cases that matter most are the ones Python gets wrong on its own. `True == 1`
and `1 == 1.0` are both true in Python; §9.4 forbids implicit boolean coercion
but RFC 8785 canonicalizes `1` and `1.0` identically, so the first must be false
here and the second must be true. And `"20.00"` must equal `"20.0"` under
`changed_by` (§17.2 requires decimal comparison there) while differing under
`equals` (§17.2 does not extend it there).
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.enums import AssertionSeverity
from actionwitness_core.contracts.models import Assertion, Precondition
from actionwitness_core.engine.assertions import (
    deep_equal,
    evaluate_assertion,
    evaluate_assertions,
    evaluate_precondition,
)
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.engine.findings import (
    Finding,
    aggregate,
    critical_classifications,
    order_failures,
    primary_failure,
)
from actionwitness_core.reports.enums import LayerResult

INITIAL = {
    "target": {
        "cart": {"items": {}, "total": "0.00", "quantity": 0, "note": None},
        "order": {"created": False},
        "flags": {"ready": False},
    }
}

FINAL = {
    "target": {
        "cart": {
            "items": {"mug": {"quantity": 1}},
            "total": "20.00",
            "quantity": 2,
            "note": None,
            "tags": ["gift", "fragile"],
            "label": "ceramic mug",
        },
        "order": {"created": False},
        "flags": {"ready": True},
    }
}


def _assert(path: str, operator: str, **extra: object) -> Assertion:
    fields: dict = {"id": "check", "path": path, "operator": operator}
    fields.update(extra)
    return Assertion(**fields)


def _run(path: str, operator: str, **extra: object) -> Finding:
    return evaluate_assertion(_assert(path, operator, **extra), initial=INITIAL, final=FINAL)


def _held(path: str, operator: str, **extra: object) -> bool:
    return _run(path, operator, **extra).status is CheckStatus.PASSED


# --- deep equality ----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "left,right,equal",
    [
        (1, 1, True),
        (1, 1.0, True),
        (1, True, False),
        (0, False, False),
        (True, True, True),
        (True, False, False),
        ("1", 1, False),
        ("a", "a", True),
        (None, None, True),
        (None, False, False),
        ({"a": 1}, {"a": 1}, True),
        ({"a": 1}, {"a": 1, "b": 2}, False),
        ([1, 2], [1, 2], True),
        ([1, 2], [2, 1], False),
        ([1], (1,), True),
        ({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]}, True),
    ],
)
def test_equality_never_coerces_across_json_types(left: object, right: object, equal: bool) -> None:
    """§9.4: "no implicit string/number/boolean coercion"."""
    assert deep_equal(left, right) is equal


# --- the eight operators (§9.4) ---------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "path,operator,extra,expected",
    [
        # equals
        ("target.cart.total", "equals", {"value": "20.00"}, True),
        ("target.cart.total", "equals", {"value": "20.0"}, False),
        ("target.order.created", "equals", {"value": False}, True),
        ("target.order.created", "equals", {"value": 0}, False),
        ("target.cart.note", "equals", {"value": None}, True),
        ("target.cart.missing", "equals", {"value": "x"}, False),
        # not_equals
        ("target.cart.total", "not_equals", {"value": "16.00"}, True),
        ("target.cart.total", "not_equals", {"value": "20.00"}, False),
        ("target.cart.missing", "not_equals", {"value": "x"}, False),
        # exists
        ("target.cart.total", "exists", {}, True),
        ("target.cart.note", "exists", {}, True),
        ("target.cart.missing", "exists", {}, False),
        # absent
        ("target.cart.missing", "absent", {}, True),
        ("target.cart.note", "absent", {}, False),
        ("target.cart.total", "absent", {}, False),
        # contains
        ("target.cart.label", "contains", {"value": "mug"}, True),
        ("target.cart.label", "contains", {"value": "bowl"}, False),
        ("target.cart.tags", "contains", {"value": "gift"}, True),
        ("target.cart.tags", "contains", {"value": "boxed"}, False),
        ("target.cart.quantity", "contains", {"value": 2}, False),
        ("target.cart.label", "contains", {"value": 1}, False),
        # unchanged
        ("target.order.created", "unchanged", {}, True),
        ("target.cart.total", "unchanged", {}, False),
        ("target.cart.tags", "unchanged", {}, False),
        # changed_by
        ("target.cart.quantity", "changed_by", {"value": 2}, True),
        ("target.cart.quantity", "changed_by", {"value": 1}, False),
        ("target.cart.total", "changed_by", {"value": "20.00"}, True),
        ("target.cart.total", "changed_by", {"value": "20.0"}, True),
        ("target.cart.total", "changed_by", {"value": 20}, True),
        # count_equals
        ("target.cart.items", "count_equals", {"value": 1}, True),
        ("target.cart.items", "count_equals", {"value": 2}, False),
        ("target.cart.tags", "count_equals", {"value": 2}, True),
        ("target.cart.label", "count_equals", {"value": 11}, False),
    ],
    ids=lambda arg: str(arg),
)
def test_operator_semantics_are_exact(
    path: str, operator: str, extra: dict, expected: bool
) -> None:
    assert _held(path, operator, **extra) is expected


@pytest.mark.unit
def test_a_present_null_exists_and_is_not_absent() -> None:
    """§9.4: "`exists`: including when its value is null"; "a present null is not absent"."""
    assert _held("target.cart.note", "exists") is True
    assert _held("target.cart.note", "absent") is False


@pytest.mark.unit
def test_absent_passes_by_definition_on_a_missing_path() -> None:
    """FR-051 names this as the one exception to missing-path mismatch."""
    finding = _run("target.cart.nothing.here", "absent")
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_a_missing_path_is_a_mismatch_and_never_an_exception() -> None:
    """FR-051: "a structured mismatch rather than an unhandled exception"."""
    finding = _run("target.cart.missing", "equals", value="x")
    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH
    assert finding.evidence["reason"]


@pytest.mark.unit
def test_changed_by_compares_decimals_by_value_not_by_spelling() -> None:
    """§17.2: "`20.00` and `20.0` are the same value" - stated for changed_by."""
    assert _held("target.cart.total", "changed_by", value="20.0") is True
    assert _held("target.cart.total", "changed_by", value="20.000") is True


@pytest.mark.unit
def test_equals_compares_the_json_value_the_author_wrote() -> None:
    """§17.2 lists where decimal comparison applies; `equals` is not on that list."""
    assert _held("target.cart.total", "equals", value="20.00") is True
    assert _held("target.cart.total", "equals", value="20.0") is False


@pytest.mark.unit
def test_changed_by_refuses_a_float_rather_than_comparing_it() -> None:
    """A binary float in a money path is already wrong; comparing it hides that."""
    final = {"target": {"cart": {"total": 20.0}}}
    initial = {"target": {"cart": {"total": 0.0}}}
    finding = evaluate_assertion(
        _assert("target.cart.total", "changed_by", value=20), initial=initial, final=final
    )
    assert finding.status is CheckStatus.FAILED
    assert "integer or decimal" in finding.evidence["reason"]


@pytest.mark.unit
def test_changed_by_refuses_a_boolean_masquerading_as_a_number() -> None:
    finding = evaluate_assertion(
        _assert("target.flags.ready", "changed_by", value=1), initial=INITIAL, final=FINAL
    )
    assert finding.status is CheckStatus.FAILED


@pytest.mark.unit
def test_a_transition_operator_needs_the_path_in_both_snapshots() -> None:
    """§9.4: `unchanged` requires the path to resolve in both."""
    initial = {"target": {"cart": {}}}
    for operator, extra in (("unchanged", {}), ("changed_by", {"value": 1})):
        finding = evaluate_assertion(
            _assert("target.cart.quantity", operator, **extra), initial=initial, final=FINAL
        )
        assert finding.status is CheckStatus.FAILED
        assert "both snapshots" in finding.evidence["reason"]


@pytest.mark.unit
def test_count_equals_refuses_a_negative_or_non_integer_expectation() -> None:
    assert _held("target.cart.items", "count_equals", value=-1) is False
    assert _held("target.cart.items", "count_equals", value=True) is False


@pytest.mark.unit
def test_a_failed_assertion_reports_expected_and_actual() -> None:
    """FR-054: "every failed assertion shall include redacted expected and actual"."""
    finding = _run("target.cart.total", "equals", value="16.00")
    assert finding.expected == "16.00"
    assert finding.actual == "20.00"


@pytest.mark.unit
def test_evaluation_is_deterministic_for_the_same_snapshots() -> None:
    """FR-050: identical inputs always produce identical results."""
    assertion = _assert("target.cart.total", "equals", value="16.00")
    first = evaluate_assertion(assertion, initial=INITIAL, final=FINAL)
    second = evaluate_assertion(assertion, initial=INITIAL, final=FINAL)
    assert first == second


@pytest.mark.unit
def test_assertions_are_evaluated_in_contract_order() -> None:
    findings = evaluate_assertions(
        [
            _assert("target.cart.total", "exists"),
            _assert("target.order.created", "exists"),
        ],
        initial=INITIAL,
        final=FINAL,
    )
    assert [str(finding.path) for finding in findings] == [
        "target.cart.total",
        "target.order.created",
    ]


# --- unavailable observations (constitution §5) -----------------------------


@pytest.mark.unit
def test_a_missing_final_snapshot_is_unresolved_and_never_a_pass() -> None:
    finding = evaluate_assertion(
        _assert("target.cart.total", "exists"), initial=INITIAL, final=None
    )
    assert finding.status is CheckStatus.OBSERVATION_UNAVAILABLE
    assert finding.classification is FailureClassification.OBSERVATION_UNAVAILABLE
    assert finding.failed is True


@pytest.mark.unit
def test_a_missing_initial_snapshot_only_blocks_transition_operators() -> None:
    """A one-snapshot operator does not need the initial observation."""
    transition = evaluate_assertion(
        _assert("target.cart.total", "unchanged"), initial=None, final=FINAL
    )
    assert transition.status is CheckStatus.OBSERVATION_UNAVAILABLE

    single = evaluate_assertion(_assert("target.cart.total", "exists"), initial=None, final=FINAL)
    assert single.status is CheckStatus.PASSED


@pytest.mark.unit
def test_an_absent_assertion_does_not_pass_when_nothing_was_observed() -> None:
    """`absent` passes on a missing path, not on a missing observation."""
    finding = evaluate_assertion(
        _assert("target.cart.missing", "absent"), initial=INITIAL, final=None
    )
    assert finding.status is CheckStatus.OBSERVATION_UNAVAILABLE


# --- preconditions (FR-030) -------------------------------------------------


@pytest.mark.unit
def test_a_precondition_reads_the_initial_snapshot() -> None:
    finding = evaluate_precondition(
        Precondition(path="target.cart.items", operator="count_equals", value=0), initial=INITIAL
    )
    assert finding.status is CheckStatus.PASSED
    assert finding.check_type is CheckType.PRECONDITION


@pytest.mark.unit
def test_a_precondition_is_always_critical() -> None:
    """A run whose starting state was wrong cannot produce a meaningful verdict."""
    finding = evaluate_precondition(
        Precondition(path="target.cart.items", operator="count_equals", value=5), initial=INITIAL
    )
    assert finding.severity is AssertionSeverity.CRITICAL
    assert finding.status is CheckStatus.FAILED


# --- severity aggregation (FR-052, FR-053) ----------------------------------


def _finding(check_id: str, status: CheckStatus, severity: AssertionSeverity, **extra) -> Finding:
    return Finding(
        check_id=check_id,
        check_type=CheckType.ASSERTION,
        status=status,
        severity=severity,
        classification=FailureClassification.ASSERTION_MISMATCH
        if status is CheckStatus.FAILED
        else None,
        **extra,
    )


@pytest.mark.unit
def test_all_passing_checks_aggregate_to_passed() -> None:
    findings = [_finding("a", CheckStatus.PASSED, AssertionSeverity.CRITICAL)]
    assert aggregate(findings) is LayerResult.PASSED


@pytest.mark.unit
def test_a_critical_failure_fails_the_layer() -> None:
    findings = [
        _finding("a", CheckStatus.PASSED, AssertionSeverity.CRITICAL),
        _finding("b", CheckStatus.FAILED, AssertionSeverity.CRITICAL),
        _finding("c", CheckStatus.FAILED, AssertionSeverity.WARNING),
    ]
    assert aggregate(findings) is LayerResult.FAILED


@pytest.mark.unit
def test_warning_only_failures_yield_passed_with_warnings() -> None:
    findings = [
        _finding("a", CheckStatus.PASSED, AssertionSeverity.CRITICAL),
        _finding("b", CheckStatus.FAILED, AssertionSeverity.WARNING),
    ]
    assert aggregate(findings) is LayerResult.PASSED_WITH_WARNINGS


@pytest.mark.unit
def test_info_only_failures_leave_the_result_passed_but_stay_visible() -> None:
    """FR-053: info mismatches "leave the result passed while remaining visible"."""
    failing_info = _finding("b", CheckStatus.FAILED, AssertionSeverity.INFO)
    assert aggregate([failing_info]) is LayerResult.PASSED
    assert failing_info.failed is True


@pytest.mark.unit
def test_an_unresolved_critical_check_fails_rather_than_being_skipped() -> None:
    """Constitution §5: observation failure never degrades to success."""
    unresolved = _finding("a", CheckStatus.OBSERVATION_UNAVAILABLE, AssertionSeverity.CRITICAL)
    assert aggregate([unresolved]) is LayerResult.FAILED


@pytest.mark.unit
def test_a_not_evaluated_check_counts_neither_way() -> None:
    findings = [_finding("a", CheckStatus.NOT_EVALUATED, AssertionSeverity.CRITICAL)]
    assert aggregate(findings) is LayerResult.PASSED
    assert findings[0].failed is False


@pytest.mark.unit
def test_no_findings_at_all_aggregate_to_passed() -> None:
    assert aggregate([]) is LayerResult.PASSED


# --- primary-failure ordering (§22) -----------------------------------------


@pytest.mark.unit
def test_severity_outranks_event_sequence() -> None:
    early_warning = _finding(
        "a", CheckStatus.FAILED, AssertionSeverity.WARNING, causal_event_sequence=1
    )
    late_critical = _finding(
        "b", CheckStatus.FAILED, AssertionSeverity.CRITICAL, causal_event_sequence=99
    )
    assert primary_failure([early_warning, late_critical]) is late_critical


@pytest.mark.unit
def test_within_one_severity_the_lowest_causal_sequence_wins() -> None:
    late = _finding("a", CheckStatus.FAILED, AssertionSeverity.CRITICAL, causal_event_sequence=9)
    early = _finding("b", CheckStatus.FAILED, AssertionSeverity.CRITICAL, causal_event_sequence=2)
    assert primary_failure([late, early]) is early


@pytest.mark.unit
def test_a_finding_with_no_causal_event_sorts_after_every_finding_that_has_one() -> None:
    """§22 names this case for undeclared_state_change and surface mismatches."""
    attributed = _finding(
        "z-late", CheckStatus.FAILED, AssertionSeverity.CRITICAL, causal_event_sequence=1_000
    )
    unattributed = _finding("a-early", CheckStatus.FAILED, AssertionSeverity.CRITICAL)
    assert primary_failure([unattributed, attributed]) is attributed


@pytest.mark.unit
def test_check_id_breaks_a_remaining_tie_lexically() -> None:
    """Without this the displayed failure would depend on iteration order."""
    beta = _finding("beta", CheckStatus.FAILED, AssertionSeverity.CRITICAL, causal_event_sequence=3)
    alpha = _finding(
        "alpha", CheckStatus.FAILED, AssertionSeverity.CRITICAL, causal_event_sequence=3
    )
    assert primary_failure([beta, alpha]) is alpha
    assert primary_failure([alpha, beta]) is alpha


@pytest.mark.unit
def test_ordering_is_total_so_input_order_cannot_change_the_result() -> None:
    findings = [
        _finding("d", CheckStatus.FAILED, AssertionSeverity.INFO, causal_event_sequence=1),
        _finding("c", CheckStatus.FAILED, AssertionSeverity.CRITICAL),
        _finding("b", CheckStatus.FAILED, AssertionSeverity.CRITICAL, causal_event_sequence=5),
        _finding("a", CheckStatus.FAILED, AssertionSeverity.WARNING, causal_event_sequence=2),
    ]
    expected = [finding.check_id for finding in order_failures(findings)]
    assert expected == ["b", "c", "a", "d"]
    assert [f.check_id for f in order_failures(list(reversed(findings)))] == expected


@pytest.mark.unit
def test_passing_findings_are_never_candidates_for_primary_failure() -> None:
    assert primary_failure([_finding("a", CheckStatus.PASSED, AssertionSeverity.CRITICAL)]) is None
    assert primary_failure([]) is None


@pytest.mark.unit
def test_the_critical_classification_set_is_sorted_and_deduplicated() -> None:
    """AC-15 compares this set exactly; evaluation order must not change it."""
    findings = [
        _finding("b", CheckStatus.FAILED, AssertionSeverity.CRITICAL),
        _finding("a", CheckStatus.FAILED, AssertionSeverity.CRITICAL),
        Finding(
            check_id="c",
            check_type=CheckType.ASSERTION,
            status=CheckStatus.OBSERVATION_UNAVAILABLE,
            severity=AssertionSeverity.CRITICAL,
            classification=FailureClassification.OBSERVATION_UNAVAILABLE,
        ),
        _finding("d", CheckStatus.FAILED, AssertionSeverity.WARNING),
    ]
    assert critical_classifications(findings) == (
        FailureClassification.ASSERTION_MISMATCH,
        FailureClassification.OBSERVATION_UNAVAILABLE,
    )
    assert critical_classifications(list(reversed(findings))) == critical_classifications(findings)
