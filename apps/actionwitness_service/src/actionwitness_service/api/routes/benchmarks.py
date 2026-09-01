"""Benchmark routes (spec v1.9 §15.6, FR-090–FR-094).

| Method | Endpoint                                | Purpose |
|--------|-----------------------------------------|---------|
| `POST` | `/benchmarks`                           | Create a suite from a validated manifest |
| `POST` | `/benchmarks/{id}/imports`              | Import, validate, redact, preserve, normalize |
| `PUT`  | `/benchmarks/{id}/bindings`             | Save explicit one-to-one trial bindings |
| `POST` | `/benchmarks/{id}/replay`               | Execute eligible replay trials in isolation |
| `POST` | `/benchmarks/{id}/finalize`             | Create the immutable derived artifact |
| `GET`  | `/benchmarks/{id}`                      | Status, metadata, matrix, metrics, trials |
| `GET`  | `/benchmarks/{id}/trials/{trial_id}`    | Bounded redacted evidence for one trial |
| `GET`  | `/benchmarks/{id}/report`               | Download the immutable benchmark report |

**The import body is bytes, not parsed JSON.** FR-090 caps the artifact at 1 MiB
and BUILD_ORDER §7/M7 says "before parsing". A FastAPI model parameter would
have parsed the document before any handler ran, spending exactly the cost the
cap exists to prevent — so the raw body is read and handed to the reader, which
measures first.

**`/report` returns the stored bytes.** A benchmark is identified by its content
hash, and a reader who downloads one must be able to recompute that hash and get
the same answer. Re-serialising here would produce a document that is equal but
not identical, and the hash would not match.

**Bindings are `PUT` and the suite must still be `draft`.** §16.4 freezes them at
`ready`; the service refuses afterwards, and this layer does not soften it.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from actionwitness_core.benchmarks.enums import CorrelationMode, SourceKind
from actionwitness_core.benchmarks.models import ScenarioDefinition, TrialBinding
from actionwitness_core.kernel import CoreError
from fastapi import APIRouter, Body, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from actionwitness_service.api.dependencies import (
    ArtifactsDependency,
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    SettingsDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.benchmark_service import BenchmarkService

__all__ = ["router"]

router = APIRouter(tags=["benchmarks"])

BenchmarkId = Annotated[str, Path(min_length=1, max_length=128)]
TrialId = Annotated[str, Path(min_length=1, max_length=128)]


class _Body(BaseModel):
    """Closed request bodies: an unknown field is a rejection, not a shrug."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScenarioRequest(_Body):
    """§24.7 step 1: the target configuration one scenario runs under.

    Declared by the benchmark rather than read from the evaluator report, which
    describes what a model called and not what it called it against.
    """

    scenario_id: Annotated[str, Field(min_length=1, max_length=128)]
    scenario_mode: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    failure_profile: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class CreateBenchmarkRequest(_Body):
    """§15.6: "create a benchmark suite from a validated manifest"."""

    source_kind: SourceKind = SourceKind.RECORDED_FIXTURE
    correlation_mode: CorrelationMode = CorrelationMode.IMPORTED_TRAJECTORY_REPLAY
    scenarios: tuple[ScenarioRequest, ...] = ()


class BindingRequest(_Body):
    """One explicit one-to-one binding (FR-091)."""

    external_trial_id: Annotated[str, Field(min_length=1, max_length=128)]
    outcome_run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    evaluation_run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    #: FR-091's "explicit one-to-one developer choice" for a trial whose
    #: `(test.name, runIndex)` was absent or duplicated. Named in the request
    #: rather than inferred, because the whole point is that a human decided.
    acknowledge_unaddressable: bool = False


class BindingsRequest(_Body):
    bindings: tuple[BindingRequest, ...] = ()
    #: §16.4's `draft` → `ready`. Optional so a caller can save bindings in
    #: several calls and seal once, rather than being forced to send them all
    #: at once or to seal prematurely.
    seal: bool = False


_DEFAULT_CREATE = CreateBenchmarkRequest()


@router.post("/benchmarks", status_code=201)
async def create_benchmark(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    settings: SettingsDependency,
    request: Annotated[CreateBenchmarkRequest, Body()] = _DEFAULT_CREATE,
) -> dict[str, Any]:
    """§15.6: a new suite, in `draft`.

    **A client cannot claim a live run.** AC-17 requires the *application* to
    label a live suite `live_model_run`, and §25.3 requires a checked-in report
    never to be "presented as a live execution" — so `live_model_run` is
    accepted only where a live backend is actually configured.

    Refused rather than quietly downgraded. A caller who asked for a live run
    and silently received a fixture-labelled suite would go on to present its
    numbers as a model result, which is the precise misrepresentation the two
    requirements exist to prevent.
    """
    from integrations.google_evals.live import source_kind_for

    if request.source_kind is SourceKind.LIVE_MODEL_RUN:
        available = source_kind_for(settings.live_evaluator)
        if available is not SourceKind.LIVE_MODEL_RUN:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This deployment has no configured live model backend, so a suite "
                "cannot be labelled `live_model_run`. Import the checked-in report "
                "as `recorded_fixture` instead — it produces the same matrix and "
                "says truthfully where it came from.",
            )

    async with locks.hold(workspace_id), database.transaction() as work:
        benchmark_id = await BenchmarkService(work, workspace_id).create(
            source_kind=request.source_kind,
            correlation_mode=request.correlation_mode,
            scenarios=tuple(
                ScenarioDefinition(
                    scenario_id=scenario.scenario_id,
                    scenario_mode=scenario.scenario_mode,
                    failure_profile=scenario.failure_profile,
                )
                for scenario in request.scenarios
            ),
        )
    return {
        "benchmark_id": benchmark_id,
        "status": "draft",
        "source_kind": request.source_kind.value,
        "correlation_mode": request.correlation_mode.value,
    }


@router.post("/benchmarks/{benchmark_id}/imports", status_code=201)
async def import_evaluator_report(
    benchmark_id: BenchmarkId,
    http_request: Request,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """§15.6: "import, validate, redact, preserve, and normalize".

    In that order, and the order is the control (FR-090). The raw body is read
    as bytes so the size cap precedes parsing; the redacted document is what is
    hashed and preserved as the immutable source artifact; normalization runs
    last, over a document that has already been validated and redacted.
    """
    from integrations.google_evals.live import (
        CredentialMaterialRejected,
        screen_for_credential_material,
    )
    from integrations.google_evals.normalize import normalize
    from integrations.google_evals.reader import ImportLimits, ReportRejected, read_report

    limits = settings.evaluator_import
    if limits is None:
        raise ApiError(
            ApiErrorCode.TARGET_UNAVAILABLE,
            "Evaluator import is disabled in this deployment.",
        )

    raw = await http_request.body()
    try:
        imported = read_report(
            raw,
            limits=ImportLimits(max_bytes=limits.max_report_bytes, max_trials=limits.max_trials),
        )
    except ReportRejected as rejected:
        raise _rejection(rejected) from rejected

    # FR-099: a credential must never arrive through an uploaded manifest.
    # Screened *before* anything is written, because a secret in a persisted
    # artifact is an incident rather than a validation failure — and because
    # refusing the whole import is what makes the value's existence visible
    # instead of quietly redacted away.
    try:
        screen_for_credential_material(imported.document, settings.live_evaluator)
    except CredentialMaterialRejected as carried:
        raise ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(carried)) from carried

    async with locks.hold(workspace_id), database.transaction() as work:
        service = BenchmarkService(work, workspace_id)
        suite = await service.get(benchmark_id)
        mode = CorrelationMode(str(suite["correlation_mode"]))
        normalized = normalize(imported, correlation_mode=mode)

        # The *redacted* document is the artifact, hashed as stored.
        written = artifacts.write(
            workspace_id,
            benchmark_id,
            dict(imported.document),
            artifact_type="evaluator_report",
            schema_version=imported.reporter_schema,
        )
        source_artifact_id = await artifacts.record(
            work,
            workspace_id,
            None,
            written,
            metadata={"reporter_schema": imported.reporter_schema, "redacted": imported.redacted},
            benchmark_suite_id=benchmark_id,
        )
        await service.record_import(
            benchmark_id,
            source_artifact_id=source_artifact_id,
            trials=normalized.trials,
            manifest_fields=normalized.manifest_fields,
        )

    return {
        "benchmark_id": benchmark_id,
        "source_artifact_id": source_artifact_id,
        "content_hash": imported.content_hash,
        "reporter_schema": imported.reporter_schema,
        "normalized_adapter_version": imported.normalizer_version,
        "trial_count": imported.trial_count,
        # FR-091: these need an explicit human choice before they can bind.
        "unaddressable_trial_ids": list(normalized.unaddressable_trial_ids),
    }


@router.put("/benchmarks/{benchmark_id}/bindings")
async def save_bindings(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    request: Annotated[BindingsRequest, Body()],
) -> dict[str, Any]:
    """§15.6: "validate and save explicit one-to-one trial bindings before the
    suite becomes ready".

    Every binding in one transaction: FR-091's guarantee is about the set, and a
    partially applied batch would leave a suite whose bindings nobody chose.
    """
    async with locks.hold(workspace_id), database.transaction() as work:
        service = BenchmarkService(work, workspace_id)
        suite = await service.get(benchmark_id)
        mode = CorrelationMode(str(suite["correlation_mode"]))
        for binding in request.bindings:
            try:
                model = TrialBinding(
                    external_trial_id=binding.external_trial_id,
                    correlation_mode=mode,
                    outcome_run_id=binding.outcome_run_id,
                    evaluation_run_id=binding.evaluation_run_id,
                )
            except CoreError as invalid:
                # A binding naming both references or neither is ambiguity in
                # the request, not a malformed document.
                raise ApiError(ApiErrorCode.TRIAL_BINDING_AMBIGUOUS, str(invalid)) from invalid
            await service.bind(
                benchmark_id,
                model,
                acknowledge_unaddressable=binding.acknowledge_unaddressable,
            )
        status = await service.seal(benchmark_id) if request.seal else None

    return {
        "benchmark_id": benchmark_id,
        "bound": len(request.bindings),
        "status": status.value if status is not None else str(suite["status"]),
    }


@router.post("/benchmarks/{benchmark_id}/replay")
async def replay_benchmark(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    registry: RegistryDependency,
) -> dict[str, Any]:
    """§15.6: "execute eligible `imported_trajectory_replay` trials in isolated
    eval workspaces".

    Deliberately outside the workspace lock. Each replay creates its own eval
    workspace and its own transactions, and holding the caller's write lock
    across that I/O would violate ADR-0003's rule that nothing async holds a
    lock across a wait.
    """
    from actionwitness_service.application.benchmark_replay import (
        BenchmarkReplayService,
        TrialReplayInput,
        stored_trajectory,
    )
    from actionwitness_service.application.workspaces import WorkspaceStore

    async with database.reading() as work:
        service = BenchmarkService(work, workspace_id)
        suite = await service.get(benchmark_id)
        rows = await service.trials(benchmark_id)

    if CorrelationMode(str(suite["correlation_mode"])) is not (
        CorrelationMode.IMPORTED_TRAJECTORY_REPLAY
    ):
        raise ApiError(
            ApiErrorCode.PRECONDITION_FAILED,
            "Only an imported_trajectory_replay suite replays; an executed_browser "
            "suite binds to runs that already happened (FR-091).",
        )

    async with database.transaction() as work:
        await BenchmarkService(work, workspace_id).start(benchmark_id)

    contract, adapter_id = await _scenario_inputs(database, registry, workspace_id)
    replayer = BenchmarkReplayService(database, registry, WorkspaceStore(database))
    replayed = []
    for row in rows:
        trajectory = stored_trajectory(row["metadata_json"])
        outcome = await replayer.replay(
            TrialReplayInput(
                trial_row_id=str(row["id"]),
                external_trial_id=str(row["external_trial_id"]),
                trajectory=trajectory,
                contract=contract,
                scenario=_scenario_of(row),
            ),
            owner_workspace_id=workspace_id,
            adapter_id=adapter_id,
        )
        replayed.append(
            {
                "external_trial_id": outcome.external_trial_id,
                "outcome_result": outcome.outcome_result.value,
                "eligibility": outcome.eligibility.value,
                "exclusion_reason": (
                    None if outcome.exclusion_reason is None else outcome.exclusion_reason.value
                ),
                "evaluation_run_id": outcome.evaluation_run_id,
            }
        )

    return {"benchmark_id": benchmark_id, "replayed": replayed}


@router.post("/benchmarks/{benchmark_id}/finalize")
async def finalize_benchmark(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
) -> dict[str, Any]:
    """§15.6: "validate coverage and create an immutable derived benchmark
    artifact".

    §16.4's error path is honoured here rather than left to the caller: a
    refusal rolls its own transaction back, and the suite is then marked `error`
    in a fresh one so no partial result survives.
    """
    try:
        async with locks.hold(workspace_id), database.transaction() as work:
            artifact_id = await BenchmarkService(work, workspace_id).finalize(
                benchmark_id, artifacts
            )
    except CoreError as refused:
        async with locks.hold(workspace_id), database.transaction() as work:
            await BenchmarkService(work, workspace_id).mark_error(benchmark_id)
        raise ApiError(ApiErrorCode.PRECONDITION_FAILED, str(refused)) from refused

    async with database.reading() as work:
        suite = await BenchmarkService(work, workspace_id).get(benchmark_id)
    return {
        "benchmark_id": benchmark_id,
        "status": str(suite["status"]),
        "result_artifact_id": artifact_id,
    }


@router.get("/benchmarks/{benchmark_id}")
async def read_benchmark(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.6: "status, metadata, matrix, metrics, and trial summaries"."""
    async with database.reading() as work:
        service = BenchmarkService(work, workspace_id)
        suite = await service.get(benchmark_id)
        summary = await service.summarize(benchmark_id)

    return {
        "benchmark_id": benchmark_id,
        "status": str(suite["status"]),
        # AC-16: the source kind is shown and never represented as a live run.
        "source_kind": str(suite["source_kind"]),
        "correlation_mode": str(suite["correlation_mode"]),
        "normalized_adapter_version": str(suite["normalized_adapter_version"]),
        "result_artifact_id": suite["result_artifact_id"],
        "manifest": json.loads(str(suite["manifest_json"])),
        "counts": summary.counts.canonical_document(),
        "metrics": summary.metrics.canonical_document(),
        "by_scenario": [group.canonical_document() for group in summary.by_scenario],
        "by_failure_profile": [group.canonical_document() for group in summary.by_failure_profile],
        "trials": [
            {
                "external_trial_id": trial.external_trial_id,
                "scenario_id": trial.scenario_id,
                "call_level_result": trial.call_level_result.value,
                "outcome_result": trial.outcome_result.value,
                "eligibility": trial.eligibility.value,
                "exclusion_reason": (
                    None if trial.exclusion_reason is None else trial.exclusion_reason.value
                ),
                "addressable": trial.addressable,
            }
            for trial in summary.trials
        ],
    }


@router.get("/benchmarks/{benchmark_id}/trials/{trial_id}")
async def read_trial(
    benchmark_id: BenchmarkId,
    trial_id: TrialId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.6: "bounded redacted call-level and outcome evidence for one trial".

    Bounded: the trajectory is a list of tool names and their already-redacted
    arguments, and the response carries no evaluator prose. §20.3 keeps the full
    document in the immutable source artifact, where an auditor reads it
    deliberately rather than through a list view.
    """
    from actionwitness_service.application.benchmark_metrics import trial_from_row

    async with database.reading() as work:
        rows = await BenchmarkService(work, workspace_id).trials(benchmark_id)
    match = next((row for row in rows if str(row["external_trial_id"]) == trial_id), None)
    if match is None:
        raise ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, f"no trial {trial_id!r} here")

    trial = trial_from_row(match)
    return {
        "external_trial_id": trial.external_trial_id,
        "scenario_id": trial.scenario_id,
        "correlation_mode": trial.correlation_mode.value,
        "call_level_result": trial.call_level_result.value,
        "outcome_result": trial.outcome_result.value,
        "eligibility": trial.eligibility.value,
        "exclusion_reason": (
            None if trial.exclusion_reason is None else trial.exclusion_reason.value
        ),
        "addressable": trial.addressable,
        "outcome_run_id": trial.outcome_run_id,
        "evaluation_run_id": trial.evaluation_run_id,
        "scenario_mode": trial.scenario_mode,
        "failure_profile": trial.failure_profile,
        "trajectory": [dict(step) for step in trial.trajectory],
        "unsupported_metadata": dict(trial.metadata),
        "source_artifact_id": str(match["external_source_artifact_id"]),
    }


@router.get("/benchmarks/{benchmark_id}/report")
async def download_benchmark_report(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    artifacts: ArtifactsDependency,
) -> Response:
    """§15.6: "download the immutable benchmark JSON report".

    The stored bytes, verbatim. A reader must be able to recompute the content
    hash and get the same answer, which only holds if they receive what was
    written rather than a re-serialisation of it.
    """
    async with database.reading() as work:
        suite = await BenchmarkService(work, workspace_id).get(benchmark_id)
        artifact_id = suite["result_artifact_id"]
        if artifact_id is None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This benchmark has not been finalized, so there is no report yet.",
            )
        row = await work.fetch_one(
            "SELECT relative_path FROM artifacts WHERE id = ? AND workspace_id = ?",
            (str(artifact_id), workspace_id),
        )
    if row is None:  # pragma: no cover - finalization commits both together
        raise ApiError(ApiErrorCode.HARNESS_ERROR, "The benchmark artifact has gone.")

    return Response(
        content=artifacts.read_bytes(str(row["relative_path"])),
        media_type="application/json",
    )


def _rejection(rejected: Exception) -> ApiError:
    """An unreadable report is about the *file*, never about the target.

    422 rather than 409: the document cannot be made acceptable by retrying, and
    a caller needs to know it is theirs to fix.
    """
    return ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(rejected))


def _scenario_of(row: Any) -> Any:
    """The scenario a replayed trial runs under.

    Taken from the trial's own recorded columns. A replay that read the
    workspace's *current* scenario would judge the trial against a
    configuration nobody recorded for it.
    """
    from actionwitness_core.ports.models import ScenarioSelection

    return ScenarioSelection(
        scenario_mode=str(row["scenario_mode"] or "post_fix"),
        fault_profile=row["failure_profile"],
    )


async def _scenario_inputs(database: Any, registry: Any, workspace_id: str) -> tuple[Any, str]:
    """The contract that judges a replay, and the adapter that runs it.

    §24.7 step 1 puts the contract in the *scenario*. Until a manifest carries
    one per scenario, the workspace's selected contract is what a replay is
    judged against — and a suite with no contract selected is refused rather
    than judged against nothing.
    """
    from actionwitness_core.contracts.models import OutcomeContract

    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT selected_contract_id, selected_target_id FROM workspaces WHERE id = ?",
            (workspace_id,),
        )
        contract_id = None if row is None else row["selected_contract_id"]
        if contract_id is None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "Select an outcome contract before replaying: a replay with no "
                "contract has nothing to judge the target against.",
            )
        document = await work.fetch_one(
            "SELECT document_json FROM contracts WHERE id = ?", (str(contract_id),)
        )
    if document is None:  # pragma: no cover - contracts are immutable
        raise ApiError(ApiErrorCode.HARNESS_ERROR, "The selected contract has gone.")
    # A workspace names a *target* (`buggy-store`); the registry is keyed by
    # *module* (`buggy_store`). `resolve` accepts either, and resolving here
    # means the replay reaches the same adapter the run path would.
    slot = registry.resolve(row["selected_target_id"] if row else None)
    if slot is None:
        raise ApiError(
            ApiErrorCode.TARGET_UNAVAILABLE,
            "No target adapter is selected for this workspace, so there is nothing "
            "to replay the imported trajectory through.",
        )
    contract = OutcomeContract.model_validate(json.loads(str(document["document_json"])))
    return contract, slot.name
