"""Closed engine vocabulary: what was checked, how it ended, and why it failed.

Spec v1.9 §17.1 (`findings.check_type`, `findings.status`), §16.1 (a policy whose
baseline is missing evaluates as `observation_unavailable` and "shall never be
reported as passed"), §22 (the twelve MVP failure classifications).

The classification set is closed and complete because eval expectations compare
*exact* classification sets (§24.1, AC-15). A classification invented at a call
site would make a regression case that reproduced its source failure look like a
different failure, which is the one thing the replay path must never do.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "ENUM_REGISTRATIONS",
    "CheckStatus",
    "CheckType",
    "FailureClassification",
]


class CheckType(StrEnum):
    """Which contract term produced a finding (spec §17.1, `findings.check_type`).

    §17.1 names the column but does not enumerate its values; these four are the
    kinds of term a contract can carry (§10.1: preconditions, expected_tools,
    assertions, policies), so the set is closed by the contract format rather
    than by an independent choice.
    """

    PRECONDITION = "precondition"
    ASSERTION = "assertion"
    EXPECTED_TOOLS = "expected_tools"
    POLICY = "policy"


CHECK_TYPE_DESCRIPTIONS: Mapping[CheckType, str] = {
    CheckType.PRECONDITION: "A precondition evaluated against the initial snapshot.",
    CheckType.ASSERTION: (
        "A business assertion evaluated against the final snapshot, or across both "
        "snapshots for a transition operator."
    ),
    CheckType.EXPECTED_TOOLS: (
        "The observed-trajectory conformance check; one finding covers the whole "
        "expected_tools term."
    ),
    CheckType.POLICY: (
        "A policy evaluated across recorded evidence; its check_id is the policy type."
    ),
}


class CheckStatus(StrEnum):
    """How one check ended.

    `observation_unavailable` exists so that a check whose evidence never arrived
    is reported as unresolved rather than degraded to a pass (§16.1,
    constitution §5: "observation failure produces an explicit non-pass result").
    """

    PASSED = "passed"
    FAILED = "failed"
    NOT_EVALUATED = "not_evaluated"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"


CHECK_STATUS_DESCRIPTIONS: Mapping[CheckStatus, str] = {
    CheckStatus.PASSED: "The check held.",
    CheckStatus.FAILED: "The check did not hold; severity decides what that costs the run.",
    CheckStatus.NOT_EVALUATED: (
        "The contract carried no such term, so nothing was judged. Never a pass."
    ),
    CheckStatus.OBSERVATION_UNAVAILABLE: (
        "Required evidence could not be supplied, so the check is unresolved. Never a pass."
    ),
}


class FailureClassification(StrEnum):
    """The twelve MVP classifications (spec §22).

    Row order here follows §22's table, which explicitly "carries no precedence" -
    primary-failure selection is by severity, then causal event sequence, then
    lexical check ID.
    """

    TOOL_EXECUTION_ERROR = "tool_execution_error"
    FALSE_SUCCESS_OR_STATE_MISMATCH = "false_success_or_state_mismatch"
    IDEMPOTENCY_VIOLATION = "idempotency_violation"
    MISSING_CONFIRMATION = "missing_confirmation"
    ASSERTION_MISMATCH = "assertion_mismatch"
    MISSING_EXPECTED_TOOL = "missing_expected_tool"
    UNEXPECTED_TOOL = "unexpected_tool"
    TRAJECTORY_ORDER_VIOLATION = "trajectory_order_violation"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    UNDECLARED_STATE_CHANGE = "undeclared_state_change"
    TOOL_SURFACE_MUTATION = "tool_surface_mutation"
    HARNESS_ERROR = "harness_error"


FAILURE_CLASSIFICATION_DESCRIPTIONS: Mapping[FailureClassification, str] = {
    FailureClassification.TOOL_EXECUTION_ERROR: (
        "Tool threw or returned an unexpected non-policy error; safe confirmation "
        "denial, expiry, or cancellation is excluded."
    ),
    FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH: (
        "Tool reported success but authoritative state disagreed."
    ),
    FailureClassification.IDEMPOTENCY_VIOLATION: "Repeated request changed state more than once.",
    FailureClassification.MISSING_CONFIRMATION: "Protected mutation lacked prior approval.",
    FailureClassification.ASSERTION_MISMATCH: "Deterministic expected value did not match.",
    FailureClassification.MISSING_EXPECTED_TOOL: "A required observed tool call did not occur.",
    FailureClassification.UNEXPECTED_TOOL: "A tool explicitly forbidden by policy appeared.",
    FailureClassification.TRAJECTORY_ORDER_VIOLATION: "Required call ordering was violated.",
    FailureClassification.OBSERVATION_UNAVAILABLE: (
        "Required state provider could not supply a value."
    ),
    FailureClassification.UNDECLARED_STATE_CHANGE: (
        "A canonical state path changed that no assertion, precondition, or executed "
        "tool's declared effect covered."
    ),
    FailureClassification.TOOL_SURFACE_MUTATION: (
        "The target tool surface changed during the run outside a declared delta."
    ),
    FailureClassification.HARNESS_ERROR: ("The harness itself failed to complete verification."),
}


ENUM_REGISTRATIONS: tuple[tuple[str, str, type[StrEnum], Mapping[StrEnum, str]], ...] = (
    ("check_type", "spec §17.1 / §10.1", CheckType, CHECK_TYPE_DESCRIPTIONS),
    ("check_status", "spec §16.1 / §17.1", CheckStatus, CHECK_STATUS_DESCRIPTIONS),
    (
        "failure_classification",
        "spec §22",
        FailureClassification,
        FAILURE_CLASSIFICATION_DESCRIPTIONS,
    ),
)
