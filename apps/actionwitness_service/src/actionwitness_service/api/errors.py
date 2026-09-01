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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "API_ERROR_DESCRIPTIONS",
    "ApiError",
    "ApiErrorCode",
    "ApiErrorSpec",
    "error_from_core",
]


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
    TRIAL_BINDING_AMBIGUOUS = "TRIAL_BINDING_AMBIGUOUS"
    TRIAL_ALREADY_BOUND = "TRIAL_ALREADY_BOUND"
    BENCHMARK_BINDINGS_SEALED = "BENCHMARK_BINDINGS_SEALED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"

    # Consent
    CONFIRMATION_DENIED = "CONFIRMATION_DENIED"
    CONFIRMATION_EXPIRED = "CONFIRMATION_EXPIRED"

    # Request safety (004): the boundary refusals that precede any handler
    ORIGIN_NOT_ALLOWED = "ORIGIN_NOT_ALLOWED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"

    # Capacity and availability
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    EVENT_LIMIT_EXCEEDED = "EVENT_LIMIT_EXCEEDED"
    WORKSPACE_LIMIT_EXCEEDED = "WORKSPACE_LIMIT_EXCEEDED"
    TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
    WORKSPACE_LOCK_TIMEOUT = "WORKSPACE_LOCK_TIMEOUT"
    HARNESS_ERROR = "HARNESS_ERROR"


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
    # §26.5 requires duplicate, cross-workspace, and ambiguous bindings to be
    # rejected. They get distinct codes because a caller has to tell them apart:
    # an ambiguous binding needs a human to choose, a duplicate needs a
    # different trial, and a sealed suite needs a new suite entirely. One shared
    # code would leave that to string matching on a message.
    ApiErrorCode.TRIAL_BINDING_AMBIGUOUS: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "The trial carries no stable address of its own, so FR-091 permits only "
            "an explicit one-to-one choice. The importer never guesses a binding "
            "from list position, similar text, or timestamps."
        ),
        spec_ref="FR-091",
        provenance="project",
    ),
    ApiErrorCode.TRIAL_ALREADY_BOUND: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "The trial, or the run it names, is already bound in this suite. §17.1: "
            "a source run cannot be counted twice in one benchmark."
        ),
        spec_ref="FR-091 / §17.1",
        provenance="project",
    ),
    ApiErrorCode.BENCHMARK_BINDINGS_SEALED: ApiErrorSpec(
        http_status=409,
        retryable=False,
        description=(
            "Bindings became immutable when the suite entered `ready`. §16.4: a "
            "changed binding requires a new suite."
        ),
        spec_ref="§16.4",
        provenance="project",
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
    ApiErrorCode.ORIGIN_NOT_ALLOWED: ApiErrorSpec(
        http_status=403,
        retryable=False,
        description=(
            "A mutating request carried an Origin the deployment does not serve. "
            "§20.1 requires validating it; the request is refused before any "
            "handler runs, so nothing is mutated."
        ),
        spec_ref="§20.1",
        provenance="project",
    ),
    ApiErrorCode.RESOURCE_NOT_FOUND: ApiErrorSpec(
        http_status=404,
        retryable=False,
        description=(
            "The resource does not exist in the calling workspace. Deliberately "
            "404 rather than 403: FR-006 rejects access to another workspace's "
            "resource 'even when its identifier is known', and a 403 would "
            "confirm that the identifier names something real."
        ),
        spec_ref="FR-006",
        provenance="project",
    ),
    ApiErrorCode.RATE_LIMIT_EXCEEDED: ApiErrorSpec(
        http_status=429,
        retryable=True,
        description=(
            "A per-IP or workspace-creation token bucket was exhausted. Retryable "
            "because the request was refused before doing anything: FR-009 "
            "requires a limit response to commit no mutation, so repeating it "
            "later cannot duplicate one."
        ),
        spec_ref="FR-009",
        provenance="project",
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
    ApiErrorCode.HARNESS_ERROR: ApiErrorSpec(
        http_status=500,
        retryable=False,
        description=(
            "The harness itself failed. Carries no internal detail: §15.8 forbids "
            "exceptions and stack traces reaching a browser tool, so the code is "
            "the whole of what a client learns."
        ),
        spec_ref="§22",
        provenance="project",
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


class ApiError(Exception):
    """A deliberate refusal, carrying the stable code a client branches on.

    Raised anywhere in the service and translated once, at the boundary, into
    §15.8's envelope. Raising rather than returning matters: a handler that has
    to *remember* to return an error envelope will eventually forget, and the
    forgotten path becomes a 200 with a half-built body.

    `details` follows §15.8's shape — a list of `{path, message}` objects — so a
    field-level rejection can name every offending field at once rather than
    making a caller fix them one round trip at a time.
    """

    def __init__(
        self,
        code: ApiErrorCode,
        message: str,
        *,
        details: Sequence[Mapping[str, str]] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = [dict(detail) for detail in details]

    @property
    def spec(self) -> ApiErrorSpec:
        return API_ERROR_DESCRIPTIONS[self.code]

    @property
    def http_status(self) -> int:
        return self.spec.http_status

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    def as_envelope(self) -> dict[str, object]:
        """§15.8's one wire shape.

        `retryable` is read from the registry rather than passed in, so a call
        site cannot advertise a rejected intent as safe to repeat. The registry
        test already forbids a 4xx from being retryable; this makes that
        guarantee reach the wire.
        """
        return {
            "error": {
                "code": str(self.code),
                "message": self.message,
                "retryable": self.retryable,
                "details": list(self.details),
            }
        }


def error_from_core(exc: Exception) -> ApiError:
    """Translate a core `CoreError` into the API's vocabulary.

    The core deliberately carries no HTTP status (it must install alone), so
    this is where a domain failure acquires one. An unmapped core code becomes
    `HARNESS_ERROR` rather than leaking its text: an unrecognised failure is the
    case where an internal detail is most likely to be in the message.
    """
    from actionwitness_core.kernel import CoreError, CoreErrorCode

    if not isinstance(exc, CoreError):
        return ApiError(ApiErrorCode.HARNESS_ERROR, "The harness could not complete the request.")

    mapping = {
        CoreErrorCode.CONTRACT_VALIDATION_FAILED: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.INVALID_OBSERVATION_PATH: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.INVALID_REDACTION_PATTERN: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.EVALUATION_INPUT_INVALID: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.NON_FINITE_NUMBER: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.NUMBER_NOT_REPRESENTABLE: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.UNSUPPORTED_JSON_TYPE: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.CANONICALIZATION_FAILED: ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        CoreErrorCode.RESOURCE_LIMIT_EXCEEDED: ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED,
        # §16: "invalid non-reset state transitions shall return HTTP 409".
        CoreErrorCode.INVALID_STATE_TRANSITION: ApiErrorCode.RUN_IN_PROGRESS,
    }
    code = mapping.get(exc.code)
    if code is None:
        return ApiError(ApiErrorCode.HARNESS_ERROR, "The harness could not complete the request.")
    return ApiError(
        code,
        exc.message,
        details=[{"path": detail.location, "message": detail.message} for detail in exc.details],
    )
