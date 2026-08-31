"""Recorded evidence: ordered run events and immutable snapshots.

Spec v1.9 §9.6 (what evidence must contain), §12.5 (snapshot capture, provenance,
immutability), §16.1 (events are append-only; "corrections before verification
completes shall be represented by a new event"), §17.1 (`events` and `snapshots`
columns), FR-032 (what a terminal invocation records), FR-034 (monotonic
sequence), FR-042 (snapshot provenance).

These records are the input to every policy: FR-050 makes determinism a property
of "the same snapshots **and the same recorded event stream**". So they are
frozen, they carry their own hash, and nothing here reaches back to a live
target - a policy evaluated twice over one recorded stream must answer twice the
same way, including a year later during a replay.

Notably absent is any way to build a `Snapshot` from a `ToolExecutionResult`. A
snapshot wraps an `Observation`, and the two channels stay unconvertible all the
way down (constitution §4).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from actionwitness_core.evidence.enums import ToolReportedStatus
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, SnapshotPhase
from actionwitness_core.kernel import ContractError, CoreErrorCode, CoreModel, JsonValue, UtcInstant
from actionwitness_core.ports.models import Observation

__all__ = [
    "INVOCATION_START_EVENTS",
    "TERMINAL_INVOCATION_EVENTS",
    "RunEvent",
    "Snapshot",
    "changed_state",
]

#: The single event that establishes an invocation occurrence (§10.3).
INVOCATION_START_EVENTS: frozenset[OutcomeEventType] = frozenset(
    {OutcomeEventType.TOOL_INVOCATION_STARTED}
)

#: The events an invocation may end with (§16.1, FR-032).
TERMINAL_INVOCATION_EVENTS: frozenset[OutcomeEventType] = frozenset(
    {
        OutcomeEventType.TOOL_INVOCATION_COMPLETED,
        OutcomeEventType.TOOL_INVOCATION_FAILED,
        OutcomeEventType.TOOL_INVOCATION_CANCELLED,
    }
)

type Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
type ContentHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class RunEvent(CoreModel):
    """One append-only entry in a run timeline (§16.1, §17.1).

    Every field the engine reads is here and nothing else is: an event carries a
    *redacted* payload and a *bounded* summary, never the tool's raw output
    (§20.3, §23.3).
    """

    sequence_number: Annotated[int, Field(ge=0)]
    event_type: OutcomeEventType
    actor: EventActor
    created_at: UtcInstant
    tool_name: str | None = None
    correlation_id: Identifier | None = None
    request_id: Identifier | None = None
    #: Present only on `tool_invocation_completed` (FR-032).
    reported_status: ToolReportedStatus | None = None
    state_version_before: Identifier | None = None
    state_version_after: Identifier | None = None
    state_hash_before: ContentHash | None = None
    state_hash_after: ContentHash | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    redacted_payload: Mapping[str, JsonValue] = Field(default_factory=dict)
    #: The immediate authoritative post-call observation of this tool's declared
    #: target-effect paths, as a namespace-rooted context fragment (FR-032:
    #: "bounded before/after values for their declared target-effect paths so
    #: idempotency and false-success evidence do not depend on tool-return text
    #: or later actions").
    #:
    #: This is observed state, not the tool's report, and it is the second half
    #: of FR-055's false-success test. `None` means no immediate observation was
    #: captured, which FR-055 treats as a reason to fall back to the generic
    #: classification rather than to infer causality.
    post_call_effect_state: Mapping[str, JsonValue] | None = None

    @model_validator(mode="after")
    def _check_invocation_shape(self) -> RunEvent:
        invocation = self.event_type in INVOCATION_START_EVENTS | TERMINAL_INVOCATION_EVENTS
        if invocation and not self.tool_name:
            raise ContractError(
                f"{self.event_type.value!r} must name the tool it concerns",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
        completed = self.event_type is OutcomeEventType.TOOL_INVOCATION_COMPLETED
        if not completed and self.reported_status is not None:
            raise ContractError(
                f"{self.event_type.value!r} carries no reported status (FR-032)",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
        return self

    @property
    def is_invocation_start(self) -> bool:
        return self.event_type in INVOCATION_START_EVENTS

    @property
    def reported_success(self) -> bool:
        """True when this completion claimed success. A claim, not a verdict."""
        return self.reported_status is ToolReportedStatus.SUCCESS


def changed_state(event: RunEvent) -> bool | None:
    """Did this event's canonical state hashes move?

    Returns `None` when the event recorded no hashes, which is genuinely
    different from "nothing changed": FR-032 requires mutation completions to
    record them, so their absence means the evidence cannot answer the question
    and a policy that needs it must say so rather than assume a pass.
    """
    if event.state_hash_before is None or event.state_hash_after is None:
        return None
    return event.state_hash_before != event.state_hash_after


class Snapshot(CoreModel):
    """One immutable authoritative observation bound to a run phase (§12.5, §17.1).

    Insert-only by construction: `FR-043` makes a snapshot immutable "from the
    moment it is created", and corrections require a new run rather than an
    update, so there is no method here that changes one.
    """

    run_id: Identifier
    phase: SnapshotPhase
    observation: Observation
    content_hash: ContentHash

    @classmethod
    def of(cls, run_id: str, phase: SnapshotPhase, observation: Observation) -> Snapshot:
        """Bind an observation to a run phase, hashing the payload it carries.

        The observation must already be redacted: §20.3 requires redaction
        "before persistence, hashing, or export", so hashing here and redacting
        later would store a hash of a document that was never stored.
        """
        return cls(
            run_id=run_id,
            phase=phase,
            observation=observation,
            content_hash=observation.content_hash(),
        )

    def verify(self) -> bool:
        """True when the stored hash still describes the stored observation."""
        return self.observation.content_hash() == self.content_hash

    @property
    def provider(self) -> str:
        """FR-042: a snapshot identifies its provider."""
        return self.observation.provider_id

    def as_context(self) -> dict[str, JsonValue]:
        """The evaluation context this snapshot contributes (§9.3)."""
        return self.observation.as_context()


def context_of(snapshot: Snapshot | None) -> Mapping[str, JsonValue] | None:
    """The evaluation context for a snapshot, or `None` when there was none.

    `None` propagates all the way to `observation_unavailable`; it is never
    silently replaced by an empty context, which would make every `absent`
    assertion pass against a target nobody observed.
    """
    return None if snapshot is None else snapshot.as_context()


def ordered(events: Sequence[RunEvent]) -> tuple[RunEvent, ...]:
    """Events in recorded sequence order (FR-034).

    Callers hand the engine whatever their repository returned; sorting once here
    means no policy has to trust that ordering, and two policies can never
    disagree about which call came first.
    """
    return tuple(sorted(events, key=lambda event: event.sequence_number))
