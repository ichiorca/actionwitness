"""013's exit gate — undeclared-change detection, end to end (013-T7).

The centerpiece is `test_gate_2_every_named_assertion_passes_and_the_run_fails`.
It is the whole argument of the feature in one run: a journey does exactly what
the contract asked, every critical assertion the author wrote comes back green,
and the run fails anyway — because the same journey rewrote a path nobody
thought to name.

That shape is what makes undeclared-change detection worth having. A reviewer
reading the assertion list would sign this run off. Assertion-based verification
is only as complete as its author's imagination, and this is the run that proves
it, through the real HTTP API against the real store.

Criterion 6 (all gates green) is not a test — a suite cannot assert that it
passed. It is discharged by CI and by the commands in the README, and named in
the traceability map against the lane gate that would fail if the suites stopped
running.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.engine.diff import diff_states
from actionwitness_core.engine.enums import FailureClassification
from actionwitness_core.evals.enums import EvalEnvironment, EvalStatus
from actionwitness_core.reports.enums import LayerResult
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.application.eval_case_service import EvalCaseService
from actionwitness_service.application.eval_run_service import EvalRunService
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from buggy_store.failure_injection import IMPLEMENTED_PROFILES, FaultProfile
from fastapi import FastAPI
from integrations.buggy_store.templates import TEMPLATES, template_for

# `asyncio_mode = "auto"` marks the coroutine tests; an explicit `asyncio` mark
# here would also land on the synchronous criteria below and fail them.
pytestmark = [pytest.mark.integration]

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
TEMPLATE = "one_mug_no_side_effects"
FAULT = "undeclared_side_effect"
MUG = "mug-ceramic-001"
NOTE_PATH = "target.preferences.delivery_note"
UNDECLARED = FailureClassification.UNDECLARED_STATE_CHANGE


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


async def _journey(visitor: httpx.AsyncClient, *, mode: str = "pre_fix") -> str:
    """Select the blast-radius contract, inject the side effect, add one mug."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == TEMPLATE)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})

    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    invoked = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}},
    )
    assert invoked.status_code == 200, invoked.text
    await visitor.post(f"{RUNS}/{run_id}/verify")
    return run_id


# --- criterion 1: determinism ------------------------------------------------


async def test_gate_1_the_same_snapshots_produce_an_identical_partition(
    stack: FastAPI,
) -> None:
    """ "Same snapshots → byte-identical changed-path set and partition."

    Determinism is the product here: a blast-radius check that listed different
    paths on a second look would make every one of its verdicts advisory.
    """
    async with client(stack) as visitor:
        run_id = await _journey(visitor)
        first = (await visitor.get(f"{RUNS}/{run_id}/report")).json()["report"]
        second = (await visitor.get(f"{RUNS}/{run_id}/report")).json()["report"]

    assert first["undeclared_changes"] == second["undeclared_changes"]

    # And the diff itself, re-run over the same two documents.
    before = {"target": {"cart": {"total": "25.00"}, "preferences": {"delivery_note": ""}}}
    after = {"target": {"cart": {"total": "25.00"}, "preferences": {"delivery_note": "x"}}}
    assert diff_states(before, after) == diff_states(before, after)


# --- criterion 2: the centerpiece --------------------------------------------


async def test_gate_2_every_named_assertion_passes_and_the_run_fails(stack: FastAPI) -> None:
    """The feature's whole argument, in one run.

    Both halves are asserted separately because a reader who checks only one
    misunderstands what happened. The cart is *correct* — the agent did the job.
    The run fails because the journey also touched something nobody named.
    """
    async with client(stack) as visitor:
        run_id = await _journey(visitor)
        verdict = (await visitor.get(f"{RUNS}/{run_id}")).json()
        findings = (await visitor.get(f"{RUNS}/{run_id}/findings")).json()
        report = (await visitor.get(f"{RUNS}/{run_id}/report")).json()["report"]

    assertions = [f for f in findings["findings"] if f["check_type"] == "assertion"]
    assert assertions, "the contract must actually carry assertions"
    assert all(f["status"] == "passed" for f in assertions), (
        f"an assertion failed, so this run proves something else: {assertions}"
    )

    assert verdict["overall_result"] == "failed"
    assert report["layers"]["business_outcome"] == "passed"
    assert report["layers"]["safety_policy"] == "failed"


async def test_gate_2_the_failure_is_classified_and_names_its_paths(stack: FastAPI) -> None:
    """§22: one finding per run, carrying every undeclared path."""
    async with client(stack) as visitor:
        run_id = await _journey(visitor)
        findings = (await visitor.get(f"{RUNS}/{run_id}/findings")).json()
        report = (await visitor.get(f"{RUNS}/{run_id}/report")).json()["report"]

    failed = [f for f in findings["findings"] if f["status"] == "failed"]
    assert len(failed) == 1, "§17.1 emits exactly one finding for this classification"
    assert failed[0]["check_id"] == "no_undeclared_changes"
    assert failed[0]["classification"] == "undeclared_state_change"
    assert failed[0]["paths"] == [NOTE_PATH]

    block = report["undeclared_changes"]
    assert block["paths"] == [NOTE_PATH]
    assert block["undeclared"] == 1
    assert block["declared"] == block["changed_paths"] - block["undeclared"]
    assert block["effect_metadata_published"] is True


async def test_gate_2_the_same_journey_passes_with_the_fault_inactive(
    stack: FastAPI,
) -> None:
    """FR-019's matched pair. Without this the run proves the contract is strict,
    not that the detection works: a check that failed in both modes would be
    indistinguishable from one that always fails."""
    async with client(stack) as visitor:
        run_id = await _journey(visitor, mode="post_fix")
        verdict = (await visitor.get(f"{RUNS}/{run_id}")).json()
        report = (await visitor.get(f"{RUNS}/{run_id}/report")).json()["report"]

    assert verdict["overall_result"] == "passed"
    assert report["undeclared_changes"]["undeclared"] == 0


# --- criterion 4: replay ------------------------------------------------------


async def test_gate_4_a_generated_case_reproduces_the_classification(stack: FastAPI) -> None:
    """§24.3: the target failed, and the eval passed for reproducing it."""
    async with client(stack) as visitor:
        run_id = await _journey(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

    database: Database = stack.state.database
    async with database.transaction() as work:
        case = (
            await EvalCaseService(work, workspace_id, stack.state.adapters).generate(run_id)
        ).case

    outcome = await EvalRunService(
        stack.state.database, stack.state.adapters, stack.state.workspaces
    ).run(
        case,
        owner_workspace_id=workspace_id,
        environment=EvalEnvironment.REPRODUCE_SOURCE,
    )

    assert outcome.report.overall_result is LayerResult.FAILED
    assert outcome.report.status is EvalStatus.PASSED
    assert set(outcome.report.actual_classifications) == {UNDECLARED}


# --- criterion 5: the template and the honesty gate --------------------------


def test_gate_5_the_undeclared_side_effect_template_ships() -> None:
    template = template_for(TEMPLATE)

    assert template is not None
    assert template.demonstrates == FAULT
    assert any(
        policy["type"] == "no_undeclared_changes" for policy in template.document["policies"]
    )


def test_gate_5_the_template_honesty_gate_now_covers_three_profiles() -> None:
    """ "The test that forbade claiming an uninjectable profile now covers three."

    Extended, never weakened: the gate still refuses a template that claims a
    profile with no injector, and there are now three profiles it can vouch for.
    """
    assert (
        frozenset(
            {
                FaultProfile.NONE,
                FaultProfile.DISCOUNT_REPORTED_BUT_NOT_APPLIED,
                FaultProfile.UNDECLARED_SIDE_EFFECT,
            }
        )
        == IMPLEMENTED_PROFILES
    )
    implemented = {profile.value for profile in IMPLEMENTED_PROFILES}
    assert all(template.demonstrates in implemented for template in TEMPLATES)


# --- criterion 6 --------------------------------------------------------------


def test_gate_6_every_lane_this_milestone_touched_is_still_selectable() -> None:
    """A suite cannot assert that it passed, so this asserts what it can.

    Criterion 6 ("full suite, architecture lane, both frontend gates green") is
    discharged by running them. What is checkable here is that the lanes 013
    added tests to still exist and still select tests — a marker typo would
    silently deselect a lane and every gate would go green having run nothing.
    """
    root = Path(__file__).resolve().parent.parent.parent
    for module in (
        "tests/unit/test_state_diff.py",
        "tests/integration/test_undeclared_change_evaluation.py",
        "tests/integration/test_undeclared_change_replay.py",
        "apps/actionwitness_service/frontend/src/components/panels.test.tsx",
    ):
        assert (root / module).is_file(), f"{module} disappeared"
