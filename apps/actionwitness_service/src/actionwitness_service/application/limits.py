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
"""

from __future__ import annotations

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
#: The Tier 2 tables — evaluation cases and runs, benchmark suites and trials,
#: Shopify pairings — are absent from this map because their tables arrive with
#: M6/M7 and counting a table that does not exist is an error, not a ceiling.
#: Their constants are declared above so the numbers land in one place, and
#: `test_every_fr_008_ceiling_is_declared` fails if one is dropped.
_ROW_CEILINGS: Final[dict[str, tuple[int, str]]] = {
    "runs": (OUTCOME_RUNS_PER_WORKSPACE, "outcome runs"),
    "artifacts": (ARTIFACTS_PER_WORKSPACE, "artifacts"),
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

    async def event_budget_remaining(self, run_id: str) -> int:
        """Ordinary events this run may still append before the ceiling trips."""
        return ORDINARY_EVENTS_PER_RUN - await EventRepository(self._work).count(run_id)

    async def trip_if_event_budget_exhausted(self, run_id: str) -> ApiError | None:
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
        if await self.event_budget_remaining(run_id) > 0:
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
