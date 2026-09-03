"""Repeated trials of one frozen variant, and the correlation they produce.

Spec v1.9 §26.5 ("six intent variants with five repeated trials each"), FR-100
(the variants are frozen before trials begin and generation is not rerun between
repetitions), FR-092 (what counts and what stays out of the denominator),
§9.9 (the dual-layer matrix), constitution §5 (a partially completed operation
stays visible rather than being silently retried).

A benchmark that sampled each variant once could report *that* the observed
state disagreed with the evaluator, but never *how often* — and how often is the
only form in which a non-deterministic agent's behaviour can be characterised.
These tests drive the repetition route end to end against the real Buggy Store
adapter over its real HTTP surface, because "each repetition in its own fresh
eval workspace, through the registered adapter" is not testable against a stub
that agrees.

Four properties carry the batch, and each is a different way of losing a run:

- **the ceiling is the server's**, not the caller's. A request for more than the
  ceiling is refused, never truncated — truncation would quietly report a
  smaller population than the one the caller believes they measured.
- **a cancelled batch keeps what it started.** The repetitions already recorded
  stay visible as excluded; the ones not reached were never invented; and
  nothing is retried.
- **a failed repetition is coverage, not a verdict.** It stays in the suite with
  a reason, and the batch keeps going.
- **the correlation view names the disagreement.** Three repetitions of a
  call-level pass against a faulty implementation is a silent-failure rate of
  1.0000, and it must read that way rather than as three unrelated trials.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_core.benchmarks.enums import (
    ExclusionReason,
    OutcomeTrialResult,
    TrialEligibility,
)
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.application.benchmark_replay import (
    RepeatedTrialService,
    ReplayedTrial,
)
from actionwitness_service.application.benchmark_service import (
    MAX_TRIAL_REPETITIONS,
    BenchmarkService,
)
from buggy_store.api import create_app as create_store
from fastapi import FastAPI
from integrations.google_evals.pins import REPORTER_SCHEMA, REPORTER_VERSION

pytestmark = pytest.mark.integration

BENCHMARKS = f"{API_PREFIX}/benchmarks"
CONTRACTS = f"{API_PREFIX}/contracts"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
SCENARIO = "adds a mug"
FAULT = "discount_reported_but_not_applied"

#: The journey the canonical contract judges. `request_id` is long enough for
#: the adapter's own schema, so a failure here is the discount's and not the
#: cart's.
JOURNEY = [
    {
        "name": "update_cart",
        "arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"},
    },
    {"name": "apply_discount", "arguments": {"code": "SAVE20"}},
]

VARIANT_TEXT = "Please add a ceramic mug to my cart and use the SAVE20 code."
CANONICAL_INTENT = "Add one ceramic mug to the cart and apply the SAVE20 discount."


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


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


def _report(*trials: dict[str, Any]) -> bytes:
    document = {
        "config": {"reporterSchema": REPORTER_SCHEMA, "evaluatorVersion": REPORTER_VERSION},
        "results": {
            "results": list(trials),
            "testCount": len(trials),
            "passCount": len(trials),
            "failCount": 0,
            "errorCount": 0,
        },
    }
    return json.dumps(document).encode("utf-8")


def _trial(outcome: str = "pass", *, trajectory: list[dict[str, Any]] | None = None) -> dict:
    return {
        "test": {"name": SCENARIO},
        "outcome": outcome,
        "runIndex": 0,
        "trajectory": JOURNEY if trajectory is None else trajectory,
    }


async def _select_contract(visitor: httpx.AsyncClient) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")


async def _imported_suite(
    visitor: httpx.AsyncClient,
    *,
    scenario_mode: str = "pre_fix",
    failure_profile: str | None = FAULT,
    trial: dict[str, Any] | None = None,
) -> str:
    """A draft suite holding one imported call-level pass with a trajectory.

    The scenario declares the target configuration, because §24.7 step 1 puts it
    in the benchmark rather than in the evaluator report — a repetition run
    against the target's default would measure the corrected implementation and
    report no silent defect because none was provoked.
    """
    await _select_contract(visitor)
    created = await visitor.post(
        BENCHMARKS,
        json={
            "correlation_mode": "imported_trajectory_replay",
            "scenarios": [
                {
                    "scenario_id": SCENARIO,
                    "scenario_mode": scenario_mode,
                    "failure_profile": failure_profile,
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    benchmark_id = created.json()["benchmark_id"]
    imported = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(_trial() if trial is None else trial),
    )
    assert imported.status_code == 201, imported.text
    return benchmark_id


async def _repeat(
    visitor: httpx.AsyncClient,
    benchmark_id: str,
    *,
    trials: int,
    variant_index: int | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"source_external_trial_id": f"{SCENARIO}#0", "trials": trials}
    if variant_index is not None:
        body["variant_index"] = variant_index
    return await visitor.post(f"{BENCHMARKS}/{benchmark_id}/repeated-trials", json=body)


# --- the happy path ----------------------------------------------------------


async def test_one_variant_runs_n_times_and_each_repetition_is_its_own_trial(
    visitor: httpx.AsyncClient,
) -> None:
    """§26.5: repeated trials of the same intent, recorded individually.

    Three repetitions produce three trials, three evaluation runs, and three
    distinct identifiers. A batch that recorded one trial and a count would be
    unable to say which repetition produced which verdict.
    """
    # Arrange
    benchmark_id = await _imported_suite(visitor)

    # Act
    repeated = await _repeat(visitor, benchmark_id, trials=3)

    # Assert
    assert repeated.status_code == 201, repeated.text
    body = repeated.json()
    assert body["trials"] == 3
    assert [entry["repetition_index"] for entry in body["repetitions"]] == [1, 2, 3]
    assert [entry["external_trial_id"] for entry in body["repetitions"]] == [
        f"{SCENARIO}#0#repetition-1",
        f"{SCENARIO}#0#repetition-2",
        f"{SCENARIO}#0#repetition-3",
    ]
    # Against the faulty implementation the discount is reported and not
    # applied, so every repetition's *observed* verdict fails while the imported
    # evaluator verdict stayed a pass.
    assert {entry["outcome_result"] for entry in body["repetitions"]} == {"failed"}
    assert len({entry["evaluation_run_id"] for entry in body["repetitions"]}) == 3

    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
    # Four trials: the imported one, which nothing replayed, plus its three
    # repetitions. The imported trial is not silently consumed by repeating it.
    assert len(read.json()["trials"]) == 4


async def test_each_repetition_runs_in_its_own_eval_workspace(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """FR-083: a second repetition inheriting the first one's cart would pass or
    fail for reasons belonging to a different trial, and the benchmark counts
    repetitions as independent observations."""
    # Arrange
    benchmark_id = await _imported_suite(visitor)

    # Act
    await _repeat(visitor, benchmark_id, trials=2)

    # Assert
    async with stack.state.database.transaction() as work:
        runs = await work.fetch_all(
            "SELECT execution_workspace_id FROM evaluation_runs WHERE status = 'completed'"
        )
    workspaces = {str(row["execution_workspace_id"]) for row in runs}
    assert len(workspaces) == 2


async def test_a_second_batch_continues_the_numbering(visitor: httpx.AsyncClient) -> None:
    """A second batch is a deeper population, not a colliding one. Restarting at
    one would either fail on the suite's uniqueness constraint or, worse,
    overwrite a repetition somebody has already read."""
    # Arrange
    benchmark_id = await _imported_suite(visitor)
    await _repeat(visitor, benchmark_id, trials=2)

    # Act
    second = await _repeat(visitor, benchmark_id, trials=2)

    # Assert
    assert second.status_code == 201, second.text
    assert [entry["repetition_index"] for entry in second.json()["repetitions"]] == [3, 4]


# --- the ceiling -------------------------------------------------------------


async def test_a_batch_above_the_ceiling_is_refused_rather_than_truncated(
    visitor: httpx.AsyncClient,
) -> None:
    """The ceiling belongs to the server (constitution §5's bounded operations).

    Refused rather than clamped: a caller who asked for twenty and silently
    received ten would go on to describe a rate over a population twice the size
    of the one that ran.
    """
    # Arrange
    benchmark_id = await _imported_suite(visitor)

    # Act
    refused = await _repeat(visitor, benchmark_id, trials=MAX_TRIAL_REPETITIONS + 1)

    # Assert
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"
    assert str(MAX_TRIAL_REPETITIONS) in refused.json()["error"]["message"]

    # And nothing was written. A refusal that had already created some trials
    # would leave the suite holding a population nobody asked for.
    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
    assert len(read.json()["trials"]) == 1


async def test_the_ceiling_is_reported_beside_the_correlation_it_bounds(
    visitor: httpx.AsyncClient,
) -> None:
    """A client that had to guess the ceiling would offer an action the server
    refuses."""
    # Arrange
    benchmark_id = await _imported_suite(visitor)

    # Act
    view = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/correlation")

    # Assert
    assert view.json()["repetition_ceiling"] == MAX_TRIAL_REPETITIONS


async def test_zero_trials_is_refused(visitor: httpx.AsyncClient) -> None:
    """A batch of none is not a measurement, and accepting it would put an empty
    population beside the real ones."""
    # Arrange
    benchmark_id = await _imported_suite(visitor)

    # Act
    refused = await _repeat(visitor, benchmark_id, trials=0)

    # Assert
    assert refused.status_code == 422, refused.text


# --- refusals that protect the population ------------------------------------


async def test_a_repetition_cannot_itself_be_repeated(visitor: httpx.AsyncClient) -> None:
    """Every repetition in a population must repeat the same recorded intent.

    Repeating a repetition would build a chain whose later members are
    repetitions of a *replay*, not of the imported trial — and the population
    would silently stop being what its label says.
    """
    # Arrange
    benchmark_id = await _imported_suite(visitor)
    await _repeat(visitor, benchmark_id, trials=1)

    # Act
    refused = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/repeated-trials",
        json={"source_external_trial_id": f"{SCENARIO}#0#repetition-1", "trials": 1},
    )

    # Assert
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "PRECONDITION_FAILED"


async def test_a_trial_with_no_trajectory_cannot_be_repeated(visitor: httpx.AsyncClient) -> None:
    """There is nothing to run again, and inventing a journey would report an
    outcome for calls nobody recorded."""
    # Arrange — an imported trial whose trajectory the normalizer refused.
    benchmark_id = await _imported_suite(visitor, trial=_trial(trajectory=[]))

    # Act
    refused = await _repeat(visitor, benchmark_id, trials=2)

    # Assert
    assert refused.status_code == 409, refused.text
    assert "trajectory" in refused.json()["error"]["message"]


async def test_an_executed_browser_suite_refuses_repetition(visitor: httpx.AsyncClient) -> None:
    """FR-091 binds such a trial to a browser execution that already happened.
    There is no second execution to run, and manufacturing one would attribute a
    fresh outcome to another run's call evidence."""
    # Arrange
    await _select_contract(visitor)
    created = await visitor.post(BENCHMARKS, json={"correlation_mode": "executed_browser"})
    benchmark_id = created.json()["benchmark_id"]
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/imports", content=_report(_trial()))

    # Act
    refused = await _repeat(visitor, benchmark_id, trials=2)

    # Assert
    assert refused.status_code == 409, refused.text
    assert "executed_browser" in refused.json()["error"]["message"]


async def test_a_variant_the_manifest_never_froze_is_refused(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-100 freezes the set before trials begin; naming an index outside it
    describes a population this suite cannot have."""
    # Arrange
    benchmark_id = await _imported_suite(visitor)

    # Act
    refused = await _repeat(visitor, benchmark_id, trials=1, variant_index=3)

    # Assert
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


# --- the correlation view ----------------------------------------------------


async def test_the_correlation_view_names_the_disagreement_as_a_rate(
    visitor: httpx.AsyncClient,
) -> None:
    """Goal 8, end to end: the evaluator scored every call correct and the
    independently observed state disagreed every time.

    One trial makes that an anecdote. Three make it `0.4`-style arithmetic — here
    `1.0000` — and the view has to say so per population rather than as a
    suite-wide number that other scenarios could dilute.
    """
    # Arrange
    benchmark_id = await _imported_suite(visitor)
    await _repeat(visitor, benchmark_id, trials=3)

    # Act
    view = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/correlation")

    # Assert
    assert view.status_code == 200, view.text
    body = view.json()
    population = next(entry for entry in body["populations"] if entry["trials"] > 1)
    assert population["label"] == SCENARIO
    assert population["overstated_trials"] == 3
    assert population["overstated_rate"]["value"] == "1.0000"
    assert population["agreement_trials"] == 0
    # Four trials in this population and three of them counted: the imported
    # trial belongs to the same scenario and is disclosed as excluded rather
    # than dropped, so the denominator and the coverage stay legible together.
    assert population["counts"]["eligible_trials"] == 3
    assert population["counts"]["excluded_trials"] == 1
    evaluator = {entry["result"]: entry["trials"] for entry in population["evaluator_distribution"]}
    observed = {entry["result"]: entry["trials"] for entry in population["observed_distribution"]}
    assert evaluator == {"passed": 4, "failed": 0, "error": 0}
    # The spread a single sample would have hidden: three observed failures, and
    # the one trial nothing replayed reported as unreached rather than as a pass.
    assert observed["failed"] == 3
    assert observed["not_reached"] == 1
    assert observed["passed"] == 0


async def test_the_two_layers_agree_against_the_corrected_implementation(
    visitor: httpx.AsyncClient,
) -> None:
    """The counterpart that makes the test above mean something: a correlation
    view that always reported disagreement would satisfy it just as happily."""
    # Arrange
    benchmark_id = await _imported_suite(visitor, scenario_mode="post_fix", failure_profile=None)
    await _repeat(visitor, benchmark_id, trials=2)

    # Act
    view = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/correlation")

    # Assert
    population = next(
        entry for entry in view.json()["populations"] if entry["counts"]["eligible_trials"] > 0
    )
    assert population["agreement_trials"] == 2
    assert population["agreement_rate"]["value"] == "1.0000"
    assert population["overstated_trials"] == 0
    assert population["overstated_rate"]["value"] == "0.0000"


async def test_a_suite_with_no_repetitions_reports_no_measured_population(
    visitor: httpx.AsyncClient,
) -> None:
    """ "Nothing has run" must not read as "everything ran and agreed"."""
    # Arrange
    benchmark_id = await _imported_suite(visitor)

    # Act
    view = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/correlation")

    # Assert — the imported trial is present and excluded, so every rate over it
    # is null rather than a measured zero.
    (population,) = view.json()["populations"]
    assert population["counts"]["eligible_trials"] == 0
    assert population["overstated_rate"]["value"] is None
    assert population["agreement_rate"]["value"] is None


async def test_repetitions_of_a_frozen_variant_are_grouped_under_its_words(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-100 froze the texts; the view shows the words the agent was given
    rather than an index a reader would have to go and resolve."""
    # Arrange
    benchmark_id = await _imported_suite(visitor)
    frozen = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/frozen-variants",
        json={
            "canonical_intent": CANONICAL_INTENT,
            "variants": [{"kind": "paraphrased", "text": VARIANT_TEXT}],
            "approved_indices": [0],
            "reviewer": "ada",
        },
    )
    assert frozen.status_code == 201, frozen.text

    # Act
    repeated = await _repeat(visitor, benchmark_id, trials=2, variant_index=0)
    view = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/correlation")

    # Assert
    assert repeated.status_code == 201, repeated.text
    labels = {entry["label"] for entry in view.json()["populations"]}
    # The repetitions group under the variant's own text; the imported trial
    # keeps its scenario, because it exercised no variant and saying it did
    # would attribute it to a population it never ran in.
    assert labels == {VARIANT_TEXT, SCENARIO}


# --- partial batches and cancellation ----------------------------------------


class _StandInReplayer:
    """A replayer whose per-repetition outcome the test chooses.

    Injected rather than provoked, because a *deterministic* partial failure is
    the only kind a test may assert on: a real harness error that happened to
    strike the second repetition would be an order-dependent test, and the suite
    forbids those. `test_benchmark_replay.py` covers the real replayer against
    the real adapter; what is under test here is what the batch around it does.

    It closes an eligible repetition's row the way `BenchmarkReplayService` does,
    because that write is part of the contract this stands in for — a double that
    reported success and left the row open would let the batch's own bookkeeping
    pass a test it should fail.
    """

    def __init__(
        self,
        database: Any,
        *,
        excludes_at: int = 0,
        cancels_at: int = 0,
        outcome: OutcomeTrialResult = OutcomeTrialResult.FAILED,
    ) -> None:
        self._database = database
        self._excludes_at = excludes_at
        self._cancels_at = cancels_at
        self._outcome = outcome
        self.calls = 0

    async def replay(self, trial: Any, **_: Any) -> ReplayedTrial:
        self.calls += 1
        if self.calls == self._cancels_at:
            raise asyncio.CancelledError
        if self.calls == self._excludes_at:
            return ReplayedTrial(
                external_trial_id=trial.external_trial_id,
                outcome_result=OutcomeTrialResult.NOT_REACHED,
                eligibility=TrialEligibility.EXCLUDED,
                exclusion_reason=ExclusionReason.HARNESS_ERROR,
                evaluation_run_id=None,
                execution_workspace_id=None,
                detail="the target went away",
            )
        async with self._database.transaction() as work:
            await work.execute(
                "UPDATE benchmark_trials SET outcome_result = ?, eligibility = ?, "
                "exclusion_reason = NULL WHERE id = ?",
                (self._outcome.value, TrialEligibility.ELIGIBLE.value, trial.trial_row_id),
            )
        return ReplayedTrial(
            external_trial_id=trial.external_trial_id,
            outcome_result=self._outcome,
            eligibility=TrialEligibility.ELIGIBLE,
            exclusion_reason=None,
            evaluation_run_id=None,
            execution_workspace_id=None,
        )


async def _plan(stack: FastAPI, visitor: httpx.AsyncClient, count: int) -> tuple[Any, str, Any]:
    """A suite, a workspace, and a plan for `count` repetitions of its trial."""
    benchmark_id = await _imported_suite(visitor)
    workspace_id = (await visitor.get(f"{API_PREFIX}/workspace")).json()["workspace_id"]
    async with stack.state.database.reading() as work:
        plan = await BenchmarkService(work, workspace_id).plan_repetitions(
            benchmark_id,
            source_external_trial_id=f"{SCENARIO}#0",
            count=count,
        )
    return plan, workspace_id, benchmark_id


async def _recorded(stack: FastAPI, benchmark_id: str) -> list[dict[str, Any]]:
    async with stack.state.database.transaction() as work:
        rows = await work.fetch_all(
            "SELECT external_trial_id, eligibility, exclusion_reason, repetition_index "
            "FROM benchmark_trials WHERE benchmark_suite_id = ? AND repetition_index IS NOT NULL "
            "ORDER BY repetition_index",
            (benchmark_id,),
        )
    return [dict(row) for row in rows]


async def test_a_failed_repetition_stays_in_the_suite_with_its_reason(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """FR-092 keeps harness failures out of the denominator and *in* coverage.

    Dropping the repetition would let coverage improve every time the harness
    broke; recording it as a business failure would let a flaky harness be read
    as a broken target. It stays, excluded, and the reason says which.
    """
    # Arrange
    plan, workspace_id, benchmark_id = await _plan(stack, visitor, 3)
    replayer = _StandInReplayer(stack.state.database, excludes_at=2)
    service = RepeatedTrialService(
        stack.state.database, stack.state.adapters, stack.state.workspaces, replays=replayer
    )

    # Act
    completed = await service.run(
        plan, workspace_id=workspace_id, contract=None, adapter_id="buggy_store"
    )

    # Assert — the batch finished, and the middle repetition is disclosed.
    assert [trial.repetition_index for trial in completed] == [1, 2, 3]
    assert completed[1].exclusion_reason is ExclusionReason.HARNESS_ERROR
    rows = await _recorded(stack, benchmark_id)
    assert [row["eligibility"] for row in rows] == ["eligible", "excluded", "eligible"]
    assert rows[1]["exclusion_reason"] == "harness_error"
    # Three attempts, not four: a failed repetition is not silently retried.
    assert replayer.calls == 3


async def test_a_cancelled_batch_keeps_what_it_started_and_invents_nothing(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """Constitution §5: cancellation propagates, and a partially completed
    operation stays visible rather than being silently retried.

    The repetition that was interrupted stays as it was written — excluded, with
    no verdict — because a row that appeared only on success would make an
    interrupted batch of five look like a completed batch of one.
    """
    # Arrange
    plan, workspace_id, benchmark_id = await _plan(stack, visitor, 4)
    replayer = _StandInReplayer(
        stack.state.database, cancels_at=2, outcome=OutcomeTrialResult.PASSED
    )
    service = RepeatedTrialService(
        stack.state.database, stack.state.adapters, stack.state.workspaces, replays=replayer
    )

    # Act / Assert — the cancellation is not swallowed.
    with pytest.raises(asyncio.CancelledError):
        await service.run(plan, workspace_id=workspace_id, contract=None, adapter_id="buggy_store")

    rows = await _recorded(stack, benchmark_id)
    # Two rows: the one that completed and the one that was interrupted. The
    # third and fourth were never started and are not pretended to exist.
    assert [row["repetition_index"] for row in rows] == [1, 2]
    assert rows[0]["eligibility"] == "eligible"
    assert rows[1]["eligibility"] == "excluded"
    assert rows[1]["exclusion_reason"] == "outcome_not_reached"
    assert replayer.calls == 2


async def test_a_suite_sealed_midway_stops_the_batch(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """§16.4 closes the population at `ready`, and the check that enforces it has
    to run on every insert — the batch does not hold the workspace lock across
    its replays, so the state can move underneath it."""
    # Arrange
    plan, workspace_id, benchmark_id = await _plan(stack, visitor, 3)
    async with stack.state.database.transaction() as work:
        await BenchmarkService(work, workspace_id).seal(benchmark_id)

    # Act / Assert
    from actionwitness_service.api.errors import ApiError

    service = RepeatedTrialService(
        stack.state.database,
        stack.state.adapters,
        stack.state.workspaces,
        replays=_StandInReplayer(stack.state.database),
    )
    with pytest.raises(ApiError):
        await service.run(plan, workspace_id=workspace_id, contract=None, adapter_id="buggy_store")
    assert await _recorded(stack, benchmark_id) == []
