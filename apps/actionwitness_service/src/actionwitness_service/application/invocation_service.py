"""One target-tool invocation (FR-031, FR-032, FR-033, FR-036, FR-008, §15.3).

The pipeline, and why its boundaries fall where they do:

1. **Validate** the arguments against the tool's published schema. Before
   anything is written, because an argument the tool never accepts should not
   produce a start event claiming an invocation began.
2. **Observe** canonical state. I/O, so no lock and no transaction is held
   (ADR-0003).
3. **Reserve and start** — one transaction: re-check the run state, reserve the
   event budget (FR-008), move `armed` to `running` on the first action, and
   append the start event. FR-031: "every selected-target tool invocation,
   including reads, shall record a start event **before business logic
   executes**."
4. **Dispatch** through the adapter. I/O.
5. **Observe again**, immediately. This is the independent read that a verdict
   can rest on, and taking it here rather than at verification is what makes
   per-call false-success evidence possible at all (§12.2).
6. **Terminate** — one transaction, **exactly one** terminal event.

**The self-report and the observation are recorded in different places, on
purpose.** `ToolExecutionResult.state_version_after` is the version the *tool's
own response body* claimed; the `events.state_version_after` column holds what
the observation provider *independently saw*. FR-032 calls the column value
"canonical", and canonical means observed. A tool that reports success while
changing nothing therefore produces an event whose reported status says
`success` and whose observed state hash is unchanged — which is the disagreement
this entire product exists to surface. Collapsing the two into one column would
delete the evidence.

**Exactly one terminal event.** Two would make the timeline ambiguous about what
happened; zero would make a hung invocation indistinguishable from one that
never started. Dispatch and observation are both allowed to fail, and every one
of those paths converges on a single terminal append.

`generic` is meant literally: nothing here branches on a tool name. The adapter
publishes specs and executes; §9.1's protocols are the only thing this knows
about the target, and a branch on `update_cart` here would put commerce
semantics in the harness.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from actionwitness_core.evidence.effects import (
    bounded,
    effect_context,
    effect_evidence,
    redacted_observation,
)
from actionwitness_core.journeys.enums import (
    ConfirmationStatus,
    EventActor,
    OutcomeEventType,
    RunState,
)
from actionwitness_core.journeys.transitions import validate_run_transition
from actionwitness_core.ports.models import Observation, TargetToolSpec, ToolExecutionResult
from actionwitness_core.ports.schemas import validate_arguments
from actionwitness_core.security.limits import MAX_TOOL_RESULT_CHARS
from actionwitness_core.security.redaction import RedactionPolicy, redact

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry
from actionwitness_service.application.authorization import WorkspaceScope, not_found
from actionwitness_service.application.confirmation_service import (
    CONFIRMATION_EVENT_RESERVATION,
    ConfirmationRequirement,
    ConfirmationService,
    binding_hash,
    confirmation_requirement,
    consequence_summary,
    expiry_from,
)
from actionwitness_service.application.guidance_service import GuidanceRecorder, current_guidance
from actionwitness_service.application.limits import WorkspaceCeilings
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.locks import WorkspaceLocks
from actionwitness_service.persistence.repositories import EventRepository, new_id

__all__ = ["INVOCABLE_RUN_STATES", "InvocationOutcome", "InvocationService"]

#: The run states in which a target action is accepted. §11.5: `Armed` and
#: `Running` publish target tools; `Verifying` publishes "status only", and
#: FR-038 makes a late action lose with `RUN_ALREADY_VERIFYING`.
INVOCABLE_RUN_STATES: Final[frozenset[str]] = frozenset(
    {str(RunState.ARMED.value), str(RunState.RUNNING.value)}
)

#: `verification_started`, the final `snapshot_captured`, and
#: `verification_completed` — the events verification writes whatever the
#: contract says (§16.1). Per-check events are counted on top of these.
_VERIFICATION_FIXED_EVENTS: Final = 3


@dataclass(frozen=True)
class InvocationOutcome:
    """What the caller is told. The self-report, labelled as one."""

    invocation_id: str
    sequence_number: int
    #: `None` while a protected action waits on a human: §14 keeps the tool's
    #: promise pending, and an invocation that has not finished has no terminal
    #: event to name. The one-terminal-event rule is preserved, not relaxed —
    #: the terminal event is written when the decision resolves the invocation.
    terminal_event: str | None
    reported_status: str | None
    reported_summary: str
    error_code: str | None
    duration_ms: int
    #: Independently observed, not reported. Named so the two cannot be confused
    #: by a reader of this object either.
    observed_state_version: str | None
    observed_state_changed: bool
    next_action: Mapping[str, object]
    #: Set only when the invocation paused for consent. The caller renders the
    #: dialog from this and keeps its tool promise pending.
    confirmation: Mapping[str, object] | None = None

    @property
    def awaiting_confirmation(self) -> bool:
        return self.confirmation is not None


class InvocationService:
    """Runs one invocation end to end."""

    def __init__(
        self,
        database: Database,
        registry: AdapterRegistry,
        locks: WorkspaceLocks,
        *,
        clock: Callable[[], datetime] | None = None,
        id_source: Callable[[], str] | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._locks = locks
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_source = id_source or (lambda: new_id("inv"))

    async def invoke(
        self,
        workspace_id: str,
        run_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        tool_identity_hash: str | None = None,
    ) -> InvocationOutcome:
        # 1 — the run must be this workspace's and open to actions.
        async with self._database.reading() as work:
            run = await self._invocable_run(work, workspace_id, run_id)
            # §20.3: the contract's own paths are applied "in addition to
            # defaults", so the policy is read from the contract this run was
            # armed against rather than from whatever is selected now.
            document = await self._contract_document(work, run)
            policy = _redaction_policy_of(document)
            # §14: which actions need a human is a statement about the journey
            # being judged, so it is read from the armed contract rather than
            # from a list of tool names the harness keeps.
            requirement = confirmation_requirement(document, tool_name)
            # FR-008 counts every persisted event, and verification will write
            # one per assertion and per policy. Holding that budget back now is
            # what keeps the ceiling true without ever truncating a verdict.
            reservation = await self._verification_reservation(work, run)
        if requirement is not None:
            # The request and the decision still have to fit inside FR-008's
            # ceiling. A run that could not record the decision it is waiting
            # for would be stranded awaiting a confirmation it can never
            # resolve, which is worse than refusing the action outright.
            reservation += CONFIRMATION_EVENT_RESERVATION
        adapter = self._registry.adapter(str(run["target_adapter_id"]))
        spec = _require_published(adapter, tool_name)

        # Arguments are validated before anything is written: an argument the
        # tool never accepts must not produce a start event claiming otherwise.
        checked = validate_arguments(dict(spec.input_schema), arguments, tool_name=tool_name)

        invocation_id = self._id_source()
        correlation_id = invocation_id
        request_id = f"req_{invocation_id}"

        # 2 — canonical state before the call. I/O, outside every lock.
        before = await self._observe(adapter, workspace_id, policy)

        # 3 — reserve, transition, and record the start (FR-031, FR-008).
        started_sequence, pending = await self._start_or_trip(
            workspace_id,
            run_id,
            spec,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            request_id=request_id,
            tool_identity_hash=tool_identity_hash,
            before=before,
            policy=policy,
            arguments=checked,
            verification_reservation=reservation,
            requirement=requirement,
        )

        # A protected action stops here. Nothing is dispatched, so no mutation
        # can precede the consent that authorizes it — which is the property
        # AC-06 checks and the reason this returns before the adapter is
        # touched rather than after.
        if pending is not None:
            return await self._paused(
                workspace_id,
                run_id,
                invocation_id=invocation_id,
                started_sequence=started_sequence,
                before=before,
                pending=pending,
            )

        # 4 and 5 — dispatch, then observe immediately. Both may fail; every
        # path below converges on exactly one terminal event.
        result: ToolExecutionResult | None = None
        failure: str | None = None
        started_at = self._clock()
        try:
            result = await adapter.execute(
                workspace_id,
                tool_name,
                checked,
                _context(workspace_id, run_id, invocation_id, request_id, correlation_id),
            )
        except Exception as exc:
            # FR-033: persisted as an error event "without leaking internal
            # details to the agent". The type is recorded; the message is not.
            failure = type(exc).__name__

        after = await self._observe_or_none(adapter, workspace_id, policy)
        duration_ms = max(0, int((self._clock() - started_at).total_seconds() * 1000))

        # 6 — exactly one terminal event.
        return await self._terminate(
            workspace_id,
            run_id,
            spec,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            request_id=request_id,
            started_sequence=started_sequence,
            before=before,
            after=after,
            result=result,
            failure=failure,
            duration_ms=duration_ms,
            policy=policy,
            arguments=checked,
        )

    # -- steps ---------------------------------------------------------------

    async def _start_or_trip(
        self, *args: Any, **kwargs: Any
    ) -> tuple[int, Mapping[str, Any] | None]:
        """`_start`, with FR-008's ceiling refusal raised after its commit.

        Split out so the raise happens outside the `async with`: raising inside
        would roll back the boundary event that explains the stop, which is the
        bug 004-T8 caught and this must not reintroduce.
        """
        sequence, pending = await self._start(*args, **kwargs)
        if sequence == 0:
            raise ApiError(
                ApiErrorCode.EVENT_LIMIT_EXCEEDED,
                "This run reached its event ceiling. It has been moved to error and its "
                "evidence is preserved.",
            )
        return sequence, pending

    async def _invocable_run(
        self, work: UnitOfWork, workspace_id: str, run_id: str
    ) -> Mapping[str, Any]:
        """FR-036: events belong only to the currently armed run of this workspace."""
        run = await WorkspaceScope(work, workspace_id).run(run_id)
        status = str(run["status"])
        if status in INVOCABLE_RUN_STATES:
            return run
        if status == str(RunState.VERIFYING.value):
            # FR-038's rejection. It "creates no finding and no
            # `tool_execution_error`" — so it is raised here, before any event
            # is written, rather than recorded as a failed invocation.
            raise ApiError(
                ApiErrorCode.RUN_ALREADY_VERIFYING,
                "Verification has started for this run; no further target actions are accepted.",
            )
        raise ApiError(
            ApiErrorCode.RUN_TIMELINE_SEALED,
            f"This run is {status} and accepts no further target actions.",
        )

    async def _contract_document(
        self, work: UnitOfWork, run: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The contract this run was *armed against*, or `None`.

        Not whatever the workspace has selected now: FR-025 locks the armed
        contract, and reading the current one would judge this run's evidence
        by a rule it was never run under — and, since §14's confirmation
        requirement is read from the same document, would let a contract
        swapped mid-run remove a consent gate.
        """
        contract_id = run.get("contract_id")
        if not contract_id:
            return None
        row = await work.fetch_one(
            "SELECT document_json FROM contracts WHERE id = ?", (str(contract_id),)
        )
        if row is None:  # pragma: no cover - the contract is immutable once armed
            return None
        document: dict[str, Any] = json.loads(row["document_json"])
        return document

    async def _verification_reservation(self, work: UnitOfWork, run: Mapping[str, Any]) -> int:
        """How many events verification will need for this run's contract.

        `verification_started`, the final `snapshot_captured`, one
        `assertion_evaluated` per assertion, one `policy_evaluated` per policy,
        and `verification_completed` (§16.1). Exact rather than a margin,
        because the contract is fixed at arming (FR-012) — a guess would either
        waste budget or fail to prevent the overrun it exists to prevent.
        """
        contract_id = run.get("contract_id")
        if not contract_id:
            return _VERIFICATION_FIXED_EVENTS
        row = await work.fetch_one(
            "SELECT document_json FROM contracts WHERE id = ?", (str(contract_id),)
        )
        if row is None:  # pragma: no cover - the contract is immutable once armed
            return _VERIFICATION_FIXED_EVENTS
        document = json.loads(row["document_json"])
        checks = len(document.get("assertions") or []) + len(document.get("policies") or [])
        return _VERIFICATION_FIXED_EVENTS + checks

    async def _observe(
        self, adapter: Any, workspace_id: str, policy: RedactionPolicy
    ) -> Observation:
        """Capture, then redact before anything is hashed or stored (§20.3)."""
        observation = await adapter.observation_provider().capture(workspace_id)
        return redacted_observation(observation, policy)

    async def _observe_or_none(
        self, adapter: Any, workspace_id: str, policy: RedactionPolicy
    ) -> Observation | None:
        """The post-call read, which is allowed to fail.

        Constitution §5 makes an observation failure an explicit non-pass rather
        than a degradation — but the *invocation* still terminated, and refusing
        to record its terminal event would leave the timeline claiming a call
        that never ended. So the failure is recorded as an absent observation
        and the verdict deals with it.
        """
        try:
            return await self._observe(adapter, workspace_id, policy)
        except Exception:
            # Broad on purpose: an adapter may fail in any way its transport
            # does, and none of those ways should stop the invocation being
            # recorded as terminated. What makes this safe is the counterpart
            # test — an honest mutation must report `state_changed: true`, so a
            # bug that made every observation fail cannot pass silently. It has
            # already caught one.
            return None

    async def _start(
        self,
        workspace_id: str,
        run_id: str,
        spec: TargetToolSpec,
        *,
        invocation_id: str,
        correlation_id: str,
        request_id: str,
        tool_identity_hash: str | None,
        before: Observation,
        policy: RedactionPolicy,
        arguments: Mapping[str, Any],
        verification_reservation: int,
        requirement: ConfirmationRequirement | None = None,
    ) -> tuple[int, Mapping[str, Any] | None]:
        """Reserve the budget, open the run, and record the start (FR-031, FR-008).

        The event-budget refusal is *returned* by the ceiling rather than raised,
        because FR-008 requires the boundary event and the run's move to `error`
        to be committed. So it is carried out of the transaction and raised
        after the commit — the same shape 004-T8 established.
        """
        refusal: ApiError | None = None

        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            run = await self._invocable_run(work, workspace_id, run_id)
            refusal = await WorkspaceCeilings(work, workspace_id).trip_if_event_budget_exhausted(
                run_id, reserved=verification_reservation
            )
            if refusal is not None:
                # The transaction still commits: it is carrying the boundary
                # event that explains why this run stopped.
                return 0, None

            if str(run["status"]) == str(RunState.ARMED.value):
                # §11.5: "Armed --> Running: first target action". Validated
                # through the core's table rather than assumed, so an illegal
                # transition is refused by the one authority on transitions.
                validate_run_transition(RunState.ARMED, RunState.RUNNING)
                await work.execute(
                    "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ?",
                    (str(RunState.RUNNING.value), run_id, workspace_id),
                )
                await GuidanceRecorder(work, workspace_id).transition(
                    await current_guidance(work, workspace_id), run_id=run_id
                )

            started = await EventRepository(work).append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.TOOL_INVOCATION_STARTED.value),
                    "actor": str(EventActor.AGENT.value),
                    "tool_name": spec.name,
                    "tool_identity_hash": tool_identity_hash,
                    "correlation_id": correlation_id,
                    "request_id": request_id,
                    # Observed, not reported: nothing has been dispatched yet,
                    # so there is no self-report to confuse this with.
                    "state_version_before": before.state_version,
                    "state_hash_before": before.content_hash(),
                    "redacted_payload": {
                        "invocation_id": invocation_id,
                        "side_effect": str(spec.side_effect.value),
                        "retry": str(spec.retry.value),
                        # FR-032's redacted inputs, recorded on the *start*
                        # event: they are what the call was made with, and they
                        # are known before it returns. §20.3 requires the
                        # redaction to happen before persistence, so it happens
                        # here rather than to an already-stored row.
                        "arguments": bounded(redact(dict(arguments), policy)),
                    },
                },
            )

            if requirement is None:
                return started, None

            # §14.1: the request is created here, in the *same* transaction as
            # the start event it belongs to. A confirmation without its start
            # event would be consent for an action the timeline never records
            # being attempted; a start event without its confirmation would be
            # an invocation nothing can ever resolve.
            confirmations = ConfirmationService(work, workspace_id)
            expires_at = expiry_from(self._clock(), requirement)
            consequence = consequence_summary(
                tool_name=spec.name,
                arguments=redact(dict(arguments), policy),
                observed=before,
                effect_paths=[str(path) for path in spec.effect_paths],
                policy=policy,
            )
            confirmation_id = await confirmations.open(
                run_id=run_id,
                correlation_id=correlation_id,
                tool_name=spec.name,
                # Bound to what was independently observed, never to what a
                # tool said: an approval is consent about the world as it is.
                state_binding_hash=binding_hash(before),
                consequence=consequence,
                expires_at=expires_at,
            )

            # §11.5: `Running --> AwaitingConfirmation`. Through the core's
            # table, so the one authority on transitions approves it.
            validate_run_transition(RunState.RUNNING, RunState.AWAITING_CONFIRMATION)
            await work.execute(
                "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ?",
                (str(RunState.AWAITING_CONFIRMATION.value), run_id, workspace_id),
            )

            await EventRepository(work).append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.CONFIRMATION_REQUESTED.value),
                    # The *agent* asked; the human decides. Recording the
                    # requester as the human would make the timeline say
                    # somebody consented to being asked.
                    "actor": str(EventActor.AGENT.value),
                    "tool_name": spec.name,
                    "correlation_id": correlation_id,
                    "request_id": request_id,
                    "status": str(ConfirmationStatus.PENDING.value),
                    "state_version_before": before.state_version,
                    "state_hash_before": before.content_hash(),
                    "redacted_payload": {
                        "confirmation_id": confirmation_id,
                        "expires_at": expires_at.isoformat(),
                        "timeout_seconds": requirement.timeout_seconds,
                        "consequence": consequence,
                    },
                },
            )

            # FR-120: the active actor is now the human approver, and the
            # banner, the tool result, and the action history all read this one
            # projection rather than each deciding for themselves.
            await GuidanceRecorder(work, workspace_id).transition(
                await current_guidance(work, workspace_id), run_id=run_id
            )

            return started, {
                "confirmation_id": confirmation_id,
                "expires_at": expires_at.isoformat(),
                "consequence": consequence,
                "correlation_id": correlation_id,
                "tool_name": spec.name,
            }

    async def _paused(
        self,
        workspace_id: str,
        run_id: str,
        *,
        invocation_id: str,
        started_sequence: int,
        before: Observation,
        pending: Mapping[str, Any],
    ) -> InvocationOutcome:
        """What the agent is told while a human decides.

        Deliberately *not* an error. The action has not failed and has not
        succeeded; it is waiting, and §14.3 keeps the caller's tool promise
        pending. Reporting a failure here would teach an agent to retry, which
        is exactly the behaviour a consent gate exists to prevent.
        """
        async with self._database.reading() as work:
            guidance = await current_guidance(work, workspace_id)
        return InvocationOutcome(
            invocation_id=invocation_id,
            sequence_number=started_sequence,
            terminal_event=None,
            reported_status=None,
            reported_summary=(
                f"{pending['tool_name']} is paused for a human decision. No change has been made."
            ),
            error_code=None,
            duration_ms=0,
            observed_state_version=before.state_version,
            observed_state_changed=False,
            next_action=guidance.next_action(),
            confirmation=dict(pending),
        )

    async def _terminate(
        self,
        workspace_id: str,
        run_id: str,
        spec: TargetToolSpec,
        *,
        invocation_id: str,
        correlation_id: str,
        request_id: str,
        started_sequence: int,
        before: Observation,
        after: Observation | None,
        result: ToolExecutionResult | None,
        failure: str | None,
        duration_ms: int,
        policy: RedactionPolicy,
        arguments: Mapping[str, Any],
    ) -> InvocationOutcome:
        terminal = (
            OutcomeEventType.TOOL_INVOCATION_FAILED if result is None else result.terminal_event
        )
        observed_changed = after is not None and after.content_hash() != before.content_hash()

        payload: dict[str, Any] = {
            "invocation_id": invocation_id,
            "side_effect": str(spec.side_effect.value),
            "started_at_sequence": started_sequence,
            # The self-report, kept together and labelled. Everything under this
            # key is what the tool said about itself; nothing under it is
            # evidence of what happened (constitution §4).
            "reported": _reported(result, failure, policy),
            # The engine's view of the same reading: shaped as an evaluation
            # context so FR-055 can resolve an assertion's own path against it
            # (`RunEvent.post_call_effect_state`). Stored alongside the audit
            # view rather than instead of it — one is read by a person, the
            # other by the classifier.
            "post_call_effect_state": effect_context(
                spec.effect_paths,
                None if after is None else after.as_context(),
                policy=policy,
            ),
            # FR-032's declared target-effect evidence, so idempotency and
            # false-success evidence "do not depend on tool-return text or later
            # actions". An adapter that declares no effect paths gets an empty
            # mapping rather than a guess (§12.2).
            "effects": effect_evidence(
                spec.effect_paths,
                before=before.as_context(),
                after=None if after is None else after.as_context(),
                policy=policy,
            ),
            # The independent read. `observed` and `reported` are siblings so a
            # reader cannot mistake one for the other.
            "observed": {
                "available": after is not None,
                "state_changed": observed_changed,
            },
        }

        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            sequence = await EventRepository(work).append(
                run_id,
                {
                    "event_type": str(terminal.value),
                    "actor": str(EventActor.AGENT.value),
                    "tool_name": spec.name,
                    "correlation_id": correlation_id,
                    "request_id": request_id,
                    "status": str(terminal.value),
                    # FR-032: required on a completion, absent on the failed and
                    # cancelled event types, which carry their outcome in the
                    # event name.
                    "reported_status": (
                        None
                        if result is None or result.reported_status is None
                        else str(result.reported_status.value)
                    ),
                    "duration_ms": duration_ms,
                    # "canonical state_version_before and state_version_after" —
                    # canonical means observed. The tool's own claim lives in
                    # `reported` above and never in these columns.
                    "state_version_before": before.state_version,
                    "state_hash_before": before.content_hash(),
                    "state_version_after": None if after is None else after.state_version,
                    "state_hash_after": None if after is None else after.content_hash(),
                    "redacted_payload": payload,
                },
            )
            # FR-121's compact `next_action`, derived from the workspace as it
            # now stands rather than from the phase this handler assumed. An
            # invocation that tripped the event ceiling leaves the run in
            # `error`, and telling the caller to carry on invoking would be a
            # server inventing a next action no state supports.
            guidance = await current_guidance(work, workspace_id)

        return InvocationOutcome(
            invocation_id=invocation_id,
            sequence_number=sequence,
            terminal_event=str(terminal.value),
            reported_status=(
                None
                if result is None or result.reported_status is None
                else str(result.reported_status.value)
            ),
            reported_summary="" if result is None else result.reported_summary,
            error_code=failure if result is None else result.error_code,
            duration_ms=duration_ms,
            observed_state_version=None if after is None else after.state_version,
            observed_state_changed=observed_changed,
            next_action=guidance.next_action(),
        )


def _reported(
    result: ToolExecutionResult | None,
    failure: str | None,
    policy: RedactionPolicy | None = None,
) -> dict[str, Any]:
    """The tool-reported channel, serialized under one key.

    A failure that never produced a result still belongs here: what the harness
    knows is that the call did not come back, and that is a fact about the tool
    channel rather than about the target's state.
    """
    if result is None:
        return {
            "status": None,
            "summary": "",
            # The exception's *type*, never its message (FR-033, §20).
            "error_code": failure,
            "state_version_after": None,
        }
    return {
        "status": None if result.reported_status is None else str(result.reported_status.value),
        # Bounded and redacted: §23.3 keeps the tool's own text out of storage
        # at full length, and a self-report is as likely to carry a secret as
        # any other untrusted string.
        "summary": bounded(redact(result.reported_summary, policy), limit=MAX_TOOL_RESULT_CHARS),
        "error_code": result.error_code,
        # Deliberately kept: the version the tool *claimed*, beside the version
        # that was observed. A disagreement between them is evidence.
        "state_version_after": result.state_version_after,
    }


def _redaction_policy_of(document: Mapping[str, Any] | None) -> RedactionPolicy:
    """The run's redaction policy: defaults plus the contract's own paths.

    A run without a contract still gets the defaults. §20.3 applies contract
    paths "in addition to defaults", so there is no configuration that turns
    redaction off.
    """
    if not document:
        return RedactionPolicy()
    paths = ((document.get("redaction") or {}).get("paths")) or []
    return RedactionPolicy.from_paths([str(path) for path in paths])


def _require_published(adapter: Any, tool_name: str) -> TargetToolSpec:
    """The allowlist, checked before anything else (§20.2, FR-015).

    A name the adapter never published is a 404 rather than a validation error:
    the tool does not exist for this caller, and saying anything more precise
    would describe a surface they were not shown.
    """
    for spec in adapter.tool_specs():
        if spec.name == tool_name:
            return spec
    raise not_found()


def _context(
    workspace_id: str, run_id: str, invocation_id: str, request_id: str, correlation_id: str
) -> Any:
    from actionwitness_core.ports.models import ExecutionContext

    return ExecutionContext(
        workspace_id=workspace_id,
        run_id=run_id,
        invocation_id=invocation_id,
        request_id=request_id,
        correlation_id=correlation_id,
        # Constitution §5 gives every logical mutation a stable key. One
        # invocation is one logical intent, so the invocation's own identity is
        # that key — a retry of the same intent reuses it by reusing the id.
        idempotency_key=invocation_id,
        actor=EventActor.AGENT,
    )
