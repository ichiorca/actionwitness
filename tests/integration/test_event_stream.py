"""012-T7 — the SSE timeline transport (§15.3).

§15.3: "A Tier 3 SSE implementation shall use the event sequence as the SSE
`id`, honor `Last-Event-ID`, and retain the paged endpoint as fallback." Those
three are the contract, and each has a test below.

**The one that matters most is not in that sentence.** Constitution §17: "No
database transaction or workspace mutation lock may remain open across browser
I/O, SSE delivery, or a human-confirmation wait." An SSE response lasts as long
as a client cares to listen, and a generator that read inside a transaction and
yielded from inside it would hold a SQLite read open for that entire time —
which, under ADR-0003's WAL settings, is how a writer ends up blocked by
somebody's idle browser tab.

That last sentence is wrong, and the test that assumed it was vacuous. The first
version of the rail test asserted a writer keeps working while a stream is open,
and it **passed against an implementation deliberately broken to hold its unit
of work across every yield** — `reading()` opens a fresh connection and takes no
write lock under WAL, so there was never a writer to starve. The mutation is
what proved it.

The hazard that is real here is *connections*: one per open stream, held for as
long as a tab stays open, with nothing bounding how many tabs there are (§21).
`test_no_unit_of_work_is_held_across_a_yield` measures that directly, by
counting open units of work at the point the generator suspends, and it does
fail against the same mutation.

Time is injected throughout. A required suite that waited on a real clock would
be slow and, worse, flaky on a loaded machine (constitution §6).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.api.errors import ApiErrorCode
from actionwitness_service.api.event_stream import (
    MAX_IDLE_SECONDS,
    POLL_INTERVAL_SECONDS,
    resume_cursor,
    stream_events,
    wants_event_stream,
)
from actionwitness_service.application.limits import CONCURRENT_EVENT_STREAMS
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
STREAM = {"Accept": "text/event-stream"}
EVENT_STREAM_ACCEPT = b"text/event-stream"


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    store = create_store(database_path=tmp_path / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ={
                "HARNESS_ENV": "local",
                "BUGGY_STORE_ENABLED": "true",
                "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            },
            database_path=tmp_path / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            yield harness


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


async def _armed_run(visitor: httpx.AsyncClient, *, request_id: str = "req_sse_mug") -> str:
    """A run with a few real events on its timeline.

    `request_id` is a parameter so two workspaces can each arm one in the same
    test without sharing an idempotency key — a key identifies one intent, and
    reusing it across two of them is the thing the harness exists to catch.
    """
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})

    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    await visitor.post(
        f"{RUNS}/{run_id}/target-tools/search_catalog:invoke", json={"arguments": {"query": "mug"}}
    )
    await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": request_id}},
    )
    return run_id


def _frames(body: str) -> list[dict[str, str]]:
    """Parse an SSE body into its frames, keeping comments out."""
    frames: list[dict[str, str]] = []
    for block in body.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        frame: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":") or ":" not in line:
                continue
            field, _, value = line.partition(":")
            frame[field.strip()] = value.strip()
        if frame:
            frames.append(frame)
    return frames


async def _collect(stack: FastAPI, workspace_id: str, run_id: str, *, after: int = 0) -> str:
    """Drive the generator with an injected clock, to a terminal end frame."""
    ticks = {"count": 0}

    async def sleep(_seconds: float) -> None:
        # Ends the stream deterministically instead of waiting on a real clock:
        # after enough simulated idle time the generator emits `end` on its own.
        ticks["count"] += 1

    chunks: list[str] = []
    async for chunk in stream_events(
        stack.state.database, workspace_id, run_id, after_sequence=after, sleep=sleep
    ):
        chunks.append(chunk)
        if ticks["count"] > MAX_IDLE_SECONDS / POLL_INTERVAL_SECONDS + 2:
            break
    return "".join(chunks)


# --- §15.3's three requirements ----------------------------------------------


async def test_the_paged_endpoint_is_untouched_by_default(visitor: httpx.AsyncClient) -> None:
    """ "Retain the paged endpoint as fallback" — and as the default.

    A client that did not ask for a stream must not be given one. `fetch` sends
    `Accept: */*`, and a caller that cannot parse a stream would hang waiting
    for a body that never ends.
    """
    # Arrange
    run_id = await _armed_run(visitor)

    # Act
    paged = await visitor.get(f"{RUNS}/{run_id}/events?limit=50")

    # Assert
    assert paged.status_code == 200, paged.text
    assert paged.headers["content-type"].startswith("application/json")
    assert paged.json()["events"]


async def test_negotiating_the_stream_returns_an_event_stream(
    visitor: httpx.AsyncClient,
) -> None:
    """§15.3's `Accept: text/event-stream` switch, on the same endpoint."""
    # Arrange
    run_id = await _armed_run(visitor)
    await visitor.post(f"{RUNS}/{run_id}/verify")

    # Act
    async with visitor.stream("GET", f"{RUNS}/{run_id}/events", headers=STREAM) as streamed:
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        body = "".join([chunk async for chunk in streamed.aiter_text()])

    # Assert — a terminal run streams its whole timeline and then ends, rather
    # than holding the connection open forever.
    frames = _frames(body)
    assert any(frame.get("event") == "end" for frame in frames)


async def test_each_event_carries_its_sequence_as_the_sse_id(
    visitor: httpx.AsyncClient,
) -> None:
    """§15.3: "use the event sequence as the SSE `id`".

    This is what makes resume work at all: the browser echoes the last `id:` it
    saw, and a dense monotonic sequence turns that into a cursor the server does
    not have to remember.
    """
    # Arrange
    run_id = await _armed_run(visitor)
    await visitor.post(f"{RUNS}/{run_id}/verify")

    # Act
    async with visitor.stream("GET", f"{RUNS}/{run_id}/events", headers=STREAM) as streamed:
        body = "".join([chunk async for chunk in streamed.aiter_text()])

    # Assert
    identifiers = [int(frame["id"]) for frame in _frames(body) if "id" in frame]
    assert identifiers, body
    assert identifiers == sorted(identifiers)
    assert len(set(identifiers)) == len(identifiers)


async def test_last_event_id_resumes_rather_than_replaying(
    visitor: httpx.AsyncClient,
) -> None:
    """§15.3: "honor `Last-Event-ID`".

    A reconnect that replayed the timeline would make a browser's automatic
    retry duplicate every event it had already shown — and a client cannot tell
    a replay from genuinely repeated activity.
    """
    # Arrange
    run_id = await _armed_run(visitor)
    await visitor.post(f"{RUNS}/{run_id}/verify")
    async with visitor.stream("GET", f"{RUNS}/{run_id}/events", headers=STREAM) as first:
        whole = "".join([chunk async for chunk in first.aiter_text()])
    identifiers = [int(frame["id"]) for frame in _frames(whole) if "id" in frame]
    resume_from = identifiers[len(identifiers) // 2]

    # Act
    async with visitor.stream(
        "GET",
        f"{RUNS}/{run_id}/events",
        headers={**STREAM, "Last-Event-ID": str(resume_from)},
    ) as resumed:
        tail = "".join([chunk async for chunk in resumed.aiter_text()])

    # Assert
    seen = [int(frame["id"]) for frame in _frames(tail) if "id" in frame]
    assert seen == [i for i in identifiers if i > resume_from]


# --- the constitutional rail --------------------------------------------------


class _WatchedDatabase:
    """A database wrapper that counts how many units of work are open now.

    Wrapping rather than instrumenting the real class: `stream_events` takes its
    database as a parameter precisely so the thing under test can be observed
    without the production path growing a counter it does not need.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.open = 0
        self.peak = 0

    @asynccontextmanager
    async def reading(self) -> AsyncIterator[Any]:
        self.open += 1
        self.peak = max(self.peak, self.open)
        try:
            async with self._inner.reading() as work:
                yield work
        finally:
            self.open -= 1


async def test_no_unit_of_work_is_held_across_a_yield(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """Constitution §17, measured where the stream actually suspends.

    The generator is driven to its idle point — parked at a yield, waiting for
    events that have not happened — and the number of open units of work is read
    there. A stream that opened one and yielded from inside it would sit at one,
    holding a connection for as long as the tab stayed open, with nothing
    bounding how many tabs there are (§21).

    **This replaced a test that asserted writers get blocked.** That one passed
    against an implementation deliberately broken to hold its unit of work
    across every yield: `reading()` opens a fresh connection and takes no write
    lock under WAL, so there was no writer to starve and nothing to detect. This
    assertion fails against that same mutation.
    """
    # Arrange — a live (non-terminal) run, so the stream stays open.
    run_id = await _armed_run(visitor)
    workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
    watched = _WatchedDatabase(stack.state.database)
    stream = stream_events(watched, workspace_id, run_id, after_sequence=0)

    # Act — pull until the generator is parked waiting for more events.
    first = await anext(stream)
    assert first.startswith("retry:")
    async for chunk in stream:
        if chunk.startswith(":"):
            break

    # Assert — nothing held while suspended, and the reads did happen.
    assert watched.open == 0
    assert watched.peak >= 1, "no unit of work was opened, so the check is vacuous"
    await stream.aclose()
    assert watched.open == 0


async def test_a_stream_picks_up_events_written_after_it_opened(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """The point of a stream, and the counterpart to the test above.

    A stream that never re-read would be a slower page fetch. This one has to
    deliver an event that did not exist when the connection opened.
    """
    # Arrange
    run_id = await _armed_run(visitor)
    workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
    stream = stream_events(stack.state.database, workspace_id, run_id, after_sequence=0)
    seen: list[str] = []
    await anext(stream)
    async for chunk in stream:
        seen.append(chunk)
        if chunk.startswith(":"):
            break

    # Act
    await visitor.post(
        f"{RUNS}/{run_id}/target-tools/apply_discount:invoke",
        json={"arguments": {"code": "SAVE20"}},
    )
    async for chunk in stream:
        seen.append(chunk)
        if "apply_discount" in chunk:
            break

    # Assert
    await stream.aclose()
    assert any("apply_discount" in chunk for chunk in seen)


async def test_an_idle_stream_ends_instead_of_living_forever(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """§21: a stream nobody feeds is an unbounded resource.

    Ending is safe precisely because resume works — the client reconnects with
    `Last-Event-ID` and continues on the same code path a network blip takes.
    """
    # Arrange
    run_id = await _armed_run(visitor)
    workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

    # Act
    body = await _collect(stack, workspace_id, run_id)

    # Assert
    assert any(frame.get("event") == "end" for frame in _frames(body))


# --- authorization and negotiation -------------------------------------------


async def test_a_run_in_another_workspace_is_refused_before_the_stream_opens(
    stack: FastAPI,
) -> None:
    """FR-006, and a fact about SSE: once `200 text/event-stream` is on the
    wire, §15.8's envelope can no longer be sent.

    So the run is resolved first, and a caller who may not see it gets a plain
    404 rather than an empty stream they have to interpret as a refusal.
    """
    # Arrange
    transport = httpx.ASGITransport(app=stack, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://harness.test") as owner,
        httpx.AsyncClient(transport=transport, base_url="https://harness.test") as stranger,
    ):
        run_id = await _armed_run(owner)

        # Act
        refused = await stranger.get(f"{RUNS}/{run_id}/events", headers=STREAM)

    # Assert
    assert refused.status_code == 404, refused.text
    assert refused.headers["content-type"].startswith("application/json")


# --- the negotiation and resume helpers ---------------------------------------


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        ("text/event-stream", True),
        ("text/event-stream; charset=utf-8", True),
        ("application/json, text/event-stream", True),
        ("*/*", False),
        ("application/json", False),
        (None, False),
    ],
)
def test_only_an_explicit_accept_selects_the_stream(accept: str | None, expected: bool) -> None:
    """`*/*` is the case worth naming: it is what `fetch` sends by default."""
    assert wants_event_stream(accept) is expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [("7", 7), ("  7 ", 7), ("0", 0), (None, 3), ("nonsense", 3), ("-1", 3), ("", 3)],
)
def test_a_malformed_resume_header_falls_back_rather_than_failing(
    header: str | None, expected: int
) -> None:
    """The header is echoed by the browser but arrives as untrusted input.

    A malformed value replays from the query cursor rather than refusing: a bad
    resume header should cost a client duplicate events, never the stream.
    """
    assert resume_cursor(header, 3) == expected


# --- FR-008's two concurrent connections --------------------------------------
#
# "One interactive workspace may retain at most ... two concurrent event-stream
# connections." Each open stream is a held HTTP connection plus a poll of SQLite
# every second, against a single-worker deployment (§29.1), so the failure this
# cap prevents is a workspace with thirty tabs — or one hostile client with
# three hundred — rather than anything a single user does by accident.

#: Every wait below is bounded, and the refusals are probed through the same
#: driver rather than through httpx. That is not a preference: a request that
#: negotiates a stream against a *live* run and is wrongly admitted never
#: returns, so an httpx probe would turn a broken ceiling into a suite that
#: hangs for five minutes of real clock instead of a test that fails. This was
#: found by removing the ceiling and watching the first version hang.
_CONNECT_TIMEOUT = 5.0


class _HeldConnection:
    """One SSE connection, held open against the real ASGI application.

    httpx's `ASGITransport` awaits the entire application call and buffers the
    body before returning a response, so a stream that stays open cannot be held
    through it: the request would not return until the run reached a verdict or
    the stream idled out five minutes later. Two connections open *at the same
    time* is precisely the state this ceiling is about, so the app is driven
    directly here.

    It is also the only way to produce the ending that matters. A tab that goes
    away does not close a generator politely; it stops answering, and the server
    learns about it as `http.disconnect`. `disconnect()` below is that, and the
    slot has to come back from it.
    """

    def __init__(self, app: FastAPI, *, cookie: str, path: str) -> None:
        self._app = app
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._request_sent = False
        self._started = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._body: list[bytes] = []
        self._keepalive = asyncio.Event()
        self._scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "scheme": "https",
            "server": ("harness.test", 443),
            "client": ("127.0.0.1", 123),
            "headers": [
                (b"host", b"harness.test"),
                (b"accept", EVENT_STREAM_ACCEPT),
                (b"cookie", cookie.encode()),
            ],
        }

    async def _receive(self) -> dict[str, Any]:
        if not self._request_sent:
            self._request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return await self._incoming.get()

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = int(message["status"])
            self.headers = {
                name.decode().lower(): value.decode() for name, value in message.get("headers", ())
            }
            self._started.set()
        elif message["type"] == "http.response.body":
            body = message.get("body", b"")
            self._body.append(body)
            if body.startswith(b":"):
                # A keepalive comment. The statement after it in `stream_events`
                # is the poll interval's sleep, so seeing one places the
                # generator between two database reads rather than inside one.
                self._keepalive.set()

    async def open(self) -> int:
        """Send the request; return the status once the response has begun.

        The slot is taken inside the route, before the response starts, so a
        status is proof the connection is holding one.
        """
        task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        self._task = task
        # Releases the wait when the application finishes without ever starting
        # a response, so a failure is a failed assertion rather than a timeout.
        task.add_done_callback(lambda _finished: self._started.set())
        await asyncio.wait_for(self._started.wait(), timeout=_CONNECT_TIMEOUT)
        if task.done():
            task.result()
        assert self.status is not None
        return self.status

    async def envelope(self) -> dict[str, Any]:
        """The refusal body, once the application has finished answering.

        Bounded like everything else here: a request that was admitted when it
        should have been refused would never finish, and waiting on it forever
        is how a broken ceiling turns into a silent suite.
        """
        assert self._task is not None
        await asyncio.wait_for(self._task, timeout=_CONNECT_TIMEOUT)
        decoded = json.loads(b"".join(self._body))
        assert isinstance(decoded, dict)
        return decoded

    async def _wait_for_keepalive(self) -> None:
        """Park the stream between polls before interrupting it.

        Not politeness, and not a sleep: `Database.connect()` closes its
        connection in a `finally`, and a cancellation delivered *inside* that
        close is delivered again at the `await` that closes it — so a stream
        interrupted mid-read leaves an aiosqlite connection and its worker
        thread alive until the loop dies, which surfaces later as an unhandled
        `Event loop is closed` in whichever test happens to be running. That is
        a real defect in the cancellation path (see the report accompanying this
        change), and it lives outside this file; waiting for a keepalive keeps
        these tests from being the place it shows up at random.
        """
        self._keepalive.clear()
        await asyncio.wait_for(self._keepalive.wait(), timeout=_CONNECT_TIMEOUT)

    async def disconnect(self) -> None:
        """What a closed tab looks like from the server's side. Idempotent."""
        task = self._task
        if task is None or task.done():
            return
        await self._wait_for_keepalive()
        await self._incoming.put({"type": "http.disconnect"})
        await asyncio.wait_for(task, timeout=_CONNECT_TIMEOUT)


def _cookie_header(client: httpx.AsyncClient) -> str:
    return "; ".join(f"{name}={value}" for name, value in client.cookies.items())


@asynccontextmanager
async def _held_stream(
    stack: FastAPI, visitor: httpx.AsyncClient, run_id: str
) -> AsyncIterator[_HeldConnection]:
    """One open stream for `visitor`'s workspace, closed however the test ends."""
    connection = _HeldConnection(
        stack, cookie=_cookie_header(visitor), path=f"{RUNS}/{run_id}/events"
    )
    try:
        yield connection
    finally:
        await connection.disconnect()


async def test_the_third_concurrent_stream_is_refused_before_it_opens(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """FR-008's ceiling, and §15.8's envelope rather than a broken stream.

    The refusal has to arrive as ordinary JSON. Once `200 text/event-stream` is
    on the wire a client is reading a stream, and a connection that opened and
    then closed silently is indistinguishable from a network fault — so it would
    be retried, forever, by the browser's own reconnect.
    """
    # Arrange — a live run, so its streams stay open rather than ending.
    run_id = await _armed_run(visitor)

    # Act
    async with (
        _held_stream(stack, visitor, run_id) as first,
        _held_stream(stack, visitor, run_id) as second,
        _held_stream(stack, visitor, run_id) as third,
    ):
        assert await first.open() == 200
        assert await second.open() == 200
        status = await third.open()
        assert status == 409
        # Assert — §15.8's envelope, not a stream that closes without saying why.
        assert third.headers["content-type"].startswith("application/json")
        error = (await third.envelope())["error"]

    assert error["code"] == ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED.value
    assert error["retryable"] is False
    assert error["details"] == []
    assert error["message"]


async def test_a_disconnected_stream_frees_exactly_one_slot(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """The leak test, taken through the ending nobody codes for.

    A slot released only when a generator finishes tidily would pass every test
    that closed its streams politely and leak on the first dropped tab — and a
    leaked slot lowers this workspace's ceiling for the life of the process,
    which is worse than no ceiling at all.

    One slot, not all of them: the connection still open must still be counted.
    """
    # Arrange — at the ceiling, then one connection goes away.
    run_id = await _armed_run(visitor)
    async with (
        _held_stream(stack, visitor, run_id) as kept,
        _held_stream(stack, visitor, run_id) as dropped,
        _held_stream(stack, visitor, run_id) as replacement,
        _held_stream(stack, visitor, run_id) as over,
    ):
        assert await kept.open() == 200
        assert await dropped.open() == 200

        # Act
        await dropped.disconnect()

        # Assert — exactly one slot came back: the replacement is admitted and
        # the one after it is not, so `kept` is still being counted.
        assert await replacement.open() == 200
        assert await over.open() == 409
        error = (await over.envelope())["error"]

    assert error["code"] == ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED.value


async def test_one_workspaces_streams_do_not_spend_anothers_budget(stack: FastAPI) -> None:
    """The workspace is the isolation boundary, and so is its connection budget.

    A counter that was global rather than keyed would pass the two tests above
    and let any visitor close the streams of every other one.
    """
    # Arrange — two workspaces, each with a live run of its own.
    transport = httpx.ASGITransport(app=stack, raise_app_exceptions=False)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://harness.test") as owner,
        httpx.AsyncClient(transport=transport, base_url="https://harness.test") as neighbour,
    ):
        owned_run = await _armed_run(owner)
        neighbour_run = await _armed_run(neighbour, request_id="req_sse_mug_neighbour")

        # Act — the first workspace takes its whole allowance.
        async with (
            _held_stream(stack, owner, owned_run) as first,
            _held_stream(stack, owner, owned_run) as second,
            _held_stream(stack, neighbour, neighbour_run) as across,
            _held_stream(stack, owner, owned_run) as owner_over,
        ):
            assert await first.open() == 200
            assert await second.open() == 200

            # Assert — the owner is at its ceiling, and the neighbour is not.
            assert await owner_over.open() == 409
            assert await across.open() == 200


async def test_the_paged_fallback_is_never_capped(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """§15.3 keeps paging as the contract and SSE as the enhancement.

    A paged read is an ordinary request that ends when it is answered, so it
    holds nothing to cap. Capping it would take the enhancement's failure mode
    and hand it to the fallback that exists for clients which cannot stream.
    """
    # Arrange — the workspace is at its stream ceiling.
    run_id = await _armed_run(visitor)
    async with (
        _held_stream(stack, visitor, run_id) as first,
        _held_stream(stack, visitor, run_id) as second,
    ):
        assert await first.open() == 200
        assert await second.open() == 200

        # Act — repeatedly, because a cap would show up on the first refusal.
        pages = [await visitor.get(f"{RUNS}/{run_id}/events?limit=50") for _ in range(3)]

    # Assert
    assert [page.status_code for page in pages] == [200, 200, 200], pages[-1].text
    assert all(page.json()["events"] for page in pages)


async def test_the_ceiling_is_the_number_fr_008_states(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """Two, not "some". The count is read from the application's own counter, so
    the assertion is about what the ceiling admitted rather than about the
    constant being equal to itself."""
    # Arrange
    run_id = await _armed_run(visitor)

    # Act
    async with (
        _held_stream(stack, visitor, run_id) as first,
        _held_stream(stack, visitor, run_id) as second,
    ):
        await first.open()
        await second.open()
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        held = stack.state.event_streams.open_streams(workspace_id)

    # Assert — and the counter empties itself rather than keeping the workspace.
    assert held == CONCURRENT_EVENT_STREAMS
    assert stack.state.event_streams.open_streams(workspace_id) == 0
    assert len(stack.state.event_streams) == 0
