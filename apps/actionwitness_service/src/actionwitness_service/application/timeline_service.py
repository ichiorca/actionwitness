"""Paged reads of a run's timeline (§15.3, FR-034).

§15.3 makes polling pagination normative for Tier 1: `after_sequence` and a
`limit` between 1 and 100. Sequence numbers are dense and monotonic per run
(ADR-0003), so "everything after N" is a cursor a client can hold across
reconnects without the server remembering anything about it.

**`has_more` is not "the run is finished".** It reports only that events already
exist beyond this page. A live run appends more afterwards, so a client that
stopped polling on `has_more: false` would silently miss the rest of the
timeline. `run_status` is what ends a poll, and it travels with every page for
exactly that reason.

**The projection is explicit.** The repository returns whole rows, and returning
them would make every column added later public the day it is added — including
one nobody meant to export. §20.3 puts redaction before export; naming the
fields here is what keeps that decision reviewable in one place.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import EventRepository

__all__ = ["EVENT_PAGE_DEFAULT", "EVENT_PAGE_MAX", "EventPage", "TimelineService"]

#: §15.3's `limit={1..100}`. The bound is the specification's, not a guess.
EVENT_PAGE_MAX: Final = 100
EVENT_PAGE_DEFAULT: Final = 50

#: What a client sees of one event. Every name here is either server-issued, a
#: closed-enum value, or a payload the writer already redacted (§20.3).
_EVENT_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "sequence_number",
    "event_type",
    "actor",
    "annotated_sequence_number",
    "tool_identity_hash",
    "tool_name",
    "correlation_id",
    "request_id",
    "status",
    "reported_status",
    "state_version_before",
    "state_version_after",
    "state_hash_before",
    "state_hash_after",
    "duration_ms",
    "created_at",
    "redacted_payload",
)


@dataclass(frozen=True, slots=True)
class EventPage:
    """One page of a run's timeline, and where to ask for the next."""

    run_id: str
    run_status: str
    events: tuple[Mapping[str, Any], ...]
    next_after_sequence: int
    has_more: bool

    def as_document(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_status": self.run_status,
            "events": [dict(event) for event in self.events],
            # The cursor for the next call. Echoing the caller's own
            # `after_sequence` back when a page is empty means a polling client
            # never has to special-case "nothing happened yet".
            "next_after_sequence": self.next_after_sequence,
            "has_more": self.has_more,
        }


class TimelineService:
    """Reads one run's events, bounded to the workspace that owns the run."""

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    async def events(self, run_id: str, *, after_sequence: int, limit: int) -> EventPage:
        """§15.3's paged read.

        The run is resolved through `WorkspaceScope` first, so a run identifier
        belonging to another workspace is a 404 before any event is read —
        events carry no `workspace_id` of their own, and reaching them through
        their run is what gives "who owns this event?" one answer (FR-006).
        """
        if not 1 <= limit <= EVENT_PAGE_MAX:
            raise ValueError(f"limit must be between 1 and {EVENT_PAGE_MAX}")
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")

        run = await WorkspaceScope(self._work, self._workspace_id).run(run_id)

        # One row beyond the page, to learn whether another page exists without
        # counting the whole timeline. The extra row is discarded, never
        # returned: a page that quietly held `limit + 1` events would break the
        # cursor arithmetic the client is doing.
        probed = await EventRepository(self._work).list_after(run_id, after_sequence, limit + 1)
        has_more = len(probed) > limit
        rows = probed[:limit]

        return EventPage(
            run_id=str(run["id"]),
            run_status=str(run["status"]),
            events=tuple(_projected(row) for row in rows),
            next_after_sequence=(
                int(rows[-1]["sequence_number"]) if rows else after_sequence  # type: ignore[arg-type]
            ),
            has_more=has_more,
        )


def _projected(row: Mapping[str, object]) -> dict[str, Any]:
    """One stored event narrowed to the fields §15.3 publishes."""
    return {field: row.get(field) for field in _EVENT_FIELDS}
