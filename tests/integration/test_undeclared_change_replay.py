"""013-T4 — an undeclared change survives being turned into a regression case.

Exit-gate criterion 4: "a generated eval case reproduces the same classification
in replay."

The failure this guards against is subtle and would be read as a real
regression. §24.1 compares a replay against its source by **set equality** over
critical classifications, so a replay that partitioned the same snapshots even
slightly differently reports a classification the original run never produced —
and the eval fails while the target behaved exactly as recorded. Someone would
then go looking for a bug in the store.

Two things make the partition reproducible, and both are asserted here rather
than assumed:

* 007's minimizer keeps **complete** canonical state for a case carrying
  `no_undeclared_changes`, because that policy is defined over paths the contract
  never names — a minimized fixture would delete the very paths the diff has to
  see.
* Verification and replay derive declared paths and changed paths from the same
  core functions. Two implementations of "what did the contract declare" is
  exactly how the sets drift apart.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.engine.enums import FailureClassification
from actionwitness_core.evals.enums import EvalEnvironment, EvalStatus
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
TEMPLATE = "one_mug_no_side_effects"
FAULT = "undeclared_side_effect"
MUG = "mug-ceramic-001"
UNDECLARED = FailureClassification.UNDECLARED_STATE_CHANGE
NOTE_PATH = "target.preferences.delivery_note"


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


async def _run_with_a_side_effect(visitor: httpx.AsyncClient) -> tuple[str, str]:
    """The journey the demonstration rests on: correct cart, undeclared write."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == TEMPLATE)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})

    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    invoked = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}},
    )
    assert invoked.status_code == 200, invoked.text

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


# --- the source run ----------------------------------------------------------


async def test_the_source_run_fails_on_the_side_effect_alone(stack: FastAPI) -> None:
    """The premise of everything below. Asserted, not assumed.

    If the cart assertions were failing too, the case would carry two
    classifications and the replay comparison would prove something else.
    """
    async with client(stack) as visitor:
        workspace_id, run_id = await _run_with_a_side_effect(visitor)
        envelope = (await visitor.get(f"{RUNS}/{run_id}/report")).json()

    report = envelope["report"]
    layers = report["layers"]
    assert layers["business_outcome"] == "passed", "every named assertion must pass"
    assert layers["safety_policy"] == "failed"
    # §23.1 renders each entry as `{path, before, after, attributed_cause}`, so
    # the path is read out of the entry rather than compared to it.
    assert [entry["path"] for entry in report["undeclared_changes"]["paths"]] == [NOTE_PATH]
    assert workspace_id


# --- replay parity (criterion 4) ---------------------------------------------


async def test_a_generated_case_reproduces_the_same_classification(stack: FastAPI) -> None:
    """§24.3: the target failed, and the eval passed for reproducing it."""
    async with client(stack) as visitor:
        workspace_id, run_id = await _run_with_a_side_effect(visitor)
    case = await _case(stack, workspace_id, run_id)

    outcome = await _service(stack).run(
        case,
        owner_workspace_id=workspace_id,
        environment=EvalEnvironment.REPRODUCE_SOURCE,
    )

    assert outcome.report.overall_result is LayerResult.FAILED
    assert outcome.report.status is EvalStatus.PASSED
    assert set(outcome.report.actual_classifications) == {UNDECLARED}
    assert outcome.report.classification_match is True


async def test_the_policy_is_evaluated_in_replay_rather_than_skipped(
    stack: FastAPI,
) -> None:
    """The regression this task exists to prevent.

    Before 013-T4 the replay path supplied no diff, so `no_undeclared_changes`
    came back `not_evaluated` — and §24.3a excludes an unevaluated policy from
    both classification sets. The eval would have passed while checking nothing,
    which is the one outcome worse than failing.
    """
    async with client(stack) as visitor:
        workspace_id, run_id = await _run_with_a_side_effect(visitor)
    case = await _case(stack, workspace_id, run_id)

    outcome = await _service(stack).run(
        case,
        owner_workspace_id=workspace_id,
        environment=EvalEnvironment.REPRODUCE_SOURCE,
    )

    assert "no_undeclared_changes" not in set(outcome.report.non_replayable_policies)
    assert UNDECLARED in set(outcome.report.actual_classifications)


async def test_the_minimizer_kept_the_state_the_diff_needs(stack: FastAPI) -> None:
    """007 already special-cases this policy; 013 is what makes that matter.

    A fixture minimized to the contract's own paths would drop
    `target.preferences` entirely, the replayed diff would find nothing there,
    and the case would silently stop reproducing its own failure.
    """
    async with client(stack) as visitor:
        workspace_id, run_id = await _run_with_a_side_effect(visitor)
    case = await _case(stack, workspace_id, run_id)

    assert case.fixture.complete is True, (
        "`complete` is how the runner tells 'small because nothing else mattered' "
        "from 'small because somebody trimmed it wrongly'"
    )
    assert "preferences" in case.fixture.target_state, (
        "the minimizer dropped the path the policy is defined over"
    )


async def test_two_replays_of_one_case_agree(stack: FastAPI) -> None:
    """Exit-gate criterion 1, at the replay level.

    Determinism is the product: a case that classified differently on a second
    run would make every regression result advisory.
    """
    async with client(stack) as visitor:
        workspace_id, run_id = await _run_with_a_side_effect(visitor)
    case = await _case(stack, workspace_id, run_id)
    service = _service(stack)

    first = await service.run(
        case, owner_workspace_id=workspace_id, environment=EvalEnvironment.REPRODUCE_SOURCE
    )
    second = await service.run(
        case, owner_workspace_id=workspace_id, environment=EvalEnvironment.REPRODUCE_SOURCE
    )

    assert set(first.report.actual_classifications) == set(second.report.actual_classifications)
    assert first.report.status is second.report.status
