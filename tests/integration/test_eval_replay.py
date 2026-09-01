"""007-T5–T9 — replaying a case and judging the result (§24.3–24.5, AC-12, AC-15).

The milestone's headline claim lives here: a case cut from a failure reproduces
that failure on demand, and passes against the corrected implementation. Both
halves are needed — a runner that always reported a match would satisfy the
first, and one that never did would satisfy neither.

`test_reproduce_source_recreates_the_failure_and_the_eval_passes` is the
sentence §24.3 insists on: the *target* failed, and the *eval* passed, because
reproducing the failure is what the case asked for. The two fields are asserted
separately on purpose; a reader who checks only one will misunderstand the run.

`test_an_unrelated_critical_failure_does_not_count_as_reproduction` is the
counterpart that makes set equality mean something.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.engine.enums import FailureClassification
from actionwitness_core.evals.enums import ConfirmationStrategy, EvalEnvironment, EvalStatus
from actionwitness_core.reports.enums import LayerResult
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.application.eval_case_service import EvalCaseService
from actionwitness_service.application.eval_run_service import EvalRunService
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
FAULT = "discount_reported_but_not_applied"
MISMATCH = FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH


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


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _failed_run(visitor: httpx.AsyncClient) -> tuple[str, str]:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    for tool, arguments in (
        ("search_catalog", {"query": "mug"}),
        ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
        ("apply_discount", {"code": "SAVE20"}),
    ):
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
    assert verdict.json()["overall_result"] == "failed", verdict.text
    return (await visitor.get(WORKSPACE)).json()["workspace_id"], run_id


async def _case(stack: FastAPI, workspace_id: str, run_id: str):
    database: Database = stack.state.database
    async with database.transaction() as work:
        return (
            await EvalCaseService(work, workspace_id, stack.state.adapters).generate(run_id)
        ).case


def _service(stack: FastAPI) -> EvalRunService:
    return EvalRunService(stack.state.database, stack.state.adapters, stack.state.workspaces)


# --- the headline claim ------------------------------------------------------


async def test_reproduce_source_recreates_the_failure_and_the_eval_passes(
    stack: FastAPI,
) -> None:
    """§24.3's sentence, made executable.

    The *target* failed and the *eval* passed. Reproducing the recorded failure
    is exactly what the case asked for, and the two fields are asserted
    separately because a reader who checks only one misunderstands the run.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)

    # Act
    outcome = await _service(stack).run(
        case,
        owner_workspace_id=workspace_id,
        environment=EvalEnvironment.REPRODUCE_SOURCE,
    )

    # Assert
    assert outcome.report.overall_result is LayerResult.FAILED
    assert outcome.report.status is EvalStatus.PASSED
    assert set(outcome.report.actual_classifications) == {MISMATCH}
    assert outcome.report.classification_match is True


async def test_current_passes_against_the_corrected_implementation(
    stack: FastAPI,
) -> None:
    """§24.4: `current` maps to the corrected behaviour with no injected fault.

    The other half of the pair. Without it, a runner that reported everything as
    a reproduction would satisfy the test above.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)

    # Act
    outcome = await _service(stack).run(
        case, owner_workspace_id=workspace_id, environment=EvalEnvironment.CURRENT
    )

    # Assert
    assert outcome.report.overall_result is LayerResult.PASSED
    assert outcome.report.status is EvalStatus.PASSED
    assert outcome.report.actual_classifications == ()


async def test_an_unrelated_critical_failure_does_not_count_as_reproduction(
    stack: FastAPI,
) -> None:
    """Set equality, through the runner rather than the model.

    A different failure is not the failure the case was cut from, and a suite
    that accepted it would let a new regression ride inside a passing eval.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)

    def unrelated(_case, _outcome):
        return LayerResult.FAILED, (FailureClassification.MISSING_CONFIRMATION,)

    # Act
    outcome = await _service(stack).run(
        case,
        owner_workspace_id=workspace_id,
        environment=EvalEnvironment.REPRODUCE_SOURCE,
        evaluate=unrelated,
    )

    # Assert
    assert outcome.report.overall_result is LayerResult.FAILED
    assert outcome.report.status is EvalStatus.FAILED
    assert outcome.report.classification_match is False


async def test_an_additional_classification_also_fails(stack: FastAPI) -> None:
    """The superset case, which containment would wrongly accept."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)

    def extra(_case, _outcome):
        return LayerResult.FAILED, (MISMATCH, FailureClassification.MISSING_CONFIRMATION)

    # Act
    outcome = await _service(stack).run(
        case,
        owner_workspace_id=workspace_id,
        environment=EvalEnvironment.REPRODUCE_SOURCE,
        evaluate=extra,
    )

    # Assert
    assert outcome.report.status is EvalStatus.FAILED


# --- isolation ---------------------------------------------------------------


async def test_the_replay_runs_in_its_own_workspace(stack: FastAPI) -> None:
    """FR-083: "every replay shall create a new eval workspace ... and leave the
    interactive workspace and source run unchanged".

    Checked on the source run's own row: a replay that reached into the
    interactive workspace would let a CI job mutate somebody's live demo.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)
    async with database.reading() as work:
        before = dict((await work.fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))) or {})

    # Act
    outcome = await _service(stack).run(case, owner_workspace_id=workspace_id)

    # Assert
    async with database.reading() as work:
        after = dict((await work.fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))) or {})
        run_row = await work.fetch_one(
            "SELECT owner_workspace_id, execution_workspace_id FROM evaluation_runs WHERE id = ?",
            (outcome.eval_run_id,),
        )
    assert after == before, "the source run was modified by its own replay"
    assert run_row is not None
    assert run_row["execution_workspace_id"] != run_row["owner_workspace_id"]


async def test_the_eval_workspace_is_cleaned_after_the_report_persists(
    stack: FastAPI,
) -> None:
    """The order matters: the report is what explains the run, and an unswept
    workspace is a smaller problem than a result nobody can account for."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)

    # Act
    outcome = await _service(stack).run(case, owner_workspace_id=workspace_id)

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT report_json, execution_workspace_id FROM evaluation_runs WHERE id = ?",
            (outcome.eval_run_id,),
        )
        assert row is not None and row["report_json"], "the report must outlive the workspace"
        workspace = await work.fetch_one(
            "SELECT cleaned_at FROM workspaces WHERE id = ?", (row["execution_workspace_id"],)
        )
    # The row stays and is marked cleaned: `evaluation_runs` references it
    # (§17.1), so deleting it would orphan the run's account of where it ran.
    # FR-009 removes the *mutable* state, not the record.
    assert workspace is not None
    assert workspace["cleaned_at"] is not None, "the mutable eval state was not purged"


# --- honest failure modes ----------------------------------------------------


async def test_an_unknown_adapter_is_an_error_not_a_failure(stack: FastAPI) -> None:
    """§9.8 and FR-088: `error` says nothing about the target.

    A missing adapter means nothing was learned, and reporting it as a failure
    would tell CI the code broke when the case was simply unrunnable here.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)
    stranded = case.model_copy(
        update={"target": case.target.model_copy(update={"adapter": "integrations.absent"})}
    )

    # Act
    outcome = await _service(stack).run(stranded, owner_workspace_id=workspace_id)

    # Assert
    assert outcome.report.status is EvalStatus.ERROR
    assert outcome.report.overall_result is None
    assert "no registered adapter" in outcome.report.detail


async def test_a_tool_outside_the_allowlist_stops_the_run(stack: FastAPI) -> None:
    """FR-086. Refused rather than skipped: skipping would replay a different
    journey and report its outcome as this case's."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)
    first = case.trajectory[0].model_copy(update={"tool": "drop_database"})
    tampered = case.model_copy(update={"trajectory": (first, *case.trajectory[1:])})

    # Act
    outcome = await _service(stack).run(tampered, owner_workspace_id=workspace_id)

    # Assert
    assert outcome.report.status is EvalStatus.ERROR
    assert "does not publish" in outcome.report.detail


# --- consent (§24.5, FR-087) -------------------------------------------------


async def test_a_generated_case_supplies_no_consent_it_did_not_record(
    stack: FastAPI,
) -> None:
    """FR-087, at the point a case is cut.

    This run never obtained an approval, so the case replays with
    `no_confirmation` — which is not a fallback but the correct reproduction:
    correct behaviour then blocks the mutation.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act
    case = await _case(stack, workspace_id, run_id)

    # Assert
    assert case.replay.confirmation_strategy is ConfirmationStrategy.NO_CONFIRMATION
    assert case.replay.recorded_decisions == ()
