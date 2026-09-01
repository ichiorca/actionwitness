"""Outcome-run routes (spec v1.9 §15.3).

| Method | Endpoint  | Status |
|--------|-----------|--------|
| `POST` | `/runs`   | 005-T1 |

The rest of §15.3 — the run read, paged events, tool invocation, confirmation
decisions, verify, report, and comparison — arrives with the tasks that own it
and is deliberately absent rather than stubbed.

`POST /runs` takes no contract identifier. §15.3 describes arming "a contract",
and FR-024 already made exactly one contract active in the workspace with its
target selected atomically; accepting a second identifier here would reintroduce
the combination FR-024 forbids. The run is armed against what the workspace has
selected, which is also what `GET /workspace` reports.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field

from actionwitness_service.api.dependencies import (
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    WorkspaceDependency,
)
from actionwitness_service.application.run_service import RunMode, RunService

__all__ = ["router"]

router = APIRouter(prefix="/runs", tags=["runs"])


class ArmRequest(BaseModel):
    """§15.3's arming body. Unknown fields are refused, not ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Annotated[str, Field(min_length=1, max_length=32)] = RunMode.VERIFICATION


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
    armed = await RunService(database, registry, locks).arm(workspace_id, mode=request.mode)
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
