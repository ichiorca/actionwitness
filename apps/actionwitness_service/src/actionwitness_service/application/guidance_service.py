"""Persisting guidance transitions (FR-120, §12.13, §16.1, §17.1).

The core derives the guidance; this appends it. Two rules from §12.13 shape the
whole module:

**"Guidance before a run exists is recorded in the separate workspace-scoped
`guidance_events` stream. After arming, `guidance_transitioned` is also appended
to the run timeline using the same guidance-event ID."**

So a guidance transition can produce *two* rows — one in the workspace stream,
one in the run timeline — and they are joined by the guidance event's own
identifier rather than by timestamp or by position. A reader reconstructing "who
was asked to do what" follows that id; two rows that merely happened at the same
moment would be a guess.

**"Display-copy changes may alter future messages but never rewrite historical
actor, action code, correlation, or outcome evidence."**

So the row stores the rendered sentence *and* `copy_version` alongside the stable
`action_code`. A later release may say it differently; the historical row still
reports what was actually shown, and the code says what was actually meant.

`workspace_version` is allocated the same way event sequence numbers are:
`MAX + 1` scoped to the workspace, inside the appending transaction, with the
write lock already held. It is a per-workspace ordering for guidance that exists
*before* any run does, which is precisely the stretch the run timeline cannot
cover.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState
from actionwitness_core.journeys.guidance import (
    COPY_VERSION,
    GuidanceState,
    derive_guidance,
    phase_for,
)

from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import EventRepository, new_id

__all__ = ["GuidanceRecorder", "current_guidance"]


async def current_guidance(work: UnitOfWork, workspace_id: str) -> GuidanceState:
    """This workspace's guidance, derived from authoritative state.

    **The one place any surface asks "whose turn is it?"** FR-120 says FastAPI
    derives the guidance object and "the frontend shall not invent a conflicting
    next action" — and the same discipline has to hold on this side of the wire.
    A handler that chose a phase itself would be a second opinion with no more
    authority than the frontend's, and it would be wrong in exactly the
    situations guidance exists for: the ones where the run did not end up where
    the caller assumed.

    Read inside the caller's unit of work, *after* whatever state change
    prompted it, so the guidance describes the workspace as it now is rather
    than as it was when the request arrived.
    """
    row = await work.fetch_one(
        "SELECT selected_contract_id, active_run_id FROM workspaces WHERE id = ?",
        (workspace_id,),
    )
    if row is None:  # pragma: no cover - the middleware creates it first
        return derive_guidance(phase_for(has_contract=False, run_state=None))

    run_state: RunState | None = None
    correlation: str | None = None
    if row["active_run_id"]:
        run = await work.fetch_one(
            "SELECT id, status FROM runs WHERE id = ? AND workspace_id = ?",
            (row["active_run_id"], workspace_id),
        )
        if run is not None:
            run_state = RunState(run["status"])
            correlation = str(run["id"])

    return derive_guidance(
        phase_for(has_contract=bool(row["selected_contract_id"]), run_state=run_state),
        correlation_id=correlation,
    )


class GuidanceRecorder:
    """Appends guidance transitions for one workspace."""

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    async def append(
        self,
        guidance: GuidanceState,
        *,
        run_id: str | None = None,
        resolution: str | None = None,
    ) -> str:
        """Record one transition, returning its guidance-event id.

        When `run_id` is given, a `guidance_transitioned` event is also appended
        to that run's timeline carrying this id — §12.13's rule that the two
        streams are joined by the guidance event's identity.
        """
        guidance_id = new_id("gde")
        version = await self._next_version()

        await self._work.execute(
            """
            INSERT INTO guidance_events (
                id, workspace_id, run_id, workspace_version, phase,
                active_actor, next_actor, action_code, copy_version,
                instruction, reason, expected_consequence, waiting_for,
                recovery_action_code, correlation_id, resolution, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guidance_id,
                self._workspace_id,
                run_id,
                version,
                str(guidance.phase.value),
                str(guidance.active_actor.value),
                None if guidance.next_actor is None else str(guidance.next_actor.value),
                _code(guidance.action_code),
                COPY_VERSION,
                guidance.instruction,
                guidance.reason,
                guidance.expected_consequence,
                guidance.waiting_for,
                _code(guidance.recovery_action_code),
                guidance.correlation_id or guidance_id,
                resolution,
                self._work.now(),
            ),
        )

        if run_id is not None:
            await EventRepository(self._work).append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.GUIDANCE_TRANSITIONED.value),
                    # `harness`, not the guidance's own `active_actor`: the event
                    # records that *the server* moved guidance, not that the
                    # person or agent it is addressed to did something.
                    "actor": str(EventActor.HARNESS.value),
                    "correlation_id": guidance.correlation_id or guidance_id,
                    "redacted_payload": {
                        "guidance_event_id": guidance_id,
                        "phase": str(guidance.phase.value),
                        "active_actor": str(guidance.active_actor.value),
                        "action_code": _code(guidance.action_code),
                    },
                },
            )

        return guidance_id

    async def transition(self, guidance: GuidanceState, *, run_id: str | None = None) -> str | None:
        """Append this guidance only if it is a *change* (FR-122).

        The stream is append-only, which is not the same as append-always: a
        transition is recorded when control actually moves between actors or
        actions, and re-recording the same phase on every request would bury
        the handoffs a reader is looking for under repetitions of the state
        they were already in.

        Returns the new guidance-event id, or `None` when nothing moved.
        """
        latest = await self.latest()
        if latest is not None and latest["phase"] == str(guidance.phase.value):
            return None
        return await self.append(guidance, run_id=run_id)

    async def latest(self) -> Mapping[str, Any] | None:
        """The workspace's current guidance row, or `None` before the first.

        Read by `GET /workspace`, which reports guidance rather than re-deriving
        it — the banner must show what was recorded, not a second opinion
        computed at read time.
        """
        row = await self._work.fetch_one(
            "SELECT * FROM guidance_events WHERE workspace_id = ? "
            "ORDER BY workspace_version DESC LIMIT 1",
            (self._workspace_id,),
        )
        return None if row is None else dict(row)

    async def _next_version(self) -> int:
        row = await self._work.fetch_one(
            "SELECT COALESCE(MAX(workspace_version), 0) AS highest "
            "FROM guidance_events WHERE workspace_id = ?",
            (self._workspace_id,),
        )
        return (int(row["highest"]) if row else 0) + 1


def _code(value: object | None) -> str | None:
    return None if value is None else str(getattr(value, "value", value))
