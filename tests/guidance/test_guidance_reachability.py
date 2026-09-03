"""Guidance a person can actually be shown (FR-120, §11.5, §15.1).

`tests/guidance/test_guidance_derivation.py` proves the pure projection is total
and honest. This file proves the *server* reaches it, which is a different
claim: `derive_guidance` was already total over `WorkspacePhase` while
`eval_ready` and `eval_running` were unreachable from any real workspace, so
their copy had been passing a totality test for as long as nobody could see it.

Every phase here is arranged through the real endpoints — select, arm, invoke,
verify, generate, replay — because the question is whether the states a person
walks through produce the guidance they were written for. The two arrangements
that touch SQLite directly do so to reproduce a state the service reaches on a
path a test cannot trigger (a harness that abandons a run, a replay whose
process dies), and each says so where it happens.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.journeys.enums import (
    GuidanceActionCode,
    RunState,
    WorkspacePhase,
)
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.application.guidance_service import current_guidance
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.guidance, pytest.mark.asyncio]

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
EVALS = f"{API_PREFIX}/evals"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
FAULT = "discount_reported_but_not_applied"


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
    """A run the target fails on `false_success_or_state_mismatch`.

    The one journey §26.2 calls the product's own demonstration: the discount
    tool reports success and the observed cart total disagrees.
    """
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

    workspace_id = str((await visitor.get(WORKSPACE)).json()["workspace_id"])
    return workspace_id, run_id


async def _select_checkout_contract(visitor: httpx.AsyncClient) -> None:
    """The template whose policy protects `proceed_to_checkout`.

    Found by reading the published policies rather than by naming a template id,
    so the arrangement follows the contract that actually requires consent.
    """
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    for template in templates:
        document = (await visitor.get(f"{CONTRACTS}/{template['contract_id']}")).json()
        policies = document.get("document", document).get("policies") or []
        if any(policy.get("tool") == "proceed_to_checkout" for policy in policies):
            await visitor.post(f"{CONTRACTS}/{template['contract_id']}/select")
            return
    raise AssertionError("no template protects proceed_to_checkout")


async def _phase(database: Database, workspace_id: str) -> WorkspacePhase:
    async with database.reading() as work:
        return (await current_guidance(work, workspace_id)).phase


# --- the regression-eval lifecycle §11.5 draws ------------------------------


async def test_generating_a_case_moves_the_workspace_to_eval_ready(stack: FastAPI) -> None:
    """§11.5's `Failed --> EvalReady: eval created`.

    The behaviour the operator gets from this is the point: before the case
    exists the banner says "read the findings", and afterwards it says "replay
    the regression case" and names the panel that does it. The server could not
    emit `run_regression_eval` at all until this edge was projected.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
        assert await _phase(database, workspace_id) is WorkspacePhase.FAILED

        # Act
        generated = await visitor.post(f"{RUNS}/{run_id}/evals")
        assert generated.status_code == 200, generated.text

    # Assert
    phase = await _phase(database, workspace_id)
    assert phase is WorkspacePhase.EVAL_READY
    async with database.reading() as work:
        guidance = await current_guidance(work, workspace_id)
    assert guidance.action_code is GuidanceActionCode.RUN_REGRESSION_EVAL
    assert guidance.recovery_action_code is GuidanceActionCode.RESET_WORKSPACE


async def test_a_completed_replay_returns_the_workspace_to_eval_ready(
    stack: FastAPI,
) -> None:
    """§11.5's `EvalRunning --> EvalReady: replay completed`.

    A replay that reported an outcome must not leave the banner telling a person
    to wait for it.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
        case_id = str((await visitor.post(f"{RUNS}/{run_id}/evals")).json()["eval_case_id"])

        # Act
        replayed = await visitor.post(f"{EVALS}/{case_id}/runs")
        assert replayed.status_code == 201, replayed.text

    # Assert
    assert await _phase(database, workspace_id) is WorkspacePhase.EVAL_READY


async def test_a_replay_that_never_reported_keeps_the_workspace_waiting(
    stack: FastAPI,
) -> None:
    """§11.5's `EvalReady --> EvalRunning: replay started`, held open.

    Arranged by clearing `completed_at` on a replay row the service really
    wrote, because that is exactly what a process dying mid-replay leaves
    behind: `EvalRunService._open` inserts the row as `error` with no
    completion, so a crash cannot look like a pass. A synchronous replay never
    sits in this state long enough for a test to catch it in flight.

    The consequence is why `eval_running` carries a recovery: the banner will go
    on saying "replaying" until someone resets, so it has to say how.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
        case_id = str((await visitor.post(f"{RUNS}/{run_id}/evals")).json()["eval_case_id"])
        await visitor.post(f"{EVALS}/{case_id}/runs")

    async with database.transaction() as work:
        await work.execute(
            "UPDATE evaluation_runs SET completed_at = NULL WHERE evaluation_case_id = ?",
            (case_id,),
        )

    # Act
    async with database.reading() as work:
        guidance = await current_guidance(work, workspace_id)

    # Assert
    assert guidance.phase is WorkspacePhase.EVAL_RUNNING
    assert guidance.action_code is GuidanceActionCode.WAIT
    assert guidance.waiting_for
    assert guidance.recovery_action_code is GuidanceActionCode.RESET_WORKSPACE


async def test_another_runs_case_does_not_redirect_the_banner(stack: FastAPI) -> None:
    """A workspace can hold a case cut from an earlier run while a new one is
    armed. The reader is looking at the run in front of them, and guidance that
    pointed at an unrelated replay would be describing somebody else's work."""
    # Arrange — a failed run with a case, then a reset and a fresh run. The
    # reset reseeds the target and keeps the contract (FR-013); it does not
    # purge the generated case, which is exactly the situation under test.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
        await visitor.post(f"{RUNS}/{run_id}/evals")
        assert await _phase(database, workspace_id) is WorkspacePhase.EVAL_READY

        await visitor.post(f"{WORKSPACE}/reset")
        cases = (await visitor.get(EVALS)).json()["cases"]
        assert [case["source_run_id"] for case in cases] == [run_id]

        # Act
        armed = await visitor.post(RUNS)
        assert armed.status_code == 201, armed.text

    # Assert — the new run's own state, not the old run's case.
    assert await _phase(database, workspace_id) is WorkspacePhase.ARMED


# --- a run that ended without a verdict -------------------------------------


async def test_an_errored_run_is_not_greeted_as_a_fresh_workspace(stack: FastAPI) -> None:
    """The gap this closes, stated as behaviour.

    `error` used to project onto `contract_ready`, so a run the harness had
    abandoned mid-verification produced the banner "Arm the run." — no headline,
    reason, or consequence acknowledging that anything had happened. §22
    requires an observation failure to produce "an explicit non-pass result" that
    "never degrades to success", and the surface a person reads was degrading it.

    Arranged by writing the terminal state directly: §16 makes `verifying ->
    error` legal, and the paths that take it (`_abandon_unobservable`, the event
    ceiling in `limits.py`) both need a failure a test cannot ask the target for.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    async with database.transaction() as work:
        await work.execute(
            "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ?",
            (str(RunState.ERROR.value), run_id, workspace_id),
        )

    # Act
    async with database.reading() as work:
        guidance = await current_guidance(work, workspace_id)

    # Assert
    assert guidance.phase is WorkspacePhase.ERROR
    assert guidance.action_code is GuidanceActionCode.REVIEW_FINDINGS
    assert guidance.recovery_action_code is GuidanceActionCode.RESET_WORKSPACE
    assert guidance.headline != "Arm the run."
    assert guidance.correlation_id == run_id


async def test_resetting_after_an_error_returns_the_workspace_to_ready(
    stack: FastAPI,
) -> None:
    """The recovery the error phase names actually works.

    FR-013 retains the selected contract, so the way out is arming again rather
    than choosing a contract for a second time — and a recovery a person follows
    to a dead end would be worse than none.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
        async with database.transaction() as work:
            await work.execute(
                "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ?",
                (str(RunState.ERROR.value), run_id, workspace_id),
            )
        assert await _phase(database, workspace_id) is WorkspacePhase.ERROR

        # Act
        reset = await visitor.post(f"{WORKSPACE}/reset")

    # Assert
    assert reset.status_code == 200, reset.text
    assert reset.json()["selected_contract_id"]
    async with database.reading() as work:
        guidance = await current_guidance(work, workspace_id)
    assert guidance.phase is WorkspacePhase.CONTRACT_READY
    assert guidance.action_code is GuidanceActionCode.ARM_RUN


# --- cancellation, offered where the API can act on it ----------------------


async def test_the_pending_confirmation_phase_offers_the_cancel_endpoint(
    stack: FastAPI,
) -> None:
    """AC-21's safe recovery for the one blocking transition (§14.9).

    Asserted against the endpoint rather than only against the code: guidance
    that named a capability the API does not have would be the harness telling
    a person to do something it cannot do.
    """
    # Arrange — a protected mutation, paused on a human decision.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_checkout_contract(visitor)
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        added = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={
                "arguments": {
                    "product_id": MUG,
                    "quantity": 1,
                    "request_id": "req_onemug",
                }
            },
        )
        assert added.status_code == 200, added.text
        paused = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        assert paused.json()["status"] == "awaiting_confirmation", paused.text
        confirmation_id = str(paused.json()["confirmation"]["confirmation_id"])
        workspace_id = str((await visitor.get(WORKSPACE)).json()["workspace_id"])

        async with database.reading() as work:
            guidance = await current_guidance(work, workspace_id)

        # Assert — the phase names cancelling as its recovery...
        assert guidance.phase is WorkspacePhase.AWAITING_CONFIRMATION
        assert guidance.action_code is GuidanceActionCode.DECIDE_CONFIRMATION
        assert guidance.recovery_action_code is GuidanceActionCode.CANCEL_CONFIRMATION

        # Act — ...and the endpoint that recovery refers to accepts it.
        cancelled = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert — §14.9: cancelled, not denied, and nothing was changed.
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["mutated"] is False
