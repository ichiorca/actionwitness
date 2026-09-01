"""Workspace and capability routes (spec v1.9 §15.1).

| Method | Endpoint                      |
|--------|-------------------------------|
| `GET`  | `/workspace`                  |
| `POST` | `/workspace/reset`            |
| `PUT`  | `/workspace/failure-profile`  |
| `PUT`  | `/workspace/scenario-mode`    |

Every handler opens its own short unit of work around the work and nothing else
(ADR-0003: nothing is held across a wait), and every mutation is admitted
through the per-workspace lock first, so two tabs resetting at once queue
instead of racing into SQLite.

Request bodies are explicit Pydantic models that forbid unknown fields. The
constitution requires it, and the practical reason is that `{"purge": true}`
silently ignored is a user who believes they purged.
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
from actionwitness_service.application.workspace_service import WorkspaceService

__all__ = ["router"]

router = APIRouter(prefix="/workspace", tags=["workspace"])


class _Body(BaseModel):
    """Unknown fields are refused, not ignored (constitution §5)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ResetRequest(_Body):
    purge_completed: bool = False


#: A module-level singleton because the model is frozen, so every request that
#: omits a body shares one immutable default rather than constructing one.
_DEFAULT_RESET = ResetRequest()


class FailureProfileRequest(_Body):
    #: An opaque token. §9.1 keeps the harness from interpreting profile names;
    #: `None` clears the selection, which FR-011's `none` profile means.
    failure_profile: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class ScenarioModeRequest(_Body):
    scenario_mode: Annotated[str, Field(min_length=1, max_length=64)]


@router.get("")
async def read_workspace(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    registry: RegistryDependency,
) -> dict[str, Any]:
    """§15.1: target, scenario configuration, contract, active run, capability."""
    async with database.reading() as work:
        status = dict(await WorkspaceService(work, workspace_id).status())
    status["capabilities"] = registry.capability_report()
    return status


@router.post("/reset")
async def reset_workspace(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    request: Annotated[ResetRequest, Body()] = _DEFAULT_RESET,
) -> dict[str, Any]:
    """FR-013. Cancels what is in flight; keeps what is finished.

    `purge_completed` is the only path that removes terminal evidence, and it
    is opt-in for that reason.
    """
    async with locks.hold(workspace_id), database.transaction() as work:
        outcome = await WorkspaceService(work, workspace_id).reset(
            purge_completed=request.purge_completed
        )
    return {
        "runs_cancelled": outcome.runs_cancelled,
        "confirmations_cancelled": outcome.confirmations_cancelled,
        "runs_purged": outcome.runs_purged,
        "artifacts_purged": outcome.artifacts_purged,
        "selected_contract_id": outcome.contract_retained,
    }


@router.put("/failure-profile")
async def select_failure_profile(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    request: Annotated[FailureProfileRequest, Body()],
) -> dict[str, Any]:
    """FR-011: chosen before arming, refused while a run is in flight."""
    async with locks.hold(workspace_id), database.transaction() as work:
        await WorkspaceService(work, workspace_id).select_failure_profile(request.failure_profile)
    return {"failure_profile": request.failure_profile}


@router.put("/scenario-mode")
async def select_scenario_mode(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    request: Annotated[ScenarioModeRequest, Body()],
) -> dict[str, Any]:
    """§15.1: validated against the active adapter's descriptor, not a constant.

    The supported list comes from whichever adapter this workspace selected. A
    target advertising only `external_current` therefore disables the pre/post
    control without this route knowing what `pre_fix` would have meant (§9.1).
    """
    async with locks.hold(workspace_id), database.transaction() as work:
        service = WorkspaceService(work, workspace_id)
        selected = (await service.status())["selected_target_id"]
        supported = registry.supported_scenario_modes(selected)
        await service.select_scenario_mode(request.scenario_mode, supported)
    return {"scenario_mode": request.scenario_mode}
