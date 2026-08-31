"""Closed state and event enums (spec v1.9 §16, §17.1).

These are the project's shared vocabulary. API handlers, the React workspace, the
event recorder, and the tests all name states and events from here, so the names
cannot fork between layers — which is the whole point of registering them before
any handler exists (BUILD_ORDER §7/M0).

Every enum here is **closed**: a value outside it is invalid input, not an unknown
extension. Every member carries a description, and `tests/unit/test_registry.py`
fails if a member is added without one.

This module owns the *lifecycle* half of that vocabulary. Contract, port, evidence,
engine, and report vocabularies live beside the code that uses them, and
`actionwitness_core.registry` composes all of them into the single exported
registry — so a reader looking for the assertion operators finds them in
`contracts/`, not here.

Scope boundary: this module is target-neutral, so it holds no Shopify pairing
states and no HTTP semantics. Shopify's Tier 3 vocabulary belongs to
`integrations.shopify`; API error codes carry status codes and live in
`actionwitness_service.api.errors`.

Transition validation is M1 work and deliberately absent — this module registers
names, not rules.
"""

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "ENUM_REGISTRATIONS",
    "BenchmarkSuiteState",
    "EvalRunState",
    "EvaluationEventType",
    "EventActor",
    "GuidanceActor",
    "OutcomeEventType",
    "RunState",
    "SnapshotPhase",
    "WorkspaceKind",
]


class RunState(StrEnum):
    """Outcome-run states (spec §16)."""

    ARMED = "armed"
    PROPOSING = "proposing"
    CAPTURING = "capturing"
    PROPOSED = "proposed"
    RUNNING = "running"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    VERIFYING = "verifying"
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    FAILED = "failed"
    ERROR = "error"
    CANCELLED = "cancelled"


RUN_STATE_DESCRIPTIONS: Mapping[RunState, str] = {
    RunState.ARMED: "Verification run created and initial snapshot captured.",
    RunState.PROPOSING: (
        "Proposal-mode run created and initial snapshot captured; no contract is being judged."
    ),
    RunState.CAPTURING: "Proposal closed; final snapshot taken and candidates being derived.",
    RunState.PROPOSED: "Candidate list committed; terminal, and carries no verdict.",
    RunState.RUNNING: "Tool events being recorded.",
    RunState.AWAITING_CONFIRMATION: "Sensitive tool is paused pending a human decision.",
    RunState.VERIFYING: "Final snapshot and evaluation in progress.",
    RunState.PASSED: "All critical checks passed.",
    RunState.PASSED_WITH_WARNINGS: "No critical failure; warnings exist.",
    RunState.FAILED: "One or more critical checks failed.",
    RunState.ERROR: "Harness could not complete the run.",
    RunState.CANCELLED: "User reset or cancelled before verification completed.",
}


class EvalRunState(StrEnum):
    """Evaluation-run states (spec §16.2)."""

    QUEUED = "queued"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ERROR = "error"


EVAL_RUN_STATE_DESCRIPTIONS: Mapping[EvalRunState, str] = {
    EvalRunState.QUEUED: "Eval run accepted.",
    EvalRunState.RUNNING: "Fixture restored and trajectory replaying.",
    EvalRunState.PASSED: "Actual outcome matches the selected mode's expectation.",
    EvalRunState.FAILED: (
        "Valid execution completed but actual outcome differs from the selected mode's expectation."
    ),
    EvalRunState.CANCELLED: "User or workspace cancelled replay.",
    EvalRunState.ERROR: "Case or harness execution was invalid.",
}


class BenchmarkSuiteState(StrEnum):
    """Benchmark-suite states (spec §16.4, Tier 2)."""

    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


BENCHMARK_SUITE_STATE_DESCRIPTIONS: Mapping[BenchmarkSuiteState, str] = {
    BenchmarkSuiteState.DRAFT: "Manifest created; imports or bindings incomplete.",
    BenchmarkSuiteState.READY: "All intended imports and bindings validate.",
    BenchmarkSuiteState.RUNNING: "Imported trajectories are replaying.",
    BenchmarkSuiteState.COMPLETED: "Immutable benchmark artifact finalized.",
    BenchmarkSuiteState.CANCELLED: "Workspace reset or user cancelled nonterminal work.",
    BenchmarkSuiteState.ERROR: "Import, replay, or finalization could not complete safely.",
}


class OutcomeEventType(StrEnum):
    """Outcome-run event types (spec §16.1). Append-only; corrections are new events."""

    RUN_ARMED = "run_armed"
    GUIDANCE_TRANSITIONED = "guidance_transitioned"
    SNAPSHOT_CAPTURED = "snapshot_captured"
    EXTERNAL_OBSERVATION_RECEIVED = "external_observation_received"
    TOOL_INVOCATION_STARTED = "tool_invocation_started"
    TOOL_INVOCATION_COMPLETED = "tool_invocation_completed"
    TOOL_INVOCATION_FAILED = "tool_invocation_failed"
    TOOL_INVOCATION_CANCELLED = "tool_invocation_cancelled"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMATION_APPROVED = "confirmation_approved"
    CONFIRMATION_DENIED = "confirmation_denied"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CONFIRMATION_CANCELLED = "confirmation_cancelled"
    VERIFICATION_STARTED = "verification_started"
    ASSERTION_EVALUATED = "assertion_evaluated"
    POLICY_EVALUATED = "policy_evaluated"
    VERIFICATION_COMPLETED = "verification_completed"
    RUN_CANCELLED = "run_cancelled"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    TOOL_SURFACE_CAPTURED = "tool_surface_captured"
    TOOL_SURFACE_CHANGED = "tool_surface_changed"
    ASSERTIONS_PROPOSED = "assertions_proposed"
    ANNOTATION_ADDED = "annotation_added"
    TOOL_IDENTITY_MISMATCH = "tool_identity_mismatch"
    AUDIT_AUTHORIZATION_ASSERTED = "audit_authorization_asserted"


OUTCOME_EVENT_DESCRIPTIONS: Mapping[OutcomeEventType, str] = {
    OutcomeEventType.RUN_ARMED: "Run created with its immutable configuration copied in.",
    OutcomeEventType.GUIDANCE_TRANSITIONED: (
        "Guidance moved to a new actor or action, referencing the guidance-event ID."
    ),
    OutcomeEventType.SNAPSHOT_CAPTURED: "An authoritative observation was recorded.",
    OutcomeEventType.EXTERNAL_OBSERVATION_RECEIVED: (
        "An external target's session-API observation was accepted as evidence."
    ),
    OutcomeEventType.TOOL_INVOCATION_STARTED: "A target tool invocation was accepted and begun.",
    OutcomeEventType.TOOL_INVOCATION_COMPLETED: (
        "A target tool invocation returned; carries the self-reported status, "
        "which is evidence and never proof."
    ),
    OutcomeEventType.TOOL_INVOCATION_FAILED: "A target tool invocation returned an error.",
    OutcomeEventType.TOOL_INVOCATION_CANCELLED: (
        "A target tool invocation was aborted before its commit won the race."
    ),
    OutcomeEventType.CONFIRMATION_REQUESTED: (
        "A protected mutation created a pending human confirmation."
    ),
    OutcomeEventType.CONFIRMATION_APPROVED: "A human approved a protected mutation once.",
    OutcomeEventType.CONFIRMATION_DENIED: "A human denied a protected mutation.",
    OutcomeEventType.CONFIRMATION_EXPIRED: "A pending confirmation passed its expiry unresolved.",
    OutcomeEventType.CONFIRMATION_CANCELLED: (
        "A pending confirmation was cancelled by reset or invocation abort."
    ),
    OutcomeEventType.VERIFICATION_STARTED: "The run transitioned atomically into verification.",
    OutcomeEventType.ASSERTION_EVALUATED: "One contract assertion produced a result.",
    OutcomeEventType.POLICY_EVALUATED: "One contract policy produced a result.",
    OutcomeEventType.VERIFICATION_COMPLETED: (
        "Terminal verification event; seals the run timeline and report."
    ),
    OutcomeEventType.RUN_CANCELLED: "The run was cancelled before verification completed.",
    OutcomeEventType.RESOURCE_LIMIT_EXCEEDED: (
        "A hard ceiling was reached; recorded as a boundary event rather than "
        "dropping evidence silently."
    ),
    OutcomeEventType.TOOL_SURFACE_CAPTURED: (
        "The armed tool-surface baseline was recorded; idempotent by "
        "(run_id, surface_content_hash)."
    ),
    OutcomeEventType.TOOL_SURFACE_CHANGED: "An observed tool-surface delta was recorded.",
    OutcomeEventType.ASSERTIONS_PROPOSED: (
        "Terminal proposal event; seals a proposal run's timeline and carries no verdict."
    ),
    OutcomeEventType.ANNOTATION_ADDED: (
        "A human annotated one event; never affects verdict, assertions, or eligibility."
    ),
    OutcomeEventType.TOOL_IDENTITY_MISMATCH: (
        "A pre-invocation tool-identity check failed. Distinct from "
        "tool_surface_changed because it must be recordable when no toolchange "
        "was observed at all."
    ),
    OutcomeEventType.AUDIT_AUTHORIZATION_ASSERTED: (
        "An operator asserted authorization for an external audited origin."
    ),
}


class EvaluationEventType(StrEnum):
    """Evaluation-run event types (spec §16.3).

    Reuses the outcome event names the shared policy engine needs and adds the
    replay boundary events. These belong only to an `evaluation_run_id` and never
    appear in the source outcome run's timeline.
    """

    SNAPSHOT_CAPTURED = "snapshot_captured"
    TOOL_INVOCATION_STARTED = "tool_invocation_started"
    TOOL_INVOCATION_COMPLETED = "tool_invocation_completed"
    TOOL_INVOCATION_FAILED = "tool_invocation_failed"
    TOOL_INVOCATION_CANCELLED = "tool_invocation_cancelled"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMATION_APPROVED = "confirmation_approved"
    CONFIRMATION_DENIED = "confirmation_denied"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CONFIRMATION_CANCELLED = "confirmation_cancelled"
    ASSERTION_EVALUATED = "assertion_evaluated"
    POLICY_EVALUATED = "policy_evaluated"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    EVAL_REPLAY_STARTED = "eval_replay_started"
    EVAL_REPLAY_COMPLETED = "eval_replay_completed"
    EVAL_REPLAY_CANCELLED = "eval_replay_cancelled"
    EVAL_REPLAY_FAILED = "eval_replay_failed"


EVALUATION_EVENT_DESCRIPTIONS: Mapping[EvaluationEventType, str] = {
    EvaluationEventType.SNAPSHOT_CAPTURED: "An authoritative observation was recorded in replay.",
    EvaluationEventType.TOOL_INVOCATION_STARTED: "A replayed tool invocation was begun.",
    EvaluationEventType.TOOL_INVOCATION_COMPLETED: "A replayed tool invocation returned.",
    EvaluationEventType.TOOL_INVOCATION_FAILED: "A replayed tool invocation returned an error.",
    EvaluationEventType.TOOL_INVOCATION_CANCELLED: "A replayed tool invocation was aborted.",
    EvaluationEventType.CONFIRMATION_REQUESTED: (
        "Replay reached a protected mutation requiring consent."
    ),
    EvaluationEventType.CONFIRMATION_APPROVED: (
        "The recorded-approval interaction provider approved; never inferred consent."
    ),
    EvaluationEventType.CONFIRMATION_DENIED: "The recorded-denial interaction provider denied.",
    EvaluationEventType.CONFIRMATION_EXPIRED: "A replayed confirmation expired unresolved.",
    EvaluationEventType.CONFIRMATION_CANCELLED: "A replayed confirmation was cancelled.",
    EvaluationEventType.ASSERTION_EVALUATED: "One contract assertion produced a replay result.",
    EvaluationEventType.POLICY_EVALUATED: "One contract policy produced a replay result.",
    EvaluationEventType.RESOURCE_LIMIT_EXCEEDED: "A hard ceiling was reached during replay.",
    EvaluationEventType.EVAL_REPLAY_STARTED: "Fixture restored and replay begun.",
    EvaluationEventType.EVAL_REPLAY_COMPLETED: "Replay finished and its report was persisted.",
    EvaluationEventType.EVAL_REPLAY_CANCELLED: "Replay was cancelled by user or workspace reset.",
    EvaluationEventType.EVAL_REPLAY_FAILED: "The case definition or harness execution was invalid.",
}


class EventActor(StrEnum):
    """Who produced an event (spec §17.1 `events.actor`; §10.3, §23.1).

    `eval` is named by §10.3 ("actor `eval` in an eval replay") and §23.1
    ("eval-run reports count actor-`eval` invocation starts separately"), while
    §17.1's parenthetical list of the outcome stream's actors omits it - eval
    events live in the separate `evaluation_events` stream of §16.3. It is
    registered here so the trajectory engine can recognise a replayed occurrence
    from the start rather than having replay masquerade as an agent.
    """

    AGENT = "agent"
    HUMAN = "human"
    HARNESS = "harness"
    EXTERNAL = "external"
    EVAL = "eval"


EVENT_ACTOR_DESCRIPTIONS: Mapping[EventActor, str] = {
    EventActor.AGENT: "A browser agent acting through a registered tool.",
    EventActor.HUMAN: "A person; used for confirmation decisions and timeline annotations.",
    EventActor.HARNESS: "The server itself, for lifecycle and boundary events.",
    EventActor.EXTERNAL: "Used only for accepted external observations.",
    EventActor.EVAL: (
        "A deterministic eval replay driving recorded tool calls. Counted "
        "separately from agent calls so a replay is never presented as an agent "
        "having chosen those tools."
    ),
}


class GuidanceActor(StrEnum):
    """Who guidance is addressed to (spec §17.1 `guidance_events`, FR-120)."""

    OPERATOR = "operator"
    AGENT = "agent"
    HUMAN_APPROVER = "human_approver"
    SYSTEM = "system"


GUIDANCE_ACTOR_DESCRIPTIONS: Mapping[GuidanceActor, str] = {
    GuidanceActor.OPERATOR: "The person configuring the workspace and selecting a contract.",
    GuidanceActor.AGENT: "The browser agent exercising target tools.",
    GuidanceActor.HUMAN_APPROVER: (
        "The person deciding a protected mutation. Reserved to a human by design; "
        "an agent can never occupy this role."
    ),
    GuidanceActor.SYSTEM: "The server, while work proceeds without either person.",
}


class SnapshotPhase(StrEnum):
    """Which side of the journey a snapshot observes (spec §17.1, `snapshots.phase`)."""

    BEFORE = "before"
    AFTER = "after"


SNAPSHOT_PHASE_DESCRIPTIONS: Mapping[SnapshotPhase, str] = {
    SnapshotPhase.BEFORE: "The authoritative initial observation captured at arming.",
    SnapshotPhase.AFTER: "The authoritative final observation captured at verification.",
}


class WorkspaceKind(StrEnum):
    """Workspace kinds (spec §17.1, `workspaces.kind`)."""

    INTERACTIVE = "interactive"
    EVAL = "eval"


WORKSPACE_KIND_DESCRIPTIONS: Mapping[WorkspaceKind, str] = {
    WorkspaceKind.INTERACTIVE: "An anonymous human-facing workspace.",
    WorkspaceKind.EVAL: (
        "A server-created isolated workspace for replay; its mutable state is "
        "removed after its report is persisted."
    ),
}


#: Every lifecycle enum this module publishes, paired with its spec reference and
#: description map, in a stable order. `actionwitness_core.registry` concatenates
#: this with the other vocabulary modules; an enum missing from here is invisible
#: to the exporter, to the UI, and to the drift tests — which is what the
#: registry gate catches.
ENUM_REGISTRATIONS: tuple[tuple[str, str, type[StrEnum], Mapping[StrEnum, str]], ...] = (
    ("run_state", "spec §16", RunState, RUN_STATE_DESCRIPTIONS),
    ("eval_run_state", "spec §16.2", EvalRunState, EVAL_RUN_STATE_DESCRIPTIONS),
    (
        "benchmark_suite_state",
        "spec §16.4",
        BenchmarkSuiteState,
        BENCHMARK_SUITE_STATE_DESCRIPTIONS,
    ),
    ("outcome_event_type", "spec §16.1", OutcomeEventType, OUTCOME_EVENT_DESCRIPTIONS),
    ("evaluation_event_type", "spec §16.3", EvaluationEventType, EVALUATION_EVENT_DESCRIPTIONS),
    ("event_actor", "spec §17.1", EventActor, EVENT_ACTOR_DESCRIPTIONS),
    ("guidance_actor", "spec §17.1 / FR-120", GuidanceActor, GUIDANCE_ACTOR_DESCRIPTIONS),
    ("snapshot_phase", "spec §17.1", SnapshotPhase, SNAPSHOT_PHASE_DESCRIPTIONS),
    ("workspace_kind", "spec §17.1", WorkspaceKind, WORKSPACE_KIND_DESCRIPTIONS),
)
