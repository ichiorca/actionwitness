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

from actionwitness_core.journeys.enums import EventActor, OutcomeEventType
from actionwitness_core.journeys.guidance import COPY_VERSION, GuidanceState

from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import EventRepository, new_id

__all__ = ["GuidanceRecorder"]


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
