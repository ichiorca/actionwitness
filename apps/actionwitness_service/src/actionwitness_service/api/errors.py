"""Stable API error codes (spec v1.9 §15.8).

The wire envelope is fixed:

    {"error": {"code": ..., "message": ..., "retryable": ..., "details": [...]}}

`code` is the contract. Clients — the React workspace, WebMCP tool results, the
CLI, and tests — branch on it, so a code is an API promise: renaming one is a
breaking change, and inventing one at a call site forks the vocabulary.

These live in the service rather than `actionwitness_core` because they carry HTTP
status and retryability. The core stays framework-neutral (constitution §1) and
publishes domain vocabulary only; see `actionwitness_core.journeys.enums`.

`retryable` is a safety statement, not a hint. It is true only when repeating the
identical request under its original idempotency key cannot duplicate a mutation.
An ambiguous transport outcome is never marked retryable (constitution §5).

Internal exceptions and stack traces never reach a browser tool (spec §15.8).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

__all__ = ["API_ERROR_DESCRIPTIONS", "ApiErrorCode", "ApiErrorSpec"]


class ApiErrorCode(StrEnum):
    """Closed set of stable API error codes."""

    # Validation and request shape
    CONTRACT_VALIDATION_FAILED = "CONTRACT_VALIDATION_FAILED"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    EXTERNAL_TARGET_FORBIDDEN_OPERATION = "EXTERNAL_TARGET_FORBIDDEN_OPERATION"

    # Authorization
    AUDIT_NOT_AUTHORIZED = "AUDIT_NOT_AUTHORIZED"

    # Lifecycle conflicts — every one of these is HTTP 409
    RUN_IN_PROGRESS = "RUN_IN_PROGRESS"
    RUN_ALREADY_VERIFYING = "RUN_ALREADY_VERIFYING"
    RUN_MUTATION_LOCKED = "RUN_MUTATION_LOCKED"
    RUN_TIMELINE_SEALED = "RUN_TIMELINE_SEALED"
    CANDIDATES_UNCURATED = "CANDIDATES_UNCURATED"
    CONTRACT_NOT_PUBLISHABLE = "CONTRACT_NOT_PUBLISHABLE"
    PROPOSAL_RUN_NOT_VERIFIABLE = "PROPOSAL_RUN_NOT_VERIFIABLE"
    PROPOSAL_RUN_NOT_ELIGIBLE = "PROPOSAL_RUN_NOT_ELIGIBLE"
    SURFACE_BASELINE_ALREADY_SET = "SURFACE_BASELINE_ALREADY_SET"
    SELF_OBSERVATION_LOOP = "SELF_OBSERVATION_LOOP"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"

    # Consent
    CONFIRMATION_DENIED = "CONFIRMATION_DENIED"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"

    # Capacity and availability
    EVENT_LIMIT_EXCEEDED = "EVENT_LIMIT_EXCEEDED"
    WORKSPACE_LIMIT_EXCEEDED = "WORKSPACE_LIMIT_EXCEEDED"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    WORKSPACE_LOCK_TIMEOUT = "WORKSPACE_LOCK_TIMEOUT"


@dataclass(frozen=True, slots=True)
class ApiErrorSpec:
    """Everything a handler, a client, or a reviewer needs about one code.

    `provenance` distinguishes a code the specification names from one this
    project allocated, so an invented name can never be mistaken for a
    specified one.
    """

    http_status: int
    retryable: bool
    description: str
    spec_ref: str
    provenance: str  # "spec" or "project"


API_ERROR_DESCRIPTIONS: Mapping[ApiErrorCode, ApiErrorSpec] = {
    ApiErrorCode.CONTRACT_VALIDATION_FAILED: ApiErrorSpec(
        http_status=422,
        retryable=False,
        description=(
            "The outcome contract is invalid. `details` names the offending paths; "
            "resubmitting the same document cannot succeed."
        ),
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.PRECONDITION_FAILED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "Arming validated preconditions against the observed initial state and "
            "they did not hold. Neither a run nor a partial snapshot is created."
        ),
        spec_ref="FR-030",
        provenance="spec",
    ),
    ApiErrorCode.EXTERNAL_TARGET_FORBIDDEN_OPERATION: ApiErrorSpec(
        http_status=400,
        retryable=False,
        description="An external contract named an operation the safe scope forbids.",
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.AUDIT_NOT_AUTHORIZED: ApiErrorSpec(
        http_status=403,
        retryable=False,
        description=(
            "The audited origin is not both operator-asserted and present in the "
            "deployment allowlist. A workspace cannot authorize an origin the "
            "deployment did not configure."
        ),
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.RUN_IN_PROGRESS: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description="The operation requires no active run, and one is nonterminal.",
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.RUN_ALREADY_VERIFYING: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "Verification already won the race, so a new target action is rejected. "
            "Creates no finding and no tool_execution_error: losing this race is "
            "correct behavior, not a defect in the call."
        ),
        spec_ref="FR-038",
        provenance="spec",
    ),
    ApiErrorCode.RUN_MUTATION_LOCKED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "A direct human target mutation was rejected because a run holds the "
            "exclusive lease. Reads, reset, and confirmation decisions remain available."
        ),
        spec_ref="FR-039",
        provenance="spec",
    ),
    ApiErrorCode.RUN_TIMELINE_SEALED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description="The run's sealing event has been appended; its timeline is immutable.",
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.CANDIDATES_UNCURATED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "A contract cannot be frozen while a machine-proposed candidate is "
            "uncurated; a human curates every proposed term."
        ),
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.CONTRACT_NOT_PUBLISHABLE: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "The contract holds non-publishable terms and will not be partially disclosed."
        ),
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.PROPOSAL_RUN_NOT_VERIFIABLE: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description="A proposal-mode run judges no contract and carries no verdict.",
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.PROPOSAL_RUN_NOT_ELIGIBLE: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description="A proposal-mode run cannot generate a regression eval case.",
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.SURFACE_BASELINE_ALREADY_SET: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "A differing tool-surface baseline was submitted for a run that already "
            "has one. Identical resubmissions are idempotent and do not reach this."
        ),
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.SELF_OBSERVATION_LOOP: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "A self-witnessing run would observe its own recording workspace, or "
            "exceed the recursion cap of one."
        ),
        spec_ref="§15.8",
        provenance="spec",
    ),
    ApiErrorCode.IDEMPOTENCY_KEY_REUSED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "A request ID was reused with a different payload. Fails closed: a key "
            "identifies one intent, and retrying under a new key could duplicate "
            "the mutation."
        ),
        spec_ref="Appendix D.2",
        provenance="spec",
    ),
    ApiErrorCode.CONFIRMATION_DENIED: ApiErrorSpec(
        http_status=403,
        retryable=False,
        description=(
            "A human denied the protected mutation. No mutation occurred, and the "
            "confirmation policy outcome is a success."
        ),
        spec_ref="FR-064",
        provenance="spec",
    ),
    ApiErrorCode.CONFIRMATION_EXPIRED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "The confirmation expired unresolved. Creates no tool-execution failure "
            "classification and never converts into an approval."
        ),
        spec_ref="FR-065",
        provenance="spec",
    ),
    ApiErrorCode.EVENT_LIMIT_EXCEEDED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "The run reached its persisted-event ceiling. Recorded as a terminal "
            "boundary event; existing evidence is preserved."
        ),
        spec_ref="FR-008",
        provenance="spec",
    ),
    ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "A workspace resource cap was reached. The response names a purge "
            "action; no partial mutation is committed."
        ),
        spec_ref="FR-008",
        provenance="spec",
    ),
    ApiErrorCode.TARGET_UNAVAILABLE: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "The selected contract's target adapter is not registered or its module "
            "is disabled. A missing optional target is a bounded unavailable state, "
            "never a process failure."
        ),
        spec_ref="FR-021",
        provenance="spec",
    ),
    ApiErrorCode.WORKSPACE_LOCK_TIMEOUT: ApiErrorSpec(
        http_status=503,
        retryable=True,
        description=(
            "The per-workspace mutation lock or the SQLite busy timeout elapsed. "
            "Safe to retry under the original idempotency key because the "
            "transaction never began."
        ),
        spec_ref="§17 / ADR-0003",
        provenance="project",
    ),
}
