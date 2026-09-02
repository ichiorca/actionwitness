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

The three mutating routes also reach the *target* through the adapter, and that
is why each of them is three phases rather than one transaction: read the
selection, prepare the target, then record. Preparing is I/O, so it happens
outside the lock; recording afterwards is what keeps the stored selection and
the target's actual scenario from drifting apart.

Request bodies are explicit Pydantic models that forbid unknown fields. The
constitution requires it, and the practical reason is that `{"purge": true}`
silently ignored is a user who believes they purged.
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from actionwitness_core.ports.enums import ExecutionMode
from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field

from actionwitness_service.api.dependencies import (
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.scenario_service import ScenarioPreparer
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

    # §15.1: "the configuration panel exposes only adapter-supported scenario
    # controls". Added here rather than in `WorkspaceService` for the same reason
    # `capabilities` is: the registry is the composition root's, and the service
    # layer has no business holding a handle to it.
    #
    # Both lists come from the selected target's own descriptor and are opaque
    # tokens to everything above the adapter. Publishing them interprets nothing
    # — it only stops the UI having to. Until it did, the failure-profile control
    # was a free-text box, and reaching the demo meant reading the adapter source
    # to find the exact spelling of a token nobody could discover from the page.
    target = status["selected_target_id"]
    status["supported_scenario_modes"] = list(registry.supported_scenario_modes(target))
    status["supported_fault_profiles"] = list(registry.supported_fault_profiles(target))

    status["capabilities"] = registry.capability_report()
    # Additive alongside `capabilities`, which stays the target list the UI's
    # capability bar renders. `modules` is the wider view a judge needs: a feature
    # that was cut has to be visibly off rather than simply absent (§8, M11).
    status["modules"] = registry.module_report()
    return status


@router.post("/reset")
async def reset_workspace(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    request: Annotated[ResetRequest, Body()] = _DEFAULT_RESET,
) -> dict[str, Any]:
    """FR-013. Cancels what is in flight, keeps what is finished, reseeds the target.

    The order is the requirement's own: cancel the run and append its
    cancellation events, *then* reseed. A reseed that ran first would wipe the
    state the cancelled run's evidence describes.

    `purge_completed` is the only path that removes terminal evidence, and it is
    opt-in for that reason.
    """
    async with locks.hold(workspace_id), database.transaction() as work:
        service = WorkspaceService(work, workspace_id)
        status = await service.status()
        outcome = await service.reset(purge_completed=request.purge_completed)

    # Outside the transaction and the lock: preparing the target is I/O and
    # ADR-0003 holds neither across a wait.
    reseed = await ScenarioPreparer(registry).prepare(
        workspace_id,
        target_id=status["selected_target_id"],
        scenario_mode=status["scenario_mode"],
        failure_profile=status["failure_profile"],
    )

    return {
        "runs_cancelled": outcome.runs_cancelled,
        "confirmations_cancelled": outcome.confirmations_cancelled,
        "runs_purged": outcome.runs_purged,
        "artifacts_purged": outcome.artifacts_purged,
        "selected_contract_id": outcome.contract_retained,
        # FR-013 reseeds "when supported", so the caller is told whether it
        # happened. A silent no-op would leave them believing the target is
        # clean when it holds whatever the last run left behind.
        "target_reseeded": reseed.reseeded,
        "reseed_detail": reseed.reason,
    }


@router.put("/failure-profile")
async def select_failure_profile(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    request: Annotated[FailureProfileRequest, Body()],
) -> dict[str, Any]:
    """FR-011: chosen before arming, and the target is told about it.

    The profile is recorded in both scenario modes. FR-011 keeps it as the
    comparison fault in `post_fix` and lets the adapter disable it, which is
    what makes a matched pre/post pair differ in exactly one variable.
    """
    return await _select_and_prepare(
        workspace_id,
        database,
        locks,
        registry,
        failure_profile=request.failure_profile,
        answer={"failure_profile": request.failure_profile},
    )


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
    return await _select_and_prepare(
        workspace_id,
        database,
        locks,
        registry,
        scenario_mode=request.scenario_mode,
        answer={"scenario_mode": request.scenario_mode},
    )


async def _select_and_prepare(
    workspace_id: str,
    database: Any,
    locks: Any,
    registry: Any,
    *,
    scenario_mode: str | None = None,
    failure_profile: str | None = None,
    answer: dict[str, Any],
) -> dict[str, Any]:
    """Validate, prepare the target, then record — in that order.

    **Preparation comes before persistence.** A workspace whose column says
    `pre_fix` while the target is still in `post_fix` would arm a run against a
    scenario nobody selected, and every verdict from it would carry a label that
    is not true. If the adapter refuses, nothing is written.

    The selection is re-read inside the writing transaction and compared against
    what was read before the adapter call, because that call is a wait and
    ADR-0003 holds nothing across one. A concurrent change refuses rather than
    recording a selection the target was never prepared for — the same
    optimistic check arming uses, for the same reason.
    """
    preparer = ScenarioPreparer(registry)

    async with database.reading() as work:
        before = await WorkspaceService(work, workspace_id).status()
    _require_supported_mode(before, preparer, scenario_mode)
    _require_demo_only_profile(registry, before["selected_target_id"], failure_profile)
    _require_injectable_profile(registry, before["selected_target_id"], failure_profile)

    target_id = before["selected_target_id"]
    intended_mode = before["scenario_mode"] if scenario_mode is None else scenario_mode
    intended_profile = failure_profile if scenario_mode is None else before["failure_profile"]

    reseed = await preparer.prepare(
        workspace_id,
        target_id=target_id,
        scenario_mode=intended_mode,
        failure_profile=intended_profile,
    )

    async with locks.hold(workspace_id), database.transaction() as work:
        service = WorkspaceService(work, workspace_id)
        confirmed = await service.status()
        if _selection_of(confirmed) != _selection_of(before):
            raise ApiError(
                ApiErrorCode.RUN_IN_PROGRESS,
                "The workspace configuration changed while the target was being "
                "prepared. Nothing was recorded; retry against the current selection.",
            )
        if scenario_mode is not None:
            await service.select_scenario_mode(scenario_mode, preparer.supported_modes(target_id))
        else:
            await service.select_failure_profile(failure_profile)

    return {**answer, "target_reseeded": reseed.reseeded, "reseed_detail": reseed.reason}


def _selection_of(status: dict[str, Any]) -> tuple[Any, ...]:
    """Everything that must not have moved while the target was prepared."""
    return (
        status["selected_target_id"],
        status["scenario_mode"],
        status["failure_profile"],
    )


#: §13.3 restricts these to the embedded demo target: each "shall exist only in
#: the embedded demo target, and shall never be available against external
#: targets". Named individually rather than derived, because the rule is about
#: what the *behaviour does to somebody else's system*, not about which module
#: happens to implement it — `tool_surface_poisoned` registers a look-alike tool
#: in a live page, and `checkout_without_confirmation` creates a real order
#: without consent.
DEMO_ONLY_PROFILES: Final[frozenset[str]] = frozenset(
    {"tool_surface_poisoned", "checkout_without_confirmation"}
)


def _require_demo_only_profile(registry: Any, target_id: Any, failure_profile: str | None) -> None:
    """§13.3: an injected unsafe profile may not be pointed at an external target.

    Refused here rather than in the adapter, because the adapter for an external
    target is exactly the code that must never be asked to do this — a guard
    that lived there would be trusting the thing it guards against. The check is
    on the *selected* target, before anything is prepared or recorded.
    """
    if failure_profile is None or failure_profile not in DEMO_ONLY_PROFILES:
        return
    slot = registry.resolve(None if target_id is None else str(target_id))
    if slot is None or slot.factory is None:
        return
    descriptor = slot.factory().descriptor
    if descriptor.execution_mode is not ExecutionMode.MANAGED:
        raise ApiError(
            ApiErrorCode.EXTERNAL_TARGET_FORBIDDEN_OPERATION,
            f"The {failure_profile!r} profile injects unsafe behaviour and exists only "
            "for the embedded demo target. It can never be selected for an external "
            "target (§13.3).",
            details=[{"path": "failure_profile", "message": "demo-only profile"}],
        )


def _require_injectable_profile(registry: Any, target_id: Any, failure_profile: str | None) -> None:
    """Refuse a profile the target cannot inject (§13.3, M11 cut hygiene; 012-T8).

    §13.3 names six fault profiles and a build ships some of them. A *recognised
    but unbuilt* profile is the dangerous case, and it used to be recorded
    silently: preparation is what asked the target, preparation is skipped when
    no target is selected yet, and arming copies the workspace's profile into
    the run without re-asking. The result was a run whose report named an active
    fault while the store behaved honestly — the harness making exactly the
    false claim it exists to catch.

    Checked before anything is prepared or written, against the adapter's own
    advertised list, so the refusal does not depend on which order an operator
    happened to click.
    """
    if failure_profile is None:
        return
    if registry.injects_fault_profile(
        None if target_id is None else str(target_id), failure_profile
    ):
        return
    raise ApiError(
        ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        f"The selected target cannot inject the {failure_profile!r} fault profile. "
        "It is recognised by the specification and not implemented in this build.",
        details=[{"path": "failure_profile", "message": "recognised but not implemented"}],
    )


def _require_supported_mode(
    status: dict[str, Any], preparer: ScenarioPreparer, scenario_mode: str | None
) -> None:
    """Refuse an unadvertised mode before the target is touched (§9.1).

    Checked here as well as in the service so a mode the adapter never
    advertised never reaches `prepare`. The adapter would refuse it too, but a
    refusal that arrives *after* a reseed attempt is harder to reason about
    than one that arrives instead of it.
    """
    if scenario_mode is None:
        return
    supported = preparer.supported_modes(status["selected_target_id"])
    if scenario_mode not in supported:
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            f"The active target does not support scenario mode {scenario_mode!r}.",
            details=[
                {
                    "path": "scenario_mode",
                    "message": (
                        f"supported: {', '.join(sorted(supported))}"
                        if supported
                        else "no target is selected"
                    ),
                }
            ],
        )
