"""Outcome-run routes (spec v1.9 §15.3).

| Method | Endpoint                                          | Status |
|--------|---------------------------------------------------|--------|
| `POST` | `/runs`                                           | 005-T1 |
| `POST` | `/runs/{run_id}/target-tools/{tool_name}:invoke`   | 005-T3 |
| `POST` | `/runs/{run_id}/verify`                           | 005-T6 |
| `GET`  | `/runs/{run_id}/comparison`                       | 005-T11 |

The rest of §15.3 — the run read, paged events, confirmation decisions, and the
report — arrives with the tasks that own it and is deliberately absent rather
than stubbed.

`POST /runs/{run_id}/verify` wins FR-038's race, captures the final
observation, evaluates the contract through the core, and seals the run. The
race is settled before anything is observed, so a losing request cannot capture
a partial final snapshot.

`POST /runs` takes no contract identifier. §15.3 describes arming "a contract",
and FR-024 already made exactly one contract active in the workspace with its
target selected atomically; accepting a second identifier here would reintroduce
the combination FR-024 forbids. The run is armed against what the workspace has
selected, which is also what `GET /workspace` reports.
"""

from __future__ import annotations

from typing import Annotated, Any

from actionwitness_core.reports.enums import RunMode
from fastapi import APIRouter, Body, Path
from pydantic import BaseModel, ConfigDict, Field

from actionwitness_service.api.dependencies import (
    ArtifactsDependency,
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    WorkspaceDependency,
)
from actionwitness_service.application.comparison_service import ComparisonService
from actionwitness_service.application.invocation_service import InvocationService
from actionwitness_service.application.run_service import RunService
from actionwitness_service.application.verification_service import VerificationService

__all__ = ["router"]

router = APIRouter(prefix="/runs", tags=["runs"])


class ArmRequest(BaseModel):
    """§15.3's arming body. Unknown fields are refused, not ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Annotated[str, Field(min_length=1, max_length=32)] = RunMode.VERIFICATION.value
    #: §15.3: "optionally bind an eligible immutable `comparison_source_run_id`".
    #: Eligible means this workspace's own terminal run — checked at arming,
    #: inside the transaction, so a source that moves cannot slip through.
    comparison_source_run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


#: A frozen module-level default, so a request with no body shares one immutable
#: instance rather than constructing one per call.
_DEFAULT_ARM = ArmRequest()


@router.post("", status_code=201)
async def arm_run(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    request: Annotated[ArmRequest, Body()] = _DEFAULT_ARM,
) -> dict[str, Any]:
    """FR-030. Returns 201 with the `run_id`, or refuses and writes nothing."""
    armed = await RunService(database, registry, locks).arm(
        workspace_id,
        mode=request.mode,
        comparison_source_run_id=request.comparison_source_run_id,
    )
    return {
        "run_id": armed.run_id,
        "status": armed.status,
        "contract_id": armed.contract_id,
        "target_id": armed.target_id,
        "scenario_mode": armed.scenario_mode,
        "failure_profile": armed.failure_profile,
        "initial_snapshot": {
            "state_version": armed.state_version,
            "content_hash": armed.snapshot_content_hash,
        },
    }


class InvokeRequest(BaseModel):
    """§15.3's invocation body.

    `arguments` is deliberately an open mapping *here* and closed one layer
    down: the tool's own published schema is the authority on what it accepts,
    and duplicating that as a Pydantic model would give the harness a second
    opinion about a target's surface (§9.1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    arguments: dict[str, Any] = Field(default_factory=dict)
    #: §15.3: "the identity of the tool definition as observed immediately
    #: before dispatch, which FR-169 compares against the armed baseline." It is
    #: recorded on the start event here; the comparison is FR-169's own task.
    tool_identity_hash: Annotated[str, Field(min_length=1, max_length=128)] | None = None


_DEFAULT_INVOKE = InvokeRequest()

RunId = Annotated[str, Path(min_length=1, max_length=128)]
ToolName = Annotated[str, Path(min_length=1, max_length=128)]


@router.post("/{run_id}/target-tools/{tool_name}:invoke")
async def invoke_target_tool(
    run_id: RunId,
    tool_name: ToolName,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    request: Annotated[InvokeRequest, Body()] = _DEFAULT_INVOKE,
) -> dict[str, Any]:
    """One allowlisted target action, recorded either side of its dispatch.

    The response separates what the tool *reported* from what was independently
    *observed*, because a client that saw only the first would have no way to
    know the two disagreed — which is the disagreement this product exists to
    surface.
    """
    outcome = await InvocationService(database, registry, locks).invoke(
        workspace_id,
        run_id,
        tool_name,
        request.arguments,
        tool_identity_hash=request.tool_identity_hash,
    )
    return {
        "invocation_id": outcome.invocation_id,
        "sequence_number": outcome.sequence_number,
        "terminal_event": outcome.terminal_event,
        "reported": {
            "status": outcome.reported_status,
            "summary": outcome.reported_summary,
            "error_code": outcome.error_code,
        },
        "observed": {
            "state_version": outcome.observed_state_version,
            "state_changed": outcome.observed_state_changed,
        },
        "duration_ms": outcome.duration_ms,
        "next_action": outcome.next_action,
    }


@router.post("/{run_id}/verify")
async def verify_run(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    artifacts: ArtifactsDependency,
) -> dict[str, Any]:
    """Capture final state and evaluate (§15.3).

    FastAPI is the sole transition authority (FR-038), so winning the race,
    observing, judging, and sealing all happen here rather than being split
    across a client's two calls.

    The findings are summarised rather than returned whole: §23 owns the report
    and returning a second, differently-shaped view of the same verdict would
    give a client two places to read it from.
    """
    outcome = await VerificationService(database, registry, locks, artifacts).verify(
        workspace_id, run_id
    )
    return {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "overall_result": outcome.overall_result,
        "primary_failure": outcome.primary_failure_check_id,
        "findings": {
            "total": len(outcome.findings),
            "failed": sum(1 for finding in outcome.findings if finding.failed),
        },
        "final_snapshot": {"state_version": outcome.final_state_version},
        # §23.1's five layers, and the report's own identity. The full document
        # is an immutable artifact; this is the summary a caller needs to decide
        # whether to fetch it.
        "layers": outcome.report.layers.canonical_document(),
        "counts": outcome.report.counts.canonical_document(),
        "report_content_hash": outcome.report.content_hash(),
        "next_action": outcome.next_action,
    }


@router.get("/{run_id}/comparison")
async def read_comparison(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.3: "a validated matched pre/post comparison or a structured
    ineligibility reason".

    A mismatched pair is a **200 with `comparable: false`**, not an error. FR-019
    and §23.7 both say the rerun "remains an ordinary rerun" — it is a perfectly
    good run that simply cannot be read as the other one's counterpart, and
    returning a failure would push somebody to make the pair match by weakening
    what they meant to test.
    """
    async with database.reading() as work:
        result = await ComparisonService(work, workspace_id).compare(run_id)
    return dict(result.as_document())
