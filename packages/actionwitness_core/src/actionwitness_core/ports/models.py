"""The data crossing the target boundary: descriptors, selections, results, observations.

Spec v1.9 §9.1 (`TargetDescriptor`, `ScenarioSelection`, `TargetToolSpec`),
§9.3 (an observation is "a named value captured from a trusted provider and
mounted under an adapter-declared namespace"), §13.4 (the declared effect map),
FR-032 (what a terminal invocation records); constitution §4 ("Tool-reported
output and authoritative observations use distinct stored types and source
classifications" and "A successful tool response must never be persisted as
manufactured observed state").

The distinction between `ToolExecutionResult` and `Observation` is the product.
One is what the thing under test *said*; the other is what was *seen*
independently of it. They are separate types with separate source
classifications and no constructor, method, or helper that turns either into the
other - so manufacturing an observation from a successful tool response is not
something a caller can do by accident, and doing it deliberately means writing
code that visibly does it.

Target neutrality is the other constraint. Nothing here knows what a cart is:
`scenario_mode` and `fault_profile` are opaque adapter-declared tokens that the
core validates against the descriptor and never interprets, which is what keeps
`pre_fix` out of a library that a support-ticket target must also be able to use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from actionwitness_core.contracts.paths import ObservationPathField
from actionwitness_core.evidence.enums import EvidenceSourceClassification, ToolReportedStatus
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType
from actionwitness_core.kernel import ContractError, CoreErrorCode, CoreModel, JsonValue, UtcInstant
from actionwitness_core.ports.enums import ExecutionMode, RetrySemantics, SideEffectClass
from actionwitness_core.security.canonical import content_hash
from actionwitness_core.security.limits import (
    MAX_TOOL_DESCRIPTION_CHARS,
    MAX_TOOL_NAME_CHARS,
    MAX_TOOL_RESULT_CHARS,
)

__all__ = [
    "TERMINAL_INVOCATION_EVENTS",
    "ExecutionContext",
    "Observation",
    "ScenarioSelection",
    "TargetDescriptor",
    "TargetToolSpec",
    "ToolExecutionResult",
]

#: The three event types a tool invocation may terminate with (§16.1, FR-032).
TERMINAL_INVOCATION_EVENTS: frozenset[OutcomeEventType] = frozenset(
    {
        OutcomeEventType.TOOL_INVOCATION_COMPLETED,
        OutcomeEventType.TOOL_INVOCATION_FAILED,
        OutcomeEventType.TOOL_INVOCATION_CANCELLED,
    }
)

#: An identifier-shaped token: adapter IDs, provider IDs, namespaces, provenance.
#: Bounded and restricted because every one of these reaches a report, a log
#: line, and a hash input, and none of them is free text.
type Token = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
]

type ToolName = Annotated[str, StringConstraints(min_length=1, max_length=MAX_TOOL_NAME_CHARS)]

type Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class TargetDescriptor(CoreModel):
    """What an adapter publishes about the target it drives (§9.1).

    `supported_scenario_modes` is the whole of the core's scenario knowledge:
    §9.1 says the core "validates the selected value against
    `TargetDescriptor.supported_scenario_modes` but neither interprets mode names
    nor implements a fault". A Shopify adapter advertising only
    `external_current` therefore disables the pre/post control without the core
    knowing what `pre_fix` would have meant.
    """

    target_type: Token
    target_id: Identifier
    execution_mode: ExecutionMode
    supported_scenario_modes: Annotated[tuple[Token, ...], Field(min_length=1)]

    def supports(self, scenario_mode: str) -> bool:
        return scenario_mode in self.supported_scenario_modes


class ScenarioSelection(CoreModel):
    """The immutable scenario configuration copied into a run (§9.1, §16).

    Every field is an opaque token or a hash reference. The core compares them,
    stores them, and validates the mode against the descriptor; it interprets
    none of them.
    """

    scenario_mode: Token
    fault_profile: Token | None = None
    fixture_reference: Identifier | None = None
    build_reference: Identifier | None = None

    def validate_for(self, descriptor: TargetDescriptor) -> None:
        """Refuse a mode the selected adapter does not advertise."""
        if not descriptor.supports(self.scenario_mode):
            raise ContractError(
                f"scenario mode {self.scenario_mode!r} is not supported by target "
                f"{descriptor.target_id!r}; it advertises "
                f"{sorted(descriptor.supported_scenario_modes)}",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )


class TargetToolSpec(CoreModel):
    """One allowlisted target tool (§9.1, §11.4, §13.4).

    `effect_paths` carries the §13.4 declared target-effect prefixes. An adapter
    that publishes none still gets generic assertion evaluation; §12.2 is
    explicit that "missing effect metadata disables only causal false-success
    attribution" and must never make the harness infer an effect.
    """

    name: ToolName
    description: Annotated[str, StringConstraints(max_length=MAX_TOOL_DESCRIPTION_CHARS)] = ""
    input_schema: Mapping[str, JsonValue] = Field(default_factory=dict)
    side_effect: SideEffectClass
    retry: RetrySemantics
    effect_paths: tuple[ObservationPathField, ...] = ()

    @model_validator(mode="after")
    def _check_effects_match_the_side_effect_class(self) -> TargetToolSpec:
        if self.side_effect is SideEffectClass.READ_ONLY and self.effect_paths:
            raise ContractError(
                f"tool {self.name!r} is declared read-only but publishes effect paths; "
                "§13.4 lists a read-only tool's effects as none",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        if self.side_effect is SideEffectClass.READ_ONLY and self.retry not in {
            RetrySemantics.READ_ONLY_SAFE
        }:
            raise ContractError(
                f"tool {self.name!r} is read-only, so its retry semantics must be read_only_safe",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self


class ExecutionContext(CoreModel):
    """Everything one invocation is bound to (§9.1, FR-032, FR-036).

    `idempotency_key` is required rather than optional because constitution §5
    gives every logical mutation a stable key: an optional field would let a
    caller omit it on the one retry path where it mattered. A read-only call
    still carries one, which costs nothing and removes the branch.
    """

    workspace_id: Identifier
    run_id: Identifier
    invocation_id: Identifier
    request_id: Identifier
    correlation_id: Identifier
    idempotency_key: Identifier
    actor: EventActor
    #: Whether a human has authorized *this* invocation (§14, FR-060).
    #:
    #: Target-neutral on purpose: the harness states that consent exists and the
    #: adapter decides what its target needs in order to honour it — a store
    #: confirmation record, a signed header, nothing at all. The alternative,
    #: passing a target's own confirmation identifier through the harness, would
    #: put one target's consent mechanism in a field every other target has to
    #: understand.
    #:
    #: Defaults to `False` so an adapter that forgets to check it fails closed,
    #: and so a caller cannot omit the field on the one path where it matters.
    human_consent_granted: bool = False


class ToolExecutionResult(CoreModel):
    """What a tool reported about its own call. Evidence, never proof.

    Source classification is fixed at `tool_reported` and cannot be set by a
    caller: the whole value of this record is that it stays labelled as the
    channel under test even after it is stored, summarised, and replayed.

    The result carries a *bounded* summary rather than the tool's payload (§23.3,
    FR-032). Full evidence lives server-side; putting it here would both blow the
    §11.4 tool-context budget and invite a reader to treat a self-report as state.
    """

    tool_name: ToolName
    terminal_event: OutcomeEventType
    reported_status: ToolReportedStatus | None = None
    reported_summary: Annotated[str, StringConstraints(max_length=MAX_TOOL_RESULT_CHARS)] = ""
    error_code: Token | None = None
    request_id: Identifier
    correlation_id: Identifier
    duration_ms: Annotated[int, Field(ge=0)] = 0
    state_version_before: Identifier | None = None
    state_version_after: Identifier | None = None

    @property
    def source_classification(self) -> EvidenceSourceClassification:
        """Always `tool_reported`. Not a field, so it cannot be overridden."""
        return EvidenceSourceClassification.TOOL_REPORTED

    @model_validator(mode="after")
    def _check_terminal_shape(self) -> ToolExecutionResult:
        if self.terminal_event not in TERMINAL_INVOCATION_EVENTS:
            raise ContractError(
                f"{self.terminal_event.value!r} is not a terminal invocation event",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
        completed = self.terminal_event is OutcomeEventType.TOOL_INVOCATION_COMPLETED
        # FR-032: `reported_status` "is required on tool_invocation_completed";
        # the failed and cancelled event types carry their outcome in the event
        # name and must not also claim a self-reported status.
        if completed and self.reported_status is None:
            raise ContractError(
                "a completed invocation must record its reported status",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
        if not completed and self.reported_status is not None:
            raise ContractError(
                f"{self.terminal_event.value!r} carries no reported status",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
        return self

    def claims_success(self) -> bool:
        """True when the tool said it did what it was asked (FR-055).

        Named as a claim on purpose. This is one half of false-success detection;
        the other half is an independent observation that disagrees.
        """
        return self.reported_status is ToolReportedStatus.SUCCESS


class Observation(CoreModel):
    """Authoritative state captured independently of the tool under test (§9.3).

    Mounted under an adapter-declared `namespace`, so the same engine resolves
    `target.cart.total` for a store and `target.ticket.status` for a support
    application without knowing what either means.

    `state_version` is metadata, not business payload (§9.3), so it is a sibling
    of the payload rather than a key inside it - otherwise a contract could
    assert on it through `target.state_version` and a provider that exposes no
    monotonic version would silently change what a contract asserts.
    """

    namespace: Token
    provider_id: Token
    provenance: Token
    schema_version: str
    payload: Mapping[str, JsonValue]
    state_version: Identifier | None = None
    captured_at: UtcInstant

    @property
    def source_classification(self) -> EvidenceSourceClassification:
        """Always `authoritative_observation`. Not a field, so it cannot be set."""
        return EvidenceSourceClassification.AUTHORITATIVE_OBSERVATION

    def as_context(self) -> dict[str, JsonValue]:
        """The evaluation context this observation contributes (§9.3)."""
        return {self.namespace: dict(self.payload)}

    def content_hash(self) -> str:
        """The `sha256:...` hash of the observed payload (§17.2, FR-042).

        Hashes the payload the caller supplies, which is the redacted payload by
        the time an observation is built: §20.3 requires redaction "before
        persistence, hashing, or export", so redacting after this point would
        produce a hash describing a document nobody stored.
        """
        return content_hash(dict(self.payload))
