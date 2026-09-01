"""The verification race gate and the exclusive run mutation lease (FR-038, FR-039).

FR-038, in full, because every clause of it is a test below:

> Verification shall **atomically** transition a run from `running` to
> `verifying` only when at least one recorded target action has a terminal event
> and no recorded agent invocation or confirmation is in flight. Once the
> transition succeeds, new target actions for that run shall be rejected with
> HTTP 409, code `RUN_ALREADY_VERIFYING`, and `retryable: false`; that rejection
> creates no finding and no `tool_execution_error`. A tool invocation that was
> still in network flight and had not recorded its start loses the race and
> receives the same rejection. Browser-side enabled-state checks are advisory
> UX; **FastAPI is the sole transition authority.** No invalid or racing request
> may capture a partial final snapshot.

**Atomically** is the load-bearing word, and it decides the shape of this
module. The precondition check and the status change are one `UPDATE … WHERE
status = 'running'` inside one transaction, so two concurrent verify requests
cannot both observe `running` and both proceed. A check-then-update written as
two statements would pass every single-client test and admit two verifications
under load — and two verifications mean two final snapshots, which is exactly
the "partial final snapshot" the last sentence forbids.

**"In flight" is defined by the timeline, not by a flag.** An invocation is in
flight when its start event has no matching terminal event under the same
correlation id. That definition survives a process restart, which a flag would
not: a server that died mid-invocation would come back with its flag cleared and
happily verify over the top of a call that never finished.

FR-039's lease is the other half. While a run occupies any of its four
non-terminal states, a *human* may not mutate target state directly — the rule
exists so that Phase 1 replay has no ambiguous human causality. Reads, reset,
and confirmation decisions stay available, which is why the guard names the
states rather than "any active run".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from actionwitness_core.journeys.enums import OutcomeEventType, RunState
from actionwitness_core.journeys.transitions import validate_run_transition

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.persistence.database import UnitOfWork

__all__ = ["LEASED_RUN_STATES", "VerificationGate", "require_no_lease"]

#: FR-039's four states. A run in any of them holds the mutation lease.
LEASED_RUN_STATES: Final[frozenset[str]] = frozenset(
    {
        str(RunState.ARMED.value),
        str(RunState.RUNNING.value),
        str(RunState.AWAITING_CONFIRMATION.value),
        str(RunState.VERIFYING.value),
    }
)

#: The events that open and close one invocation on the timeline.
_START: Final = str(OutcomeEventType.TOOL_INVOCATION_STARTED.value)
_TERMINAL: Final[frozenset[str]] = frozenset(
    {
        str(OutcomeEventType.TOOL_INVOCATION_COMPLETED.value),
        str(OutcomeEventType.TOOL_INVOCATION_FAILED.value),
        str(OutcomeEventType.TOOL_INVOCATION_CANCELLED.value),
    }
)


async def require_no_lease(work: UnitOfWork, workspace_id: str) -> None:
    """FR-039: refuse a direct human target mutation while a run holds the lease.

    Called by any surface through which a *person* could change target state
    while a run is in flight. Reads, reset, and confirmation decisions are
    deliberately not callers: FR-039 keeps all three available, and a guard
    applied to them would break the recovery paths the lease exists alongside.

    The harness has no such surface yet — the human store panel is M5's, and the
    Buggy Store's own `/demo` API belongs to the store (§15.5), which cannot be
    told about runs without breaking the boundary the architecture gate
    enforces. This is exported and tested now so that the panel is built against
    a rule that already exists rather than one invented alongside it.
    """
    placeholders = ",".join("?" for _ in LEASED_RUN_STATES)
    row = await work.fetch_one(
        f"SELECT id, status FROM runs WHERE workspace_id = ? AND status IN ({placeholders})",
        (workspace_id, *sorted(LEASED_RUN_STATES)),
    )
    if row is None:
        return
    raise ApiError(
        ApiErrorCode.RUN_MUTATION_LOCKED,
        f"A run is {row['status']}; direct changes to target state are locked until it "
        "finishes or the workspace is reset. Reads, reset, and confirmation decisions "
        "remain available.",
    )


class VerificationGate:
    """FR-038's transition, and only the transition.

    Capturing the final observation and evaluating the contract belong to the
    task that owns verification; this decides *whether* verification may start
    and makes the state change that locks the run against further actions. The
    split is deliberate — the race is decided before any observation is taken,
    so a losing request cannot capture a partial final snapshot.
    """

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    async def begin(self, run_id: str) -> Mapping[str, Any]:
        """Move `running` to `verifying`, atomically, or refuse.

        Returns the run row as it was before the transition, so a caller has the
        controlled inputs without a second read.
        """
        run = await self._require_run(run_id)
        status = str(run["status"])

        if status == str(RunState.VERIFYING.value):
            # Already ours or already somebody else's — either way this request
            # did not win, and FR-038 gives both the same answer.
            raise _already_verifying()
        if status != str(RunState.RUNNING.value):
            # Validated through the core's table so the one authority on
            # transitions decides, rather than a second opinion here.
            validate_run_transition(RunState(status), RunState.VERIFYING)

        # In-flight is checked first because in `running` it is the only thing
        # that can cause "nothing has completed": the timeline is append-only,
        # so a terminal event never disappears. Reporting the accurate reason
        # matters more than the order the requirement lists them in.
        await self._require_nothing_in_flight(run_id)
        await self._require_a_completed_action(run_id)

        # The atomic step. `WHERE status = 'running'` is the whole race: a
        # second request that reached here first has already moved the row, so
        # this update matches nothing and the caller is told it lost.
        updated = await self._work.execute(
            "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ? AND status = ?",
            (
                str(RunState.VERIFYING.value),
                run_id,
                self._workspace_id,
                str(RunState.RUNNING.value),
            ),
        )
        if updated.rowcount == 0:
            raise _already_verifying()

        return run

    async def _require_run(self, run_id: str) -> Mapping[str, Any]:
        from actionwitness_service.application.authorization import WorkspaceScope

        return await WorkspaceScope(self._work, self._workspace_id).run(run_id)

    async def _require_a_completed_action(self, run_id: str) -> None:
        """ "at least one recorded target action has a terminal event".

        Without one there is nothing to verify: the contract would be judged
        against a target nobody touched, and the report would describe an agent
        that never acted as having passed.

        Reached only defensively. An `armed` run is refused by the core's
        transition table before this, and a `running` run has either completed
        an action or has one in flight — so this fires only if the timeline is
        in a shape neither path produces.
        """
        placeholders = ",".join("?" for _ in _TERMINAL)
        row = await self._work.fetch_one(
            f"SELECT id FROM events WHERE run_id = ? AND event_type IN ({placeholders}) LIMIT 1",
            (run_id, *sorted(_TERMINAL)),
        )
        if row is None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "No target action has completed on this run, so there is nothing to verify.",
                details=[
                    {
                        "path": "run",
                        "message": "at least one target action must have a terminal event",
                    }
                ],
            )

    async def _require_nothing_in_flight(self, run_id: str) -> None:
        """ "no recorded agent invocation or confirmation is in flight".

        In flight is read off the timeline: a start event whose correlation id
        has no terminal event yet. Defining it that way rather than with a flag
        is what makes it survive a restart — a flag cleared by a crash would let
        verification run over the top of a call that never finished.
        """
        placeholders = ",".join("?" for _ in _TERMINAL)
        row = await self._work.fetch_one(
            f"""
            SELECT started.correlation_id AS correlation_id
              FROM events AS started
             WHERE started.run_id = ?
               AND started.event_type = ?
               AND NOT EXISTS (
                     SELECT 1 FROM events AS done
                      WHERE done.run_id = started.run_id
                        AND done.correlation_id = started.correlation_id
                        AND done.event_type IN ({placeholders})
                   )
             LIMIT 1
            """,
            (run_id, _START, *sorted(_TERMINAL)),
        )
        if row is not None:
            raise ApiError(
                ApiErrorCode.RUN_IN_PROGRESS,
                "A target action on this run has not finished. Verification waits for it "
                "so the final observation describes one settled moment.",
            )

        pending = await self._work.fetch_one(
            "SELECT id FROM confirmation_requests WHERE run_id = ? AND status = 'pending' LIMIT 1",
            (run_id,),
        )
        if pending is not None:
            raise ApiError(
                ApiErrorCode.RUN_IN_PROGRESS,
                "A confirmation on this run is still unresolved. Decide or cancel it "
                "before verifying.",
            )


def _already_verifying() -> ApiError:
    """FR-038's exact rejection, used for every way of losing the race."""
    return ApiError(
        ApiErrorCode.RUN_ALREADY_VERIFYING,
        "Verification has already started for this run.",
    )
