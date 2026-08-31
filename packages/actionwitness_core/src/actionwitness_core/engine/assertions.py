"""The eight assertion operators, evaluated exactly (spec v1.9 §9.4).

Also §12.6: FR-050 (same snapshots and contract hash always produce the same
result), FR-051 (a missing path is a structured mismatch, not an exception, and
`absent` passes by definition), FR-054 (a failed assertion carries expected and
actual).

"Operator semantics are exact and do not perform implicit string/number/boolean
coercion." Python's own equality does not meet that bar - `True == 1` and
`1 == 1.0` are both true - so equality here is written out rather than delegated.
The boolean case is the one that would actually bite: a target returning `1` for
a flag a contract asserts as `true` is a defect, and Python would call it a pass.

Where §17.2 requires *decimal* comparison it says so precisely: "in the
full-state diff of FR-157, in `changed_by`, and in candidate derivation". It does
not extend that to `equals`, so `"20.00"` and `"20.0"` are equal under
`changed_by` and different under `equals`. That asymmetry is deliberate and
tested: `equals` compares the JSON value an author wrote, while `changed_by`
computes an arithmetic difference.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation

from actionwitness_core.contracts.enums import (
    TRANSITION_OPERATORS,
    AssertionOperator,
    AssertionSeverity,
)
from actionwitness_core.contracts.models import Assertion, Precondition
from actionwitness_core.contracts.paths import MISSING, Resolution, resolve
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.engine.findings import Finding
from actionwitness_core.kernel import JsonValue

__all__ = [
    "deep_equal",
    "evaluate_assertion",
    "evaluate_assertions",
    "evaluate_operator",
    "evaluate_precondition",
    "evaluate_preconditions",
]

type Context = Mapping[str, JsonValue] | None


def deep_equal(left: object, right: object) -> bool:
    """Exact JSON deep equality with no cross-type coercion.

    Booleans are compared as booleans and never as numbers; numbers compare
    numerically, so `1` and `1.0` are equal exactly as RFC 8785 canonicalizes
    both to `1`; strings compare as strings, so `"1"` is never `1`.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, int | float) and isinstance(right, int | float):
        return left == right
    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return set(left) == set(right) and all(deep_equal(left[k], right[k]) for k in left)
    if _is_array(left) and _is_array(right):
        pair = (list(left), list(right))  # type: ignore[arg-type]
        return len(pair[0]) == len(pair[1]) and all(
            deep_equal(a, b) for a, b in zip(*pair, strict=True)
        )
    if left is None or right is None:
        return left is None and right is None
    return False


def _is_array(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray)


def _as_exact(value: object) -> Decimal | None:
    """Coerce an integer or decimal-string to an exact `Decimal`, else `None`.

    §9.4 defines `changed_by` over "integer or decimal value", and §17.2 makes
    the comparison a `Decimal` one. A float is refused rather than converted: a
    binary float that reached a money path is already the wrong number, and
    quietly comparing it would report a total as correct when it is not.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            candidate = Decimal(value)
        except InvalidOperation:
            return None
        return candidate if candidate.is_finite() else None
    return None


def evaluate_operator(
    operator: AssertionOperator,
    expected: JsonValue,
    expected_supplied: bool,
    initial: Resolution,
    final: Resolution,
) -> tuple[bool, JsonValue, str | None]:
    """Apply one operator and return (held, actual-for-the-report, reason).

    Public because causal classification re-applies the same operator to the
    immediate post-call observation (FR-055). Two implementations of one
    operator - one for the verdict, one for the causal check - would eventually
    disagree, and the disagreement would show up as a false accusation.
    """
    match operator:
        case AssertionOperator.EQUALS:
            if not final.found:
                return False, None, "path did not resolve in the final snapshot"
            return deep_equal(final.value, expected), final.value, "value did not match"

        case AssertionOperator.NOT_EQUALS:
            if not final.found:
                return False, None, "path did not resolve in the final snapshot"
            return not deep_equal(final.value, expected), final.value, "value matched"

        case AssertionOperator.EXISTS:
            # FR-051: "`exists` fails on a missing path"; §9.4: a present null
            # is present.
            return final.found, final.value if final.found else None, "path is absent"

        case AssertionOperator.ABSENT:
            # FR-051: "an `absent` assertion passes by definition" on a missing
            # path; §9.4: "a present null value is not absent".
            return not final.found, final.value if final.found else None, "path is present"

        case AssertionOperator.CONTAINS:
            if not final.found:
                return False, None, "path did not resolve in the final snapshot"
            actual = final.value
            if isinstance(actual, str):
                if not isinstance(expected, str):
                    return False, actual, "an observed string can only contain a string"
                return expected in actual, actual, "substring not found"
            if _is_array(actual):
                return (
                    any(deep_equal(item, expected) for item in actual),
                    actual,
                    "no member matched",
                )
            return False, actual, "contains applies to an observed string or array"

        case AssertionOperator.UNCHANGED:
            # §9.4: "Path resolves in both snapshots and the two values are
            # deep-equal." A path missing from either side is a change, not a
            # vacuous pass.
            if not initial.found or not final.found:
                return (
                    False,
                    final.value if final.found else None,
                    ("path did not resolve in both snapshots"),
                )
            return deep_equal(initial.value, final.value), final.value, "value changed"

        case AssertionOperator.CHANGED_BY:
            if not initial.found or not final.found:
                return (
                    False,
                    final.value if final.found else None,
                    ("path did not resolve in both snapshots"),
                )
            before, after = _as_exact(initial.value), _as_exact(final.value)
            delta = _as_exact(expected) if expected_supplied else None
            if before is None or after is None:
                return False, final.value, "changed_by requires integer or decimal values"
            if delta is None:
                return False, final.value, "the expected delta is not an integer or decimal"
            return after - before == delta, final.value, "delta did not match"

        case AssertionOperator.COUNT_EQUALS:
            if not final.found:
                return False, None, "path did not resolve in the final snapshot"
            actual = final.value
            if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
                return False, actual, "count_equals expects a non-negative integer"
            if isinstance(actual, Mapping) or _is_array(actual):
                return len(actual) == expected, len(actual), "count did not match"
            return False, actual, "count_equals applies to an observed object or array"

    raise AssertionError(f"unhandled operator {operator!r}")  # pragma: no cover


def evaluate_assertion(assertion: Assertion, *, initial: Context, final: Context) -> Finding:
    """Evaluate one assertion against the snapshots (§9.4, §12.6).

    A missing snapshot yields `observation_unavailable` rather than a mismatch:
    the difference between "the value was wrong" and "nobody could see the value"
    is exactly the difference §22 draws, and collapsing it would attribute a
    provider outage to the target under test.
    """
    needs_initial = assertion.operator in TRANSITION_OPERATORS
    if final is None or (needs_initial and initial is None):
        return Finding(
            check_id=assertion.id,
            check_type=CheckType.ASSERTION,
            status=CheckStatus.OBSERVATION_UNAVAILABLE,
            severity=assertion.severity,
            classification=FailureClassification.OBSERVATION_UNAVAILABLE,
            path=assertion.path,
            expected=assertion.value,
            evidence={"reason": "the required authoritative observation was not available"},
        )

    held, actual, reason = evaluate_operator(
        assertion.operator,
        assertion.value,
        "value" in assertion.model_fields_set,
        resolve(assertion.path, initial) if initial is not None else MISSING,
        resolve(assertion.path, final),
    )
    return Finding(
        check_id=assertion.id,
        check_type=CheckType.ASSERTION,
        status=CheckStatus.PASSED if held else CheckStatus.FAILED,
        severity=assertion.severity,
        # Causal attribution refines this to `false_success_or_state_mismatch`
        # where FR-055's conditions hold; on its own the engine can only say the
        # value disagreed.
        classification=None if held else FailureClassification.ASSERTION_MISMATCH,
        path=assertion.path,
        expected=assertion.value,
        actual=actual,
        evidence={} if held else {"reason": reason or "assertion did not hold"},
    )


def evaluate_assertions(
    assertions: Sequence[Assertion], *, initial: Context, final: Context
) -> tuple[Finding, ...]:
    """Evaluate every assertion, preserving contract order."""
    return tuple(
        evaluate_assertion(assertion, initial=initial, final=final) for assertion in assertions
    )


def evaluate_precondition(precondition: Precondition, *, initial: Context) -> Finding:
    """Evaluate one precondition against the initial snapshot (§9.4, FR-030).

    A failing precondition stops arming with `PRECONDITION_FAILED` and creates
    neither a run nor a snapshot, so these findings are the detail attached to
    that refusal rather than terms in a report. Severity is fixed at `critical`
    because a precondition that did not hold makes the whole run meaningless.
    """
    check_id = str(precondition.path)
    if initial is None:
        return Finding(
            check_id=check_id,
            check_type=CheckType.PRECONDITION,
            status=CheckStatus.OBSERVATION_UNAVAILABLE,
            severity=AssertionSeverity.CRITICAL,
            classification=FailureClassification.OBSERVATION_UNAVAILABLE,
            path=precondition.path,
            expected=precondition.value,
            evidence={"reason": "the initial authoritative observation was not available"},
        )

    held, actual, reason = evaluate_operator(
        precondition.operator,
        precondition.value,
        "value" in precondition.model_fields_set,
        MISSING,
        resolve(precondition.path, initial),
    )
    return Finding(
        check_id=check_id,
        check_type=CheckType.PRECONDITION,
        status=CheckStatus.PASSED if held else CheckStatus.FAILED,
        severity=AssertionSeverity.CRITICAL,
        classification=None if held else FailureClassification.ASSERTION_MISMATCH,
        path=precondition.path,
        expected=precondition.value,
        actual=actual,
        evidence={} if held else {"reason": reason or "precondition did not hold"},
    )


def evaluate_preconditions(
    preconditions: Sequence[Precondition], *, initial: Context
) -> tuple[Finding, ...]:
    """Evaluate every precondition, preserving contract order."""
    return tuple(
        evaluate_precondition(precondition, initial=initial) for precondition in preconditions
    )
