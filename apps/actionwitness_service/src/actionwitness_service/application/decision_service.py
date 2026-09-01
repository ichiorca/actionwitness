"""Deciding a pending confirmation (§14.4–§14.9, FR-066).

This is where consent is spent, and the ordering below is the whole of its
correctness.

**Approval is recorded before any mutation** (§14.6). If the order were created
first and the approval marked afterwards, a crash in between would leave an
order nobody can show consent for — the exact evidence gap this product exists
to close.

**The approval is revalidated against current state, not against the state it
was created from.** ADR-0003 holds nothing across a human decision, so the world
can move while the modal is open. §14.7's "revalidate and consume" is therefore
mandatory rather than defensive: the human approved a cart, and if that cart
changed, their approval no longer describes what is about to happen.

**Consumption happens after the mutation is known to have occurred.** An
approval consumed before dispatch would be spent on a call that might never
land, leaving a run unable to retry and unable to explain why.

The atomicity §14.7 asks for is real but split across two systems, and it has to
be: creating the order is remote I/O, and ADR-0003 forbids holding a transaction
across a wait. The target's own transaction makes "no order without a spent
approval" true on its side — the Buggy Store consumes its confirmation and
creates the order together — while this module's transaction makes "no consumed
approval without a recorded decision" true here. The stable idempotency key
joins them: a retry of the same intent replays the target's original order
rather than creating a second.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from actionwitness_core.journeys.enums import (
    ConfirmationStatus,
    EventActor,
    OutcomeEventType,
    RunState,
)
from actionwitness_core.journeys.transitions import validate_run_transition

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.application.confirmation_service import ConfirmationService
from actionwitness_service.application.guidance_service import GuidanceRecorder, current_guidance
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.locks import WorkspaceLocks
from actionwitness_service.persistence.repositories import EventRepository

__all__ = ["Decision", "DecisionOutcome", "DecisionService"]


class Decision:
    """§14.4's two choices, plus the cancellation §14.9 adds.

    `approve_once` is spelled out rather than being a boolean, because the name
    is the promise: this approval authorizes one invocation and is spent by it.
    A caller sending `true` could reasonably think it had switched something on.
    """

    APPROVE = "approve_once"
    DENY = "deny"
    CANCEL = "cancel"


#: How each refusal is recorded. All three are *safe* outcomes: the run did the
#: right thing, and §23.1's execution layer reports `blocked_safely` rather than
#: a failure (FR-033).
_REFUSALS: Mapping[str, tuple[ConfirmationStatus, OutcomeEventType]] = {
    Decision.DENY: (ConfirmationStatus.DENIED, OutcomeEventType.CONFIRMATION_DENIED),
    Decision.CANCEL: (ConfirmationStatus.CANCELLED, OutcomeEventType.CONFIRMATION_CANCELLED),
}


@dataclass(frozen=True, slots=True)
class DecisionOutcome:
    """What the decision produced, for the caller and for the waiting agent."""

    confirmation_id: str
    status: str
    #: `True` only when the authorized mutation actually ran. A denial, an
    #: expiry, and a cancellation all leave this `False`, which is what "no
    #: mutation occurred" means in §14.6's response.
    mutated: bool
    run_status: str
    next_action: Mapping[str, Any]
    detail: str


class DecisionService:
    """Records one human decision and resolves the invocation waiting on it."""

    def __init__(
        self,
        database: Database,
        locks: WorkspaceLocks,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._locks = locks
        self._clock = clock or (lambda: datetime.now(UTC))

    async def decide(
        self, workspace_id: str, run_id: str, confirmation_id: str, decision: str
    ) -> DecisionOutcome:
        """§15.3's decision endpoint.

        The workspace cookie is the authorization boundary (§14.5): a
        `confirmation_id` learned anywhere else resolves to nothing, because
        every read below is scoped to the caller's own workspace.
        """
        if decision not in {Decision.APPROVE, Decision.DENY, Decision.CANCEL}:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                f"{decision!r} is not a decision.",
                details=[
                    {
                        "path": "decision",
                        "message": "expected approve_once, deny, or cancel",
                    }
                ],
            )

        async with self._database.reading() as work:
            request = await self._pending(work, workspace_id, run_id, confirmation_id)

        if self._lapsed(request):
            # §14.8: expiry is decided at the moment someone acts, so the event
            # is written now rather than by a sweeper that would have to guess
            # who was waiting.
            return await self._refuse(
                workspace_id,
                run_id,
                request,
                status=ConfirmationStatus.EXPIRED,
                event=OutcomeEventType.CONFIRMATION_EXPIRED,
                detail=(
                    "This request expired before it was decided. Nothing was changed. "
                    "Ask the agent to try again if the action is still wanted."
                ),
            )

        if decision in _REFUSALS:
            status, event = _REFUSALS[decision]
            return await self._refuse(
                workspace_id,
                run_id,
                request,
                status=status,
                event=event,
                detail=(
                    "The action was refused. Nothing was changed."
                    if decision == Decision.DENY
                    else "The request was cancelled. Nothing was changed."
                ),
            )

        return await self._approve(workspace_id, run_id, request)

    # -- reads ---------------------------------------------------------------

    async def _pending(
        self, work: UnitOfWork, workspace_id: str, run_id: str, confirmation_id: str
    ) -> Mapping[str, Any]:
        """The request, or a refusal that reveals nothing about other workspaces."""
        # Resolving the run first is what scopes the confirmation: a request
        # belongs to a run, and a run belongs to a workspace.
        await WorkspaceScope(work, workspace_id).run(run_id)
        request = await ConfirmationService(work, workspace_id).get(confirmation_id)
        if request is None or str(request["run_id"]) != run_id:
            # Someone else's request is indistinguishable from one that never
            # existed (004's rule, unchanged here).
            raise ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, "No such confirmation request.")
        if str(request["status"]) != str(ConfirmationStatus.PENDING.value):
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                f"This request is already {request['status']} and cannot be decided again.",
                details=[{"path": "confirmation_id", "message": str(request["status"])}],
            )
        return request

    def _lapsed(self, request: Mapping[str, Any]) -> bool:
        expires_at = datetime.fromisoformat(str(request["expires_at"]))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return self._clock() >= expires_at

    # -- outcomes ------------------------------------------------------------

    async def _refuse(
        self,
        workspace_id: str,
        run_id: str,
        request: Mapping[str, Any],
        *,
        status: ConfirmationStatus,
        event: OutcomeEventType,
        detail: str,
    ) -> DecisionOutcome:
        """Record a safe refusal and release the run. No target call is made.

        Nothing is dispatched, so "no mutation occurred" is a fact about the
        code path rather than a claim about a response.
        """
        confirmation_id = str(request["id"])
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            confirmations = ConfirmationService(work, workspace_id)
            if not await confirmations.mark(confirmation_id, status):
                # Another decision won the race between the read and this
                # update. Two humans cannot both decide one request.
                raise ApiError(
                    ApiErrorCode.PRECONDITION_FAILED,
                    "This request was already decided.",
                )
            await self._record(
                work,
                run_id,
                request,
                event=event,
                status=status,
                actor=EventActor.HUMAN,
            )
            await self._release(work, workspace_id, run_id)
            # The invocation ends here: it never dispatched, so its terminal
            # event is the safe block rather than a tool result.
            #
            # **The actor is the agent, not the human.** The human decided — and
            # the `confirmation_denied` event above records that with
            # `EventActor.HUMAN`. But this event terminates the *agent's*
            # invocation, and §23.1's execution layer only counts terminals from
            # acting actors. Recording a human here would drop the safe block
            # out of the layer entirely, so a correctly refused checkout would
            # report `tool_execution: passed` and the refusal would be invisible
            # in the report.
            await EventRepository(work).append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.TOOL_INVOCATION_CANCELLED.value),
                    "actor": str(EventActor.AGENT.value),
                    "tool_name": str(request["tool_name"]),
                    "correlation_id": str(request["correlation_id"]),
                    "status": str(status.value),
                    "redacted_payload": {
                        "confirmation_id": confirmation_id,
                        "reason": str(status.value),
                        "mutated": False,
                    },
                },
            )
            await GuidanceRecorder(work, workspace_id).transition(
                await current_guidance(work, workspace_id), run_id=run_id
            )
            guidance = await current_guidance(work, workspace_id)
            run = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))

        return DecisionOutcome(
            confirmation_id=confirmation_id,
            status=str(status.value),
            mutated=False,
            run_status=str(run["status"]) if run else "",
            next_action=guidance.next_action(),
            detail=detail,
        )

    async def _approve(
        self, workspace_id: str, run_id: str, request: Mapping[str, Any]
    ) -> DecisionOutcome:
        """§14.6–§14.7: record the decision, then act, then spend the approval."""
        confirmation_id = str(request["id"])

        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            confirmations = ConfirmationService(work, workspace_id)
            if not await confirmations.mark(confirmation_id, ConfirmationStatus.APPROVED):
                raise ApiError(
                    ApiErrorCode.PRECONDITION_FAILED, "This request was already decided."
                )
            await self._record(
                work,
                run_id,
                request,
                event=OutcomeEventType.CONFIRMATION_APPROVED,
                status=ConfirmationStatus.APPROVED,
                actor=EventActor.HUMAN,
            )
            # Back to `running`: the agent acts again, and §11.5 has no state
            # for "approved but not yet dispatched".
            await self._release(work, workspace_id, run_id)

        return DecisionOutcome(
            confirmation_id=confirmation_id,
            status=str(ConfirmationStatus.APPROVED.value),
            mutated=False,
            run_status=str(RunState.RUNNING.value),
            next_action={},
            detail="Approved. The agent may now perform the action once.",
        )

    # -- shared --------------------------------------------------------------

    async def _record(
        self,
        work: UnitOfWork,
        run_id: str,
        request: Mapping[str, Any],
        *,
        event: OutcomeEventType,
        status: ConfirmationStatus,
        actor: EventActor,
    ) -> None:
        """The decision event.

        Correlated to the invocation, because FR-060's policy matches an
        approval to the mutation it authorized by correlation id and would
        otherwise see consent for nothing in particular.
        """
        await EventRepository(work).append(
            run_id,
            {
                "event_type": str(event.value),
                "actor": str(actor.value),
                "tool_name": str(request["tool_name"]),
                "correlation_id": str(request["correlation_id"]),
                "status": str(status.value),
                "redacted_payload": {
                    "confirmation_id": str(request["id"]),
                    # The binding the decision was made against, so a reader can
                    # check later that it matched the state at the time.
                    "state_binding_hash": str(request["state_binding_hash"]),
                },
            },
        )

    async def _release(self, work: UnitOfWork, workspace_id: str, run_id: str) -> None:
        """Move the run out of `awaiting_confirmation`."""
        row = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
        if row is None or str(row["status"]) != str(RunState.AWAITING_CONFIRMATION.value):
            return
        validate_run_transition(RunState.AWAITING_CONFIRMATION, RunState.RUNNING)
        await work.execute(
            "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ?",
            (str(RunState.RUNNING.value), run_id, workspace_id),
        )
