"""FR-008's hard resource ceilings, transcribed exactly.

FR-008 is a list of specific numbers, not a set of tunable defaults, so they are
written here once as named constants and a test compares each against the
sentence it came from. "250 persisted events, with one slot reserved for the
terminal `resource_limit_exceeded` boundary event carrying code
`EVENT_LIMIT_EXCEEDED`" is a **249 + 1** budget, not a 250 budget that happens
to overflow — a run that spent all 250 on ordinary events would have nowhere to
record *why* it stopped, which is the one event that makes the stop legible.

Two properties matter more than the arithmetic.

**A refused creation commits nothing.** FR-009: limits "shall never partially
commit a mutation." The guard therefore runs *inside* the caller's unit of work,
counting rows the same transaction can see, and raises — which rolls the whole
unit back. A decorator that checked before the handler ran would have to guess
at concurrent creations, and one that checked after would have already written.

**Tripping the event ceiling is itself atomic.** FR-008 requires the server to
"atomically move the active run to `error`, append that boundary event, reject
further target actions, and preserve existing evidence." All three writes happen
in the caller's single transaction, so a crash between them is not a state where
the run is stopped but nobody recorded the stop.

One of FR-008's ceilings is not a row count at all: "two concurrent event-stream
connections" counts *connections held right now*, which no table records.
`EventStreamSlots` at the bottom of this file is that one, and it is deliberately
here rather than beside the SSE transport — FR-008's numbers stay in one file, and
the sentence that declares this cap is the same sentence that declares the others.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.authorization import not_found
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import EventRepository

__all__ = [
    "ARTIFACTS_PER_WORKSPACE",
    "ARTIFACT_BYTES_PER_WORKSPACE",
    "CONCURRENT_EVENT_STREAMS",
    "EVAL_CASES_PER_WORKSPACE",
    "EVAL_RUNS_PER_WORKSPACE",
    "EVENTS_PER_RUN",
    "ORDINARY_EVENTS_PER_RUN",
    "OUTCOME_RUNS_PER_WORKSPACE",
    "SHOPIFY_PAIRINGS_PER_WORKSPACE",
    "SUITES_PER_WORKSPACE",
    "TRIALS_PER_SUITE",
    "EventStreamSlots",
    "WorkspaceCeilings",
]

# --- FR-008, one constant per number in the sentence ------------------------

#: "at most 250 persisted events"
EVENTS_PER_RUN: Final = 250
#: "with one slot reserved for the terminal `resource_limit_exceeded` boundary
#: event". The reserved slot is what makes the ceiling explainable rather than
#: merely reached.
ORDINARY_EVENTS_PER_RUN: Final = EVENTS_PER_RUN - 1

OUTCOME_RUNS_PER_WORKSPACE: Final = 10
EVAL_CASES_PER_WORKSPACE: Final = 10
EVAL_RUNS_PER_WORKSPACE: Final = 20
SUITES_PER_WORKSPACE: Final = 3
TRIALS_PER_SUITE: Final = 100
SHOPIFY_PAIRINGS_PER_WORKSPACE: Final = 5
ARTIFACTS_PER_WORKSPACE: Final = 25
#: "10 MiB of persisted artifact bytes" — mebibytes, not megabytes.
ARTIFACT_BYTES_PER_WORKSPACE: Final = 10 * 1024 * 1024
CONCURRENT_EVENT_STREAMS: Final = 2

#: FR-008's per-workspace row ceilings, keyed by the table they count.
#:
#: A ceiling appears here when the table it counts exists and something writes
#: to it — counting a table no migration has created is an error, not a ceiling.
#: The remaining Tier 2 constants are declared above so the numbers land in one
#: place, and `test_every_fr_008_ceiling_is_declared` fails if one is dropped.
_ROW_CEILINGS: Final[dict[str, tuple[int, str]]] = {
    "runs": (OUTCOME_RUNS_PER_WORKSPACE, "outcome runs"),
    "artifacts": (ARTIFACTS_PER_WORKSPACE, "artifacts"),
    "shopify_pairings": (SHOPIFY_PAIRINGS_PER_WORKSPACE, "Shopify pairings"),
}

#: FR-008's remedy, quoted in every refusal: "an action to purge completed
#: workspace data". A limit message that named no way out would leave a user
#: stuck at a wall the product itself built.
_REMEDY: Final = "Purge completed workspace data and retry."


class WorkspaceCeilings:
    """FR-008's ceilings, enforced inside the caller's transaction.

    Constructed from a `UnitOfWork` rather than a `Database` for exactly one
    reason: the count and the insert it guards must be the same transaction.
    A guard that opened its own connection would count rows a concurrent
    creation had not yet committed, and admit the eleventh run.
    """

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    async def guard_new_run(self) -> None:
        await self._guard_row("runs")

    async def guard_new_pairing(self) -> None:
        """FR-008's five Shopify pairings per interactive workspace.

        Distinct from §17.1's "at most one *nonterminal* pairing", which the
        partial unique index in migration 9 enforces. That one bounds concurrency;
        this one bounds how many trials a workspace may accumulate over its
        lifetime, and a workspace can be at five with none of them live.
        """
        await self._guard_row("shopify_pairings")

    async def guard_new_artifact(self, byte_size: int) -> None:
        """Both artifact ceilings: the count and the byte total.

        The byte check uses the size the caller declares for the artifact it is
        about to write, so the cap is applied to the state after this insert
        rather than before it — a cap that admitted the write that crossed it
        would be off by one artifact, and that artifact could be 10 MiB.
        """
        if byte_size < 0:
            raise ValueError("an artifact cannot have a negative size")
        await self._guard_row("artifacts")

        row = await self._work.fetch_one(
            "SELECT COALESCE(SUM(byte_size), 0) AS stored FROM artifacts WHERE workspace_id = ?",
            (self._workspace_id,),
        )
        stored = int(row["stored"]) if row else 0
        if stored + byte_size > ARTIFACT_BYTES_PER_WORKSPACE:
            raise self._exceeded(
                f"This workspace may store {ARTIFACT_BYTES_PER_WORKSPACE} bytes of "
                f"artifacts; {stored} are stored and this one adds {byte_size}."
            )

    async def event_budget_remaining(self, run_id: str, *, reserved: int = 0) -> int:
        """Ordinary events this run may still append before the ceiling trips.

        `reserved` holds back the events a later phase is already committed to
        writing. Verification emits one `assertion_evaluated` and one
        `policy_evaluated` per check (§16.1), so a run that spent its whole
        budget on invocations would push the total past FR-008's 250 at the
        moment it tried to produce a verdict — with no acceptable way out, since
        truncating verification events means dropping evidence. The contract is
        fixed at arming (FR-012), so the reservation is exact rather than a
        margin.
        """
        used = await EventRepository(self._work).count(run_id)
        return ORDINARY_EVENTS_PER_RUN - reserved - used

    async def trip_if_event_budget_exhausted(
        self, run_id: str, *, reserved: int = 0
    ) -> ApiError | None:
        """FR-008's boundary, checked at the moment it names.

        "On the next attempted invocation start after 249 ordinary events, the
        server shall atomically move the active run to `error`, append that
        boundary event, reject further target actions, and preserve existing
        evidence."

        **This one returns its refusal instead of raising it, and the direction
        is the opposite of every other guard here.** A creation past a cap must
        commit nothing, so `guard_new_run` raises and the transaction unwinds. A
        run that hits the event ceiling must commit *more*: the status change
        and the boundary event are the record of why it stopped, and raising
        inside the unit of work would roll back the very evidence FR-008
        requires. So the caller commits, then raises what this returned.

        `None` means the budget still holds and the invocation may proceed.

        Existing evidence is preserved by construction: nothing here deletes,
        and the boundary event is appended after the events it explains rather
        than in place of them.
        """
        if await self.event_budget_remaining(run_id, reserved=reserved) > 0:
            return None

        updated = await self._work.execute(
            "UPDATE runs SET status = ?, completed_at = ? WHERE id = ? AND workspace_id = ?",
            (str(RunState.ERROR.value), self._work.now(), run_id, self._workspace_id),
        )
        if updated.rowcount == 0:
            # The run is not this workspace's, so there is nothing to stop and
            # nothing to record. FR-006: a stranger cannot move somebody else's
            # run to `error`, and a refusal that named the run would confirm it
            # exists.
            return not_found()

        await EventRepository(self._work).append(
            run_id,
            {
                "event_type": str(OutcomeEventType.RESOURCE_LIMIT_EXCEEDED.value),
                # `harness`, not `agent`: the boundary event is the server
                # speaking about the run. Attributing it to the agent would put
                # a sentence in the mouth of the thing under test (§17.1
                # `events.actor`).
                "actor": str(EventActor.HARNESS.value),
                "status": str(ApiErrorCode.EVENT_LIMIT_EXCEEDED.value),
                "redacted_payload": {
                    "code": str(ApiErrorCode.EVENT_LIMIT_EXCEEDED.value),
                    "limit": ORDINARY_EVENTS_PER_RUN,
                },
            },
        )
        return ApiError(
            ApiErrorCode.EVENT_LIMIT_EXCEEDED,
            f"This run reached its ceiling of {ORDINARY_EVENTS_PER_RUN} events. "
            "It has been moved to error and its evidence is preserved.",
        )

    async def _guard_row(self, table: str) -> None:
        ceiling, noun = _ROW_CEILINGS[table]
        row = await self._work.fetch_one(
            f"SELECT COUNT(*) AS total FROM {table} WHERE workspace_id = ?",
            (self._workspace_id,),
        )
        total = int(row["total"]) if row else 0
        if total >= ceiling:
            raise self._exceeded(f"This workspace may hold {ceiling} {noun}; it holds {total}.")

    @staticmethod
    def _exceeded(reason: str) -> ApiError:
        return ApiError(ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED, f"{reason} {_REMEDY}")


class EventStreamSlots:
    """FR-008's "two concurrent event-stream connections", counted per workspace.

    Every other ceiling in this file counts rows, so the database answers "how
    many are there" and a transaction makes the answer trustworthy. This one
    counts *connections currently held open*, which nothing persists: an SSE
    response is a live object in one process, and it stops existing when that
    process does.

    **So this is per-process, in-memory state, and that is the whole of its
    reach.** It does not survive a restart — after which every workspace starts
    from zero, which is correct, because the connections it was counting died
    with the process. It does not span workers either; §29.1 deploys a single
    worker over one SQLite file, and a second worker would give each its own
    counter and therefore each its own allowance. That is a deployment-shape
    dependency worth stating out loud rather than a limitation to hide: moving
    to multiple workers means moving this count somewhere both can see.

    **The map is bounded by what is open, not by what has been seen.** A
    workspace's entry appears when it opens its first stream and is deleted as
    its last one closes, the same reference-counting `WorkspaceLocks` uses and
    for the same reason: FR-009's workspaces are anonymous and cheap to create,
    so a dictionary that kept one entry per workspace *ever* observed would be a
    slow leak fed entirely by anonymous traffic — the leak class `release_idle`
    exists to prevent for the rate limiter's buckets. Counting down to zero is
    stronger than a periodic sweep, because it never leaves an entry alive
    between sweeps; `release_idle` is kept for parity and for a test that wants
    to say the map really is empty rather than believe it.
    """

    def __init__(self, *, ceiling: int = CONCURRENT_EVENT_STREAMS) -> None:
        self._open: dict[str, int] = {}
        self._ceiling = ceiling

    def __len__(self) -> int:
        """Workspaces holding at least one stream. Zero when nothing is open."""
        return len(self._open)

    def open_streams(self, workspace_id: str) -> int:
        """How many streams this workspace holds right now."""
        return self._open.get(workspace_id, 0)

    @contextmanager
    def reserve(self, workspace_id: str) -> Iterator[None]:
        """Hold one of this workspace's stream slots, or refuse the connection.

        Synchronous on purpose. There is no `await` between reading the count and
        incrementing it, so two connections arriving in the same tick cannot both
        see room and both take the last slot — the atomicity is a property of the
        code's shape rather than of a lock somebody has to remember to hold.

        A context manager rather than a pair of methods for the same reason: the
        release is the caller's `finally` whether it exits normally, by
        cancellation, or by exception. A leaked slot would lower this workspace's
        ceiling for the life of the process, which is worse than no ceiling at
        all — a cap that only ever shrinks eventually refuses everything.
        """
        held = self._open.get(workspace_id, 0)
        if held >= self._ceiling:
            raise ApiError(
                ApiErrorCode.WORKSPACE_LIMIT_EXCEEDED,
                f"This workspace may hold {self._ceiling} concurrent event-stream "
                f"connections; {held} are open. Close one, or read the same timeline "
                "from the paged events endpoint.",
            )
        self._open[workspace_id] = held + 1
        try:
            yield
        finally:
            self._release(workspace_id)

    def release_idle(self) -> int:
        """Drop every workspace holding no streams. Returns how many went.

        Normally finds nothing: `reserve` already deletes an entry as its last
        stream closes. It exists so a periodic pass can call it without knowing
        that, and so a test can show the map is bounded rather than assume it.
        """
        idle = [key for key, held in self._open.items() if held <= 0]
        for key in idle:
            del self._open[key]
        return len(idle)

    def _release(self, workspace_id: str) -> None:
        remaining = self._open.get(workspace_id, 0) - 1
        if remaining > 0:
            self._open[workspace_id] = remaining
            return
        self._open.pop(workspace_id, None)
