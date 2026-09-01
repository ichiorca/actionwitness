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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from actionwitness_core.journeys.enums import (
    EventActor,
    OutcomeEventType,
    RunState,
    WorkspacePhase,
)
from actionwitness_core.journeys.guidance import derive_guidance
from actionwitness_core.journeys.transitions import validate_run_transition
from actionwitness_core.ports.models import Observation, TargetToolSpec, ToolExecutionResult
from actionwitness_core.ports.schemas import validate_arguments

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry
from actionwitness_service.application.authorization import WorkspaceScope, not_found
from actionwitness_service.application.guidance_service import GuidanceRecorder
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


@dataclass(frozen=True)
class InvocationOutcome:
    """What the caller is told. The self-report, labelled as one."""

    invocation_id: str
    sequence_number: int
    terminal_event: str
    reported_status: str | None
    reported_summary: str
    error_code: str | None
    duration_ms: int
    #: Independently observed, not reported. Named so the two cannot be confused
    #: by a reader of this object either.
    observed_state_version: str | None
    observed_state_changed: bool
    next_action: Mapping[str, object]


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
        adapter = self._registry.adapter(str(run["target_adapter_id"]))
        spec = _require_published(adapter, tool_name)

        # Arguments are validated before anything is written: an argument the
        # tool never accepts must not produce a start event claiming otherwise.
        checked = validate_arguments(dict(spec.input_schema), arguments, tool_name=tool_name)

        invocation_id = self._id_source()
        correlation_id = invocation_id
        request_id = f"req_{invocation_id}"

        # 2 — canonical state before the call. I/O, outside every lock.
        before = await self._observe(adapter, workspace_id)

        # 3 — reserve, transition, and record the start (FR-031, FR-008).
        started_sequence = await self._start_or_trip(
            workspace_id,
            run_id,
            spec,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            request_id=request_id,
            tool_identity_hash=tool_identity_hash,
            before=before,
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

        after = await self._observe_or_none(adapter, workspace_id)
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
        )

    # -- steps ---------------------------------------------------------------

    async def _start_or_trip(self, *args: Any, **kwargs: Any) -> int:
        """`_start`, with FR-008's ceiling refusal raised after its commit.

        Split out so the raise happens outside the `async with`: raising inside
        would roll back the boundary event that explains the stop, which is the
        bug 004-T8 caught and this must not reintroduce.
        """
        sequence = await self._start(*args, **kwargs)
        if sequence == 0:
            raise ApiError(
                ApiErrorCode.EVENT_LIMIT_EXCEEDED,
                "This run reached its event ceiling. It has been moved to error and its "
                "evidence is preserved.",
            )
        return sequence

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

    async def _observe(self, adapter: Any, workspace_id: str) -> Observation:
        return await adapter.observation_provider().capture(workspace_id)

    async def _observe_or_none(self, adapter: Any, workspace_id: str) -> Observation | None:
        """The post-call read, which is allowed to fail.

        Constitution §5 makes an observation failure an explicit non-pass rather
        than a degradation — but the *invocation* still terminated, and refusing
        to record its terminal event would leave the timeline claiming a call
        that never ended. So the failure is recorded as an absent observation
        and the verdict deals with it.
        """
        try:
            return await self._observe(adapter, workspace_id)
        except Exception:
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
    ) -> int:
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
                run_id
            )
            if refusal is not None:
                # The transaction still commits: it is carrying the boundary
                # event that explains why this run stopped.
                return 0

            if str(run["status"]) == str(RunState.ARMED.value):
                # §11.5: "Armed --> Running: first target action". Validated
                # through the core's table rather than assumed, so an illegal
                # transition is refused by the one authority on transitions.
                validate_run_transition(RunState.ARMED, RunState.RUNNING)
                await work.execute(
                    "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ?",
                    (str(RunState.RUNNING.value), run_id, workspace_id),
                )
                await GuidanceRecorder(work, workspace_id).append(
                    derive_guidance(WorkspacePhase.RUNNING, correlation_id=run_id),
                    run_id=run_id,
                )

            return await EventRepository(work).append(
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
                    },
                },
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
            "reported": _reported(result, failure),
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
            next_action=derive_guidance(
                WorkspacePhase.RUNNING, correlation_id=run_id
            ).next_action(),
        )


def _reported(result: ToolExecutionResult | None, failure: str | None) -> dict[str, Any]:
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
        "summary": result.reported_summary,
        "error_code": result.error_code,
        # Deliberately kept: the version the tool *claimed*, beside the version
        # that was observed. A disagreement between them is evidence.
        "state_version_after": result.state_version_after,
    }


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
