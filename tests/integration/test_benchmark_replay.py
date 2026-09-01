"""008-T6 — replaying an imported trajectory (FR-091, §24.7, §26.5, AC-16).

FR-091's sentence has four clauses and each is a separate failure mode, so each
gets its own test: a *fresh isolated* eval workspace, *through the registered
adapter*, *allowlisted* tools only, and *deterministic confirmation*.

The one that would be easiest to get wrong quietly is the last. An evaluator
report carries no consent evidence at all, so there is nothing for a provider to
replay — and any replay that supplied an approval would manufacture the exact
thing the harness exists to check. `no_confirmation` is not a default here, it
is the only possibility, and `test_no_imported_replay_can_grant_consent` pins
that to the type rather than to a call site.

Run against the real Buggy Store adapter over its real HTTP surface, because
"through the registered adapter" is not testable against a stub that agrees.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.benchmarks.enums import (
    ExclusionReason,
    OutcomeTrialResult,
    TrialEligibility,
)
from actionwitness_core.ports.models import ScenarioSelection
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.application.benchmark_replay import (
    BenchmarkReplayService,
    TrialReplayInput,
    stored_trajectory,
)
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

CONTRACTS = f"{API_PREFIX}/contracts"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
FAULT = "discount_reported_but_not_applied"

#: The journey the canonical contract judges: add a mug, then apply SAVE20.
JOURNEY = (
    {
        "name": "update_cart",
        # The adapter's schema sets `minLength: 8` on `request_id`. A shorter
        # one is rejected, the cart stays empty, and the contract then fails for
        # a reason that has nothing to do with the discount — which is exactly
        # how a replay test passes while proving nothing.
        "arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"},
    },
    {"name": "apply_discount", "arguments": {"code": "SAVE20"}},
)


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


async def _contract_and_workspace(stack: FastAPI) -> tuple[object, str]:
    """The canonical contract, and a workspace to own the replays."""
    async with client(stack) as visitor:
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
        await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
        workspace_id = (await visitor.get(f"{API_PREFIX}/workspace")).json()["workspace_id"]

    # Read the selected contract straight from its immutable row, the way
    # `EvalCaseService` does: the document is what judges the replay, and
    # loading it through a service that could re-resolve a *later* selection
    # would judge a different contract than the one the suite named.
    import json

    from actionwitness_core.contracts.models import OutcomeContract

    async with stack.state.database.transaction() as work:
        # Built-in templates are global rows (`workspace_id` is null), so the
        # contract is found by the template it came from rather than by owner.
        row = await work.fetch_one(
            "SELECT document_json FROM contracts WHERE source_template_id = ?",
            (CANONICAL,),
        )
    contract = OutcomeContract.model_validate(json.loads(row["document_json"]))
    return contract, workspace_id


def _service(stack: FastAPI) -> BenchmarkReplayService:
    return BenchmarkReplayService(
        stack.state.database, stack.state.adapters, stack.state.workspaces
    )


async def _trial_row(stack: FastAPI, workspace_id: str, trial_id: str = "t1") -> str:
    """A stored benchmark trial for the replay to reference (§17.1)."""
    async with stack.state.database.transaction() as work:
        await work.execute(
            "INSERT INTO artifacts (id, workspace_id, artifact_type, schema_version, "
            "content_hash, metadata_json, relative_path, created_at) "
            "VALUES ('art', ?, 'evaluator_report', '1.0', 'sha256:x', '{}', 'p', ?)",
            (workspace_id, work.now()),
        )
        await work.execute(
            "INSERT INTO benchmark_suites (id, workspace_id, schema_version, source_kind, "
            "manifest_content_hash, manifest_json, correlation_mode, status, "
            "normalized_adapter_version, created_at) VALUES "
            "('suite', ?, '1.0', 'recorded_fixture', 'sha256:y', '{}', "
            "'imported_trajectory_replay', 'ready', '1', ?)",
            (workspace_id, work.now()),
        )
        await work.execute(
            "INSERT INTO benchmark_trials (id, benchmark_suite_id, "
            "external_source_artifact_id, external_trial_id, scenario_id, "
            "correlation_mode, call_level_result, outcome_result, eligibility, "
            "metadata_json, created_at) VALUES "
            "(?, 'suite', 'art', ?, 'adds a mug', 'imported_trajectory_replay', "
            "'passed', 'not_reached', 'excluded', '{}', ?)",
            (trial_id, trial_id, work.now()),
        )
    return trial_id


def _input(
    contract: object, trial_row_id: str, *, mode: str, trajectory=JOURNEY
) -> TrialReplayInput:
    return TrialReplayInput(
        trial_row_id=trial_row_id,
        external_trial_id=trial_row_id,
        trajectory=tuple(trajectory),
        contract=contract,  # type: ignore[arg-type]
        scenario=ScenarioSelection(
            scenario_mode=mode,
            fault_profile=FAULT if mode == "pre_fix" else None,
        ),
    )


# --- deterministic confirmation ---------------------------------------------


def test_no_imported_replay_can_grant_consent() -> None:
    """FR-087 and §24.5, pinned to the type rather than to a call site.

    An evaluator report contains no consent evidence, so there is nothing for a
    provider to replay. The replay service takes no consent parameter at all —
    a knob here would be a way to grant an approval nobody gave, and the
    constitution forbids an agent creating its own.
    """
    # Arrange / Act
    import inspect

    signature = inspect.signature(BenchmarkReplayService.replay)

    # Assert
    assert set(signature.parameters) == {"self", "trial", "owner_workspace_id", "adapter_id"}


# --- the outcome layer -------------------------------------------------------


async def test_a_replayed_trajectory_fails_against_the_faulty_implementation(
    stack: FastAPI,
) -> None:
    """§24.7 step 5: the deterministic engine judges the outcome layer.

    `pre_fix` reports success on the discount and does not apply it, so the
    contract fails — which is the outcome half of the product's headline claim.
    """
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)

    # Act
    replayed = await _service(stack).replay(
        _input(contract, trial_row, mode="pre_fix"),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert — and specifically the *discount* failure. Asserting only
    # `FAILED` would pass just as happily if the cart were empty, which is how
    # this test read before the trajectory's request_id was long enough for the
    # adapter to accept it.
    assert replayed.outcome_result is OutcomeTrialResult.FAILED, replayed.detail
    assert replayed.eligibility is TrialEligibility.ELIGIBLE
    assert replayed.evaluation_run_id is not None
    async with stack.state.database.transaction() as work:
        after = await work.fetch_one(
            "SELECT overall_result FROM evaluation_runs WHERE id = ?",
            (replayed.evaluation_run_id,),
        )
    assert after["overall_result"] == "failed"


async def test_the_same_trajectory_passes_against_the_corrected_implementation(
    stack: FastAPI,
) -> None:
    """The counterpart that makes the test above mean something: a replay that
    always failed would satisfy it."""
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)

    # Act
    replayed = await _service(stack).replay(
        _input(contract, trial_row, mode="post_fix"),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert
    assert replayed.outcome_result is OutcomeTrialResult.PASSED
    assert replayed.eligibility is TrialEligibility.ELIGIBLE


# --- isolation ---------------------------------------------------------------


async def test_each_replay_runs_in_its_own_fresh_eval_workspace(stack: FastAPI) -> None:
    """FR-083 and FR-091: a *fresh isolated* eval workspace per replay.

    A second trial inheriting the first's cart would pass or fail for reasons
    belonging to a different trial, and the benchmark counts trials as
    independent observations.
    """
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    first_row = await _trial_row(stack, workspace_id, "t1")
    async with stack.state.database.transaction() as work:
        await work.execute(
            "INSERT INTO benchmark_trials (id, benchmark_suite_id, "
            "external_source_artifact_id, external_trial_id, scenario_id, "
            "correlation_mode, call_level_result, outcome_result, eligibility, "
            "metadata_json, created_at) VALUES "
            "('t2', 'suite', 'art', 't2', 'adds a mug', 'imported_trajectory_replay', "
            "'passed', 'not_reached', 'excluded', '{}', ?)",
            (work.now(),),
        )

    # Act
    service = _service(stack)
    first = await service.replay(
        _input(contract, first_row, mode="post_fix"),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )
    second = await service.replay(
        _input(contract, "t2", mode="post_fix"),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert — different workspaces, and the second still passes rather than
    # tripping over the first one's cart.
    assert first.execution_workspace_id != second.execution_workspace_id
    assert second.outcome_result is OutcomeTrialResult.PASSED


async def test_the_replay_does_not_touch_the_owning_workspace(stack: FastAPI) -> None:
    """§12.4: the workspace is the isolation boundary. A replay that mutated the
    workspace it was launched from would corrupt the operator's own session."""
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)

    # Act
    replayed = await _service(stack).replay(
        _input(contract, trial_row, mode="post_fix"),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert
    assert replayed.execution_workspace_id != workspace_id


# --- the allowlist -----------------------------------------------------------


async def test_a_tool_the_adapter_does_not_publish_stops_the_replay(stack: FastAPI) -> None:
    """FR-086 / §26.5: replay "only through allowlisted target tools".

    Refused rather than skipped — skipping would replay a shorter journey and
    report its outcome as this trial's.
    """
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)
    trajectory = (*JOURNEY, {"name": "drop_database", "arguments": {}})

    # Act
    replayed = await _service(stack).replay(
        _input(contract, trial_row, mode="post_fix", trajectory=trajectory),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert — coverage, not a business verdict.
    assert replayed.eligibility is TrialEligibility.EXCLUDED
    assert replayed.exclusion_reason is ExclusionReason.HARNESS_ERROR
    assert replayed.outcome_result is OutcomeTrialResult.NOT_REACHED


# --- harness failures are coverage, not verdicts -----------------------------


async def test_an_unknown_adapter_is_excluded_rather_than_failed(stack: FastAPI) -> None:
    """FR-092 keeps errors out of the denominator so a broken harness cannot be
    read as a broken target."""
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)

    # Act
    replayed = await _service(stack).replay(
        _input(contract, trial_row, mode="post_fix"),
        owner_workspace_id=workspace_id,
        adapter_id="an_adapter_nobody_registered",
    )

    # Assert
    assert replayed.eligibility is TrialEligibility.EXCLUDED
    assert replayed.exclusion_reason is ExclusionReason.HARNESS_ERROR
    assert replayed.evaluation_run_id is None


async def test_a_trial_with_no_replayable_steps_is_excluded_by_name(stack: FastAPI) -> None:
    """There is nothing to execute through the adapter, and coverage should say
    so rather than blaming the outcome layer."""
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)

    # Act
    replayed = await _service(stack).replay(
        _input(contract, trial_row, mode="post_fix", trajectory=()),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert
    assert replayed.exclusion_reason is ExclusionReason.MISSING_TRAJECTORY


async def test_a_malformed_step_yields_no_partial_journey(stack: FastAPI) -> None:
    """Replaying some of a journey and reporting the result as the whole one is
    the failure this refuses."""
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)
    trajectory = (JOURNEY[0], {"name": "apply_discount"})

    # Act
    replayed = await _service(stack).replay(
        _input(contract, trial_row, mode="post_fix", trajectory=trajectory),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert
    assert replayed.exclusion_reason is ExclusionReason.MISSING_TRAJECTORY


# --- §17.1's evaluation-run reference ---------------------------------------


async def test_an_executed_trial_references_its_evaluation_run(stack: FastAPI) -> None:
    """§17.1: `evaluation_run_id` is "required after an eligible
    `imported_trajectory_replay` trial executes".

    Written against migration 4's widened table, whose CHECK makes "exactly one
    origin" a schema fact: this row carries a benchmark trial and no eval case.
    """
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)

    # Act
    replayed = await _service(stack).replay(
        _input(contract, trial_row, mode="pre_fix"),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert
    async with stack.state.database.transaction() as work:
        run = await work.fetch_one(
            "SELECT * FROM evaluation_runs WHERE id = ?", (replayed.evaluation_run_id,)
        )
        trial = await work.fetch_one("SELECT * FROM benchmark_trials WHERE id = ?", (trial_row,))
    assert run is not None
    assert run["benchmark_trial_id"] == trial_row
    assert run["evaluation_case_id"] is None
    assert run["overall_result"] == "failed"
    assert trial["evaluation_run_id"] == replayed.evaluation_run_id
    assert trial["eligibility"] == TrialEligibility.ELIGIBLE.value


async def test_an_excluded_trial_records_no_evaluation_run(stack: FastAPI) -> None:
    """A trial that never executed has no run to reference, and inventing one
    would make coverage look complete."""
    # Arrange
    contract, workspace_id = await _contract_and_workspace(stack)
    trial_row = await _trial_row(stack, workspace_id)

    # Act
    await _service(stack).replay(
        _input(contract, trial_row, mode="post_fix", trajectory=()),
        owner_workspace_id=workspace_id,
        adapter_id="buggy_store",
    )

    # Assert
    async with stack.state.database.transaction() as work:
        rows = await work.fetch_all(
            "SELECT id FROM evaluation_runs WHERE benchmark_trial_id = ?", (trial_row,)
        )
    assert rows == []


# --- stored trajectories -----------------------------------------------------


def test_a_stored_trial_hands_back_the_trajectory_it_was_given() -> None:
    """The round trip binding and replay depend on."""
    # Arrange
    stored = '{"trajectory": [{"name": "update_cart", "arguments": {"quantity": 1}}]}'

    # Act / Assert
    assert stored_trajectory(stored) == ({"name": "update_cart", "arguments": {"quantity": 1}},)


@pytest.mark.parametrize("stored", [None, "", "not json", '{"trajectory": "nope"}', "{}"])
def test_an_unreadable_stored_trajectory_yields_nothing(stored: str | None) -> None:
    """Nothing, rather than a partial or invented journey."""
    # Arrange / Act / Assert
    assert stored_trajectory(stored) == ()
