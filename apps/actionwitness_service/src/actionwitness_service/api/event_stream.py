"""The Tier 3 SSE timeline transport (§15.3; 012-T7).

§15.3: "A Tier 3 SSE implementation shall use the event sequence as the SSE
`id`, honor `Last-Event-ID`, and retain the paged endpoint as fallback." All
three are here, and the third is the reason this module is small: SSE is an
*enhancement*, so the paged endpoint keeps working unchanged and a client that
never negotiates a stream cannot tell this shipped.

## The rail that shapes the code

Constitution §17: "No database transaction or workspace mutation lock may remain
open across browser I/O, SSE delivery, or a human-confirmation wait." An SSE
response is, by construction, I/O that lasts as long as the client cares to
listen — minutes, or until a laptop lid closes.

Be precise about the hazard, because the obvious version of it is wrong here.
`Database.reading()` opens a **fresh connection** and issues no `BEGIN`; under
ADR-0003's WAL settings a reader does not block a writer, so a stream that held
one open would not starve mutations. What it would exhaust is *connections* —
one per open stream, held for as long as a tab stays open, with nothing bounding
how many tabs there are (§21). The rule is the same and the reason is different.

So every read is materialized and its unit of work closed *before* anything is
yielded. `_page` exists as a separate function for exactly that reason: it is
not an async generator, so it cannot accidentally suspend while holding one.

## Resume, and why the cursor is the sequence number

Event sequence numbers are dense and monotonic per run (ADR-0003), which is what
lets "everything after N" be a cursor the client holds rather than server state
somebody has to expire. `Last-Event-ID` is therefore honoured directly: the
browser sends back the last `id:` it saw, and the stream continues from there.

A resumed stream re-reads from the database rather than from anything buffered,
so a reconnect after an hour is the same operation as one after a second.

## Ending, rather than reconnecting forever

`EventSource` reconnects on its own whenever the server closes the connection.
That is right for a network blip and wrong for a run that finished: without a
distinguishable ending, a completed run would be re-streamed every few seconds
for as long as the tab stayed open. The stream therefore emits a named `end`
event carrying the terminal status, and the client closes on it deliberately.

An idle stream also ends on its own after `MAX_IDLE_SECONDS`, which costs
nothing precisely because resume works: the client reconnects with
`Last-Event-ID` and continues. A stream that lived forever would be an unbounded
resource held by whoever opened the most tabs (§21).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Final

from actionwitness_service.application.timeline_service import (
    EVENT_PAGE_MAX,
    EventPage,
    TimelineService,
)
from actionwitness_service.persistence.database import Database

__all__ = [
    "EVENT_STREAM_MEDIA_TYPE",
    "MAX_IDLE_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "RECONNECT_DELAY_MS",
    "resume_cursor",
    "stream_events",
    "wants_event_stream",
]

EVENT_STREAM_MEDIA_TYPE: Final = "text/event-stream"

#: How often the stream looks for new events while the run is live.
#:
#: This is a poll against SQLite, not a push: the database has no change feed,
#: and inventing one — a shared in-process broadcaster — would be a second
#: source of truth about what happened in a run. A one-second read of an
#: indexed range is cheap, and the client still gets sub-second delivery
#: relative to the polling transport's page interval.
POLL_INTERVAL_SECONDS: Final = 1.0

#: How long a stream with nothing to say stays open before ending cleanly.
#:
#: Ending is safe *because* resume works: the client reconnects with
#: `Last-Event-ID` and continues where it stopped. An immortal stream would be
#: an unbounded resource held by whoever opened the most tabs.
MAX_IDLE_SECONDS: Final = 300.0

#: The `retry:` hint, in milliseconds — how soon a browser should reconnect.
RECONNECT_DELAY_MS: Final = 3_000

#: Run states that end a stream. Terminal means the timeline is complete, so
#: there is nothing further to wait for.
_TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {"passed", "passed_with_warnings", "failed", "error", "cancelled"}
)


def wants_event_stream(accept: str | None) -> bool:
    """Whether this request negotiated SSE (§15.3's `Accept` switch).

    Deliberately narrow: the media type must be named. A browser sending
    `Accept: */*` — which `fetch` does by default — gets JSON, because a client
    that did not ask for a stream and cannot parse one would otherwise hang
    waiting for a body that never ends.
    """
    if accept is None:
        return False
    return any(part.strip().split(";")[0] == EVENT_STREAM_MEDIA_TYPE for part in accept.split(","))


def resume_cursor(last_event_id: str | None, after_sequence: int) -> int:
    """Where to resume: `Last-Event-ID` if usable, else the query cursor.

    The header comes from the browser and is therefore untrusted, even though
    the browser is only echoing an `id:` this server wrote. Anything that is not
    a non-negative integer falls back to `after_sequence` rather than raising: a
    malformed resume header should replay the timeline, not refuse to stream it.
    """
    if last_event_id is None:
        return after_sequence
    try:
        parsed = int(last_event_id.strip())
    except ValueError:
        return after_sequence
    return parsed if parsed >= 0 else after_sequence


def _frame(*, event: str | None = None, data: Mapping[str, Any], identifier: int | None) -> str:
    """One SSE frame.

    `data` is serialized compactly and on one line. A newline inside a `data:`
    field would start a second field, so a payload containing one would be
    silently truncated at the client — `json.dumps` without indentation cannot
    emit a bare newline, which is what makes this safe rather than lucky.
    """
    lines: list[str] = []
    if identifier is not None:
        lines.append(f"id: {identifier}")
    if event is not None:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


async def _page(
    database: Database, workspace_id: str, run_id: str, *, after_sequence: int
) -> EventPage:
    """One page, with its transaction closed before the caller can yield.

    A plain coroutine rather than part of the generator below, which is the
    whole point: a coroutine cannot suspend at a `yield` while holding the
    transaction open, so the constitution's "no transaction across SSE
    delivery" is a property of the shape here rather than of remembering.
    """
    async with database.reading() as work:
        return await TimelineService(work, workspace_id).events(
            run_id, after_sequence=after_sequence, limit=EVENT_PAGE_MAX
        )


async def stream_events(
    database: Database,
    workspace_id: str,
    run_id: str,
    *,
    after_sequence: int,
    sleep: Any = asyncio.sleep,
) -> AsyncIterator[str]:
    """Stream a run's timeline until it ends or the stream goes idle.

    `sleep` is injected so a test can drive the loop without wall-clock waits;
    required suites must not depend on real time (constitution §6).

    The first read happens before anything is yielded, so a run the caller may
    not see fails with §15.8's envelope from the route rather than as a broken
    stream a client has to interpret.
    """
    yield f"retry: {RECONNECT_DELAY_MS}\n\n"

    cursor = after_sequence
    idle = 0.0
    while True:
        page = await _page(database, workspace_id, run_id, after_sequence=cursor)

        for event in page.events:
            sequence = event.get("sequence_number")
            yield _frame(
                data=dict(event),
                identifier=int(sequence) if isinstance(sequence, int) else None,
            )
        if page.events:
            cursor = page.next_after_sequence
            idle = 0.0

        # Drain first, then stop. A terminal run whose last page was full still
        # has events beyond it, and ending here would truncate the timeline at
        # exactly the moment it became complete.
        if page.run_status in _TERMINAL_STATUSES and not page.has_more:
            yield _frame(event="end", data={"run_status": page.run_status}, identifier=None)
            return

        if page.has_more:
            # More is already stored; read it immediately rather than waiting a
            # poll interval per page while catching up a long timeline.
            continue

        if idle >= MAX_IDLE_SECONDS:
            # Not an error. The client reconnects with `Last-Event-ID` and
            # continues, which is the same code path a network blip takes.
            yield _frame(event="end", data={"run_status": page.run_status}, identifier=None)
            return

        # A comment frame: ignored by every client, and enough to keep a proxy
        # from closing an idle connection it thinks is dead.
        yield ": keepalive\n\n"
        await sleep(POLL_INTERVAL_SECONDS)
        idle += POLL_INTERVAL_SECONDS
