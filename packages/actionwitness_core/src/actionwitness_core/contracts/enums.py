"""Closed contract vocabulary: operators, severities, and policy types.

Spec v1.9 §9.4 (assertion operators and their exact semantics), §9.5 (policies
and severity), §10.2 ("restrict operators and policy types to known enums").

These are closed for a safety reason, not a tidiness one. An operator the engine
does not recognise must be a rejected contract, never a silently skipped
assertion — a contract that appears to check a total but does not is worse than
no contract at all. The same holds for a policy type: §12.7 requires every policy
to be evaluated or explicitly reported `not_evaluated`, and neither is possible
for a name the engine has never heard of.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "ENUM_REGISTRATIONS",
    "OPERATORS_REQUIRING_VALUE",
    "PRECONDITION_OPERATORS",
    "TRANSITION_OPERATORS",
    "AssertionOperator",
    "AssertionSeverity",
    "PolicyType",
]


class AssertionOperator(StrEnum):
    """The eight MVP operators (spec §9.4).

    Semantics are exact: no operator performs implicit string/number/boolean
    coercion, so `"1"` never equals `1`.
    """

    EQUALS = "equals"
    NOT_EQUALS = "not_equals"
    EXISTS = "exists"
    ABSENT = "absent"
    CONTAINS = "contains"
    UNCHANGED = "unchanged"
    CHANGED_BY = "changed_by"
    COUNT_EQUALS = "count_equals"


ASSERTION_OPERATOR_DESCRIPTIONS: Mapping[AssertionOperator, str] = {
    AssertionOperator.EQUALS: "Final resolved value is deep-equal to the expected JSON value.",
    AssertionOperator.NOT_EQUALS: (
        "Final resolved value is not deep-equal to the expected JSON value."
    ),
    AssertionOperator.EXISTS: "Path is present, including when its value is null.",
    AssertionOperator.ABSENT: "Path is not present; a present null value is not absent.",
    AssertionOperator.CONTAINS: (
        "Expected string is a substring of an observed string, or the expected JSON "
        "value is a deep-equal member of an observed array."
    ),
    AssertionOperator.UNCHANGED: (
        "Path resolves in both snapshots and the two values are deep-equal."
    ),
    AssertionOperator.CHANGED_BY: (
        "Final minus initial integer or decimal value equals the expected delta; "
        "decimal strings are compared as exact decimals."
    ),
    AssertionOperator.COUNT_EQUALS: (
        "Length of an observed object or array equals the expected non-negative integer."
    ),
}

#: Operators that compare the initial and final snapshots (spec §9.4). They are
#: invalid in preconditions, which see only the initial snapshot.
TRANSITION_OPERATORS: frozenset[AssertionOperator] = frozenset(
    {AssertionOperator.UNCHANGED, AssertionOperator.CHANGED_BY}
)

#: Operators a precondition may use - everything that reads one snapshot.
PRECONDITION_OPERATORS: frozenset[AssertionOperator] = (
    frozenset(AssertionOperator) - TRANSITION_OPERATORS
)

#: Operators that need an expected value (§10.2 "require an expected value only
#: for operators that need one"). `exists`/`absent` take none, and supplying one
#: is a contract error rather than an ignored field.
OPERATORS_REQUIRING_VALUE: frozenset[AssertionOperator] = frozenset(
    {
        AssertionOperator.EQUALS,
        AssertionOperator.NOT_EQUALS,
        AssertionOperator.CONTAINS,
        AssertionOperator.CHANGED_BY,
        AssertionOperator.COUNT_EQUALS,
    }
)


class AssertionSeverity(StrEnum):
    """Assertion severity (spec §9.5, FR-052).

    Severity governs assertion findings only. Every MVP policy failure is
    critical regardless of any severity a contract might try to attach to it.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


ASSERTION_SEVERITY_DESCRIPTIONS: Mapping[AssertionSeverity, str] = {
    AssertionSeverity.INFO: (
        "A mismatch stays visible in the report but does not change the overall result."
    ),
    AssertionSeverity.WARNING: (
        "A mismatch yields passed_with_warnings when no critical check fails."
    ),
    AssertionSeverity.CRITICAL: "A mismatch fails the run.",
}


class PolicyType(StrEnum):
    """The six MVP policy types (spec §9.5).

    All are recognised and safely evaluated from the first commit. The Tier 3
    label on a failure *injector* controls when a demonstration is exposed, never
    whether a seeded policy is silently ignored (BUILD_ORDER §7/M1).
    """

    REQUIRES_CONFIRMATION = "requires_confirmation"
    IDEMPOTENT_BY_REQUEST_ID = "idempotent_by_request_id"
    MAXIMUM_MUTATIONS = "maximum_mutations"
    FORBIDDEN_TOOL = "forbidden_tool"
    NO_UNDECLARED_CHANGES = "no_undeclared_changes"
    STABLE_TOOL_SURFACE = "stable_tool_surface"


POLICY_TYPE_DESCRIPTIONS: Mapping[PolicyType, str] = {
    PolicyType.REQUIRES_CONFIRMATION: (
        "A successful protected mutation must correlate to an earlier approval; a "
        "denied, expired, or cancelled attempt with no mutation passes."
    ),
    PolicyType.IDEMPOTENT_BY_REQUEST_ID: (
        "Repeating the same request ID and payload may change canonical state at most once."
    ),
    PolicyType.MAXIMUM_MUTATIONS: (
        "Qualifying state-changing completions may not exceed the configured limit."
    ),
    PolicyType.FORBIDDEN_TOOL: ("Any invocation-start event for the named tool fails the policy."),
    PolicyType.NO_UNDECLARED_CHANGES: (
        "No canonical state path outside the declared set may change during the run; "
        "every applied allow_paths waiver is recorded in the report."
    ),
    PolicyType.STABLE_TOOL_SURFACE: (
        "The target partition of the tool surface may not change during a run except "
        "through a declared delta."
    ),
}


ENUM_REGISTRATIONS: tuple[tuple[str, str, type[StrEnum], Mapping[StrEnum, str]], ...] = (
    ("assertion_operator", "spec §9.4", AssertionOperator, ASSERTION_OPERATOR_DESCRIPTIONS),
    (
        "assertion_severity",
        "spec §9.5 / FR-052",
        AssertionSeverity,
        ASSERTION_SEVERITY_DESCRIPTIONS,
    ),
    ("policy_type", "spec §9.5", PolicyType, POLICY_TYPE_DESCRIPTIONS),
)
