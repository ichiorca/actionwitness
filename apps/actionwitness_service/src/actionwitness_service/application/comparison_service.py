"""Assembling a matched pre/post comparison from stored evidence (FR-019, §23.7).

The comparison itself is the core's — pure, and reaching the same verdict from
the same rows on any machine. This assembles the two `ComparableRun`s from what
was persisted and nothing else, which is what makes that promise keepable: a
comparison computed partly from live state could not be re-derived later, and
§24's replay would have nothing to check against.

**The trajectory comes from the timeline, not from the contract.** FR-019 says
"actual tool trajectory", so it is the calls the agent really made, read back out
of the events in sequence order. The contract's `expected_tools` is what the
trajectory was *judged* against and is already part of the contract hash.

**A run is comparable only to one in this workspace.** Nothing here widens the
authorization boundary: both runs are resolved through `WorkspaceScope`, so a
source run identifier learned elsewhere resolves to nothing (FR-006).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from actionwitness_core.engine.enums import FailureClassification
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState
from actionwitness_core.reports.comparison import ComparableRun, ComparisonResult, compare_runs
from actionwitness_core.reports.enums import LayerResult

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.persistence.database import UnitOfWork

__all__ = ["ComparisonService", "comparable_run"]


class ComparisonService:
    """Reads two runs and asks the core whether they form a pair."""

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    async def compare(self, run_id: str) -> ComparisonResult:
        """§15.3: "a validated matched pre/post comparison or a structured
        ineligibility reason".

        The candidate names its own source. A run armed without one has nothing
        to compare against, which is a refusal rather than a `not_comparable`
        result — there is no pair at all, so there are no differing fields to
        list.
        """
        candidate_row = await WorkspaceScope(self._work, self._workspace_id).run(run_id)
        source_id = candidate_row["comparison_source_run_id"]
        if not source_id:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This run was not armed against a comparison source, so there is no pair "
                "to compare.",
                details=[
                    {
                        "path": "comparison_source_run_id",
                        "message": "arm a run with a source to enable comparison",
                    }
                ],
            )

        source_row = await WorkspaceScope(self._work, self._workspace_id).run(str(source_id))
        return compare_runs(
            await self._comparable(source_row),
            await self._comparable(candidate_row),
        )

    async def _comparable(self, run: Mapping[str, Any]) -> ComparableRun:
        return comparable_run(
            run,
            trajectory=await self.trajectory(str(run["id"])),
            classifications=await self._critical_classifications(str(run["id"])),
        )

    async def trajectory(self, run_id: str) -> tuple[str, ...]:
        """The agent's observed tool calls, in sequence order (FR-019).

        Start events rather than terminal ones: §10.3 compares "eligible
        target-tool invocation-start events", and a call that started and failed
        is still a call the agent made. Human and harness events are excluded,
        because a trajectory is what the *agent* did.
        """
        rows = await self._work.fetch_all(
            "SELECT tool_name FROM events WHERE run_id = ? AND event_type = ? AND actor = ? "
            "ORDER BY sequence_number",
            (
                run_id,
                str(OutcomeEventType.TOOL_INVOCATION_STARTED.value),
                str(EventActor.AGENT.value),
            ),
        )
        return tuple(str(row["tool_name"]) for row in rows if row["tool_name"])

    async def _critical_classifications(self, run_id: str) -> tuple[FailureClassification, ...]:
        """The distinct critical classifications this run failed on (§22).

        Read from the stored findings rather than recomputed, so the comparison
        reports what the run's own verdict said.
        """
        rows = await self._work.fetch_all(
            "SELECT DISTINCT classification FROM findings "
            "WHERE run_id = ? AND status = 'failed' AND severity = 'critical' "
            "AND classification IS NOT NULL ORDER BY classification",
            (run_id,),
        )
        return tuple(FailureClassification(row["classification"]) for row in rows)


def comparable_run(
    run: Mapping[str, Any],
    *,
    trajectory: Sequence[str],
    classifications: Sequence[FailureClassification] = (),
) -> ComparableRun:
    """One stored run row as the core's comparison input."""
    return ComparableRun(
        run_id=str(run["id"]),
        status=RunState(run["status"]),
        scenario_mode=str(run["scenario_mode"] or "unspecified"),
        fault_active=bool(run["fault_active"]),
        target_id=str(run["target_id"]),
        adapter_id=str(run["target_adapter_id"]),
        contract_content_hash=run["contract_content_hash"],
        fixture_content_hash=run["fixture_content_hash"],
        intent_content_hash=run["intent_content_hash"],
        failure_profile=run["failure_profile"],
        implementation_version=str(run["implementation_version"]),
        build_commit=run["build_commit"],
        trajectory=tuple(trajectory),
        overall_result=(
            None if run["overall_result"] is None else LayerResult(run["overall_result"])
        ),
        critical_classifications=tuple(classifications),
    )
