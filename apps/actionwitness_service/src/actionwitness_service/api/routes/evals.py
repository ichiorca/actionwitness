"""Regression eval routes (spec v1.9 §15.4, FR-080–FR-088).

| Method | Endpoint                                | Status |
|--------|-----------------------------------------|--------|
| `POST` | `/runs/{run_id}/evals`                  | 007-T11 |
| `GET`  | `/evals`                                | 007-T11 |
| `GET`  | `/evals/{eval_case_id}`                 | 007-T11 |
| `GET`  | `/evals/{eval_case_id}/case.json`       | 007-T11 |
| `POST` | `/evals/{eval_case_id}/runs`            | 007-T11 |
| `GET`  | `/evals/{eval_case_id}/runs/{run_id}`   | 007-T11 |

`case.json` returns the **stored bytes**, not a re-serialisation. A case is
identified by its content hash, and a reader who downloads one must be able to
recompute that hash and get the same answer — which only holds if what they
receive is what was written.

Generation returns `created` so a caller can tell a fresh case from an identical
repeat (FR-080). Both are a 200: repeating the request is not an error, and a
409 would push a client into treating idempotence as a failure.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from actionwitness_core.evals.enums import EvalEnvironment
from fastapi import APIRouter, Body, Path, Response
from pydantic import BaseModel, ConfigDict

from actionwitness_service.api.dependencies import (
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.eval_case_service import EvalCaseService
from actionwitness_service.application.eval_run_service import EvalRunService

__all__ = ["router"]

router = APIRouter(tags=["evals"])

EvalCaseId = Annotated[str, Path(min_length=1, max_length=128)]
RunId = Annotated[str, Path(min_length=1, max_length=128)]


@router.post("/runs/{run_id}/evals", status_code=200)
async def create_regression_eval(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
) -> dict[str, Any]:
    """§15.4: "Generate a regression eval case from a failed or warning-bearing run".

    200 whether or not this call created it. FR-080 makes an identical repeat
    return the existing case, and answering a repeat with a conflict would teach
    a client to treat idempotence as an error.
    """
    async with locks.hold(workspace_id), database.transaction() as work:
        generated = await EvalCaseService(work, workspace_id, registry).generate(run_id)

    return {
        "eval_case_id": generated.case_id,
        "created": generated.created,
        "name": generated.case.name,
        "content_hash": generated.case.content_hash(),
        "source_run_id": generated.case.source.run_id,
        "expected": generated.case.expected.canonical_document(),
    }


@router.get("/evals")
async def list_eval_cases(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.4: "List eval cases in the current workspace"."""
    async with database.reading() as work:
        cases = await EvalCaseService(work, workspace_id).list_cases()
    return {
        "cases": [
            {
                "eval_case_id": str(case["id"]),
                "name": str(case["name"]),
                "source_run_id": str(case["source_run_id"]),
                "content_hash": str(case["content_hash"]),
                "schema_version": str(case["schema_version"]),
                "created_at": str(case["created_at"]),
            }
            for case in cases
        ]
    }


@router.get("/evals/{eval_case_id}")
async def read_eval_case(
    eval_case_id: EvalCaseId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.4: "Get eval metadata and latest result"."""
    async with database.reading() as work:
        case, row = await EvalCaseService(work, workspace_id).get(eval_case_id)
        latest = await work.fetch_one(
            "SELECT id, status, overall_result, environment_profile, started_at, completed_at "
            "FROM evaluation_runs WHERE evaluation_case_id = ? AND owner_workspace_id = ? "
            "ORDER BY started_at DESC, id DESC LIMIT 1",
            (eval_case_id, workspace_id),
        )

    return {
        "eval_case_id": eval_case_id,
        "name": case.name,
        "content_hash": case.content_hash(),
        "schema_version": case.schema_version,
        "source_run_id": case.source.run_id,
        "created_at": str(row["created_at"]),
        "expected": case.expected.canonical_document(),
        "non_replayable_policies": list(case.non_replayable_policies),
        # Named `latest_run` rather than `result`: a case is not a verdict, and
        # a field called `result` would invite a reader to treat the case's own
        # expectation as an outcome.
        "latest_run": (
            None
            if latest is None
            else {
                "eval_run_id": str(latest["id"]),
                "status": str(latest["status"]),
                "overall_result": latest["overall_result"],
                "environment": str(latest["environment_profile"]),
                "completed_at": latest["completed_at"],
            }
        ),
    }


@router.get("/evals/{eval_case_id}/case.json")
async def download_eval_case(
    eval_case_id: EvalCaseId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> Response:
    """§15.4: "Download the versioned built-in eval case".

    The stored bytes, verbatim. A case is identified by its content hash, and a
    reader who downloads one has to be able to recompute that hash and get the
    same answer — re-serialising here would produce a document that is equal but
    not identical, and the hash would stop being checkable by the person who
    most needs to check it.
    """
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT case_json FROM evaluation_cases WHERE id = ? AND workspace_id = ?",
            (eval_case_id, workspace_id),
        )
    if row is None:
        raise ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, "No such eval case.")
    return Response(
        content=str(row["case_json"]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{eval_case_id}.json"'},
    )


class RunEvalRequest(BaseModel):
    """§15.4's replay body. Unknown fields refused, not ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: §24.4: "`current` is always the default." A body that omitted it and got
    #: `reproduce_source` would report a reproduced failure as routine CI green.
    environment: EvalEnvironment = EvalEnvironment.CURRENT


_DEFAULT_RUN = RunEvalRequest()


@router.post("/evals/{eval_case_id}/runs", status_code=201)
async def run_regression_eval(
    eval_case_id: EvalCaseId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    registry: RegistryDependency,
    request: Annotated[RunEvalRequest, Body()] = _DEFAULT_RUN,
) -> dict[str, Any]:
    """§15.4: "Execute `replay` in an isolated eval workspace"."""
    from actionwitness_service.application.workspaces import WorkspaceStore

    async with database.reading() as work:
        case, _row = await EvalCaseService(work, workspace_id).get(eval_case_id)

    outcome = await EvalRunService(database, registry, WorkspaceStore(database)).run(
        case, owner_workspace_id=workspace_id, environment=request.environment
    )
    return _run_document(outcome.eval_run_id, outcome.report)


@router.get("/evals/{eval_case_id}/runs/{eval_run_id}")
async def read_eval_run(
    eval_case_id: EvalCaseId,
    eval_run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.4: "Get eval-run status and JSON report"."""
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT * FROM evaluation_runs WHERE id = ? AND evaluation_case_id = ? "
            "AND owner_workspace_id = ?",
            (eval_run_id, eval_case_id, workspace_id),
        )
    if row is None:
        raise ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, "No such eval run.")

    return {
        "eval_run_id": eval_run_id,
        "eval_case_id": eval_case_id,
        "status": str(row["status"]),
        "overall_result": row["overall_result"],
        "environment": str(row["environment_profile"]),
        "started_at": str(row["started_at"]),
        "completed_at": row["completed_at"],
        "report": json.loads(row["report_json"]) if row["report_json"] else None,
    }


def _run_document(eval_run_id: str, report: Any) -> dict[str, Any]:
    """What a caller learns from a completed replay.

    `status` and `overall_result` are both present and never merged: a
    reproduced failure is `status: passed` with `overall_result: failed`, and a
    response carrying one of them would be read as the other.
    """
    return {
        "eval_run_id": eval_run_id,
        "eval_case_id": report.eval_case_id,
        "status": report.status.value,
        "overall_result": None if report.overall_result is None else report.overall_result.value,
        "environment": report.environment.value,
        "classification_match": report.classification_match,
        "actual_classifications": [c.value for c in report.actual_classifications],
        "expected_classifications": [c.value for c in report.expected_classifications],
        "non_replayable_policies": list(report.non_replayable_policies),
        "detail": report.detail,
        "report": report.as_stored_document(),
    }
