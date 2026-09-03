"""Replay an imported trajectory in an isolated eval workspace (FR-091, §24.7).

FR-091's `imported_trajectory_replay`: "the imported, redacted, allowlisted tool
trajectory is replayed in a fresh isolated eval workspace through the same
registered `TargetAdapter`, its public target surface, and deterministic
confirmation safeguards required by FR-086 and FR-087."

Every clause of that sentence is load-bearing, and this module reuses 007's
machinery for each rather than growing its own:

- **fresh isolated eval workspace** — `prepare_eval_workspace`, one per trial. A
  second trial inheriting the first's target state would pass or fail for
  reasons belonging to a different trial, and the whole benchmark counts trials
  as independent observations.
- **through the registered adapter** — `TrajectoryReplayer`, which reaches the
  target only through its published surface. A runner that wrote the target's
  storage directly would produce a state the target's own code never made.
- **allowlisted** — the replayer refuses a tool the adapter does not publish,
  rather than skipping it. Skipping would replay a different journey and report
  its outcome as this trial's.
- **deterministic confirmation** — see below.

**No imported trial carries consent.** §24.5's providers replay a decision a
*recording* contained; an evaluator report contains none. FR-087 forbids
inferring consent and the constitution forbids an agent creating its own, so
every imported replay runs under `no_confirmation`: a protected mutation is
blocked, and correct behaviour is what makes the trial pass. Supplying an
approval here would manufacture the one thing the harness is supposed to check.

**A replay failure is not an outcome failure.** If the harness cannot run the
trial — unknown adapter, unrestorable fixture, a tool the adapter dropped — the
trial is `excluded` with a harness reason, never counted as a business failure.
FR-092 keeps errors out of the denominator precisely so a flaky harness cannot
be read as a broken target.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from actionwitness_core.benchmarks.enums import (
    ExclusionReason,
    OutcomeTrialResult,
    TrialEligibility,
    outcome_from_layer_result,
)
from actionwitness_core.contracts.models import OutcomeContract
from actionwitness_core.engine.assertions import evaluate_assertions
from actionwitness_core.engine.classification import classify_assertion_failures
from actionwitness_core.engine.findings import aggregate
from actionwitness_core.evals.models import TrajectoryStep
from actionwitness_core.ports.models import ScenarioSelection

from actionwitness_service.api.errors import ApiError

# One-way by design: the repetition runner reaches into the suite service to
# record each trial, and nothing in that service reaches back here. The
# trajectory reader below is deliberately duplicated in narrower form there
# rather than imported, so the dependency cannot become a cycle.
from actionwitness_service.application.benchmark_service import BenchmarkService, RepetitionPlan
from actionwitness_service.application.eval_runner import (
    TrajectoryReplayer,
    prepare_eval_workspace,
)
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.repositories import new_id

__all__ = [
    "BenchmarkReplayService",
    "RepeatedTrial",
    "RepeatedTrialService",
    "ReplayedTrial",
    "TrialReplayInput",
]


class _NoConsent:
    """§24.5's `no_confirmation` provider, for a source that recorded none.

    Deliberately not configurable. An evaluator report has no consent evidence
    in it at all, so there is nothing for a provider to replay — and a knob here
    would be a way to grant an approval nobody gave.
    """

    async def grant_for(self, step: Any, correlation: str) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class TrialReplayInput:
    """One trial's replayable content, plus the scenario that judges it."""

    trial_row_id: str
    external_trial_id: str
    trajectory: tuple[Mapping[str, Any], ...]
    contract: OutcomeContract
    scenario: ScenarioSelection


@dataclass(frozen=True, slots=True)
class ReplayedTrial:
    """What one replay concluded, before anything counts it."""

    external_trial_id: str
    outcome_result: OutcomeTrialResult
    eligibility: TrialEligibility
    exclusion_reason: ExclusionReason | None
    evaluation_run_id: str | None
    execution_workspace_id: str | None
    detail: str = ""


class BenchmarkReplayService:
    """Runs `imported_trajectory_replay` trials, one isolated workspace each."""

    def __init__(self, database: Database, registry: Any, workspaces: Any) -> None:
        self._database = database
        self._registry = registry
        self._workspaces = workspaces

    async def replay(
        self,
        trial: TrialReplayInput,
        *,
        owner_workspace_id: str,
        adapter_id: str,
    ) -> ReplayedTrial:
        """One trial, end to end. Never raises for a target failure."""
        try:
            adapter = self._registry.adapter(adapter_id)
        except Exception:
            # Nothing was learned about the target, so this is coverage, not a
            # verdict (FR-092).
            return self._excluded(trial, ExclusionReason.HARNESS_ERROR, "unknown adapter")

        steps = _steps(trial.trajectory)
        if not steps:
            return self._excluded(trial, ExclusionReason.MISSING_TRAJECTORY, "no steps")

        workspace_id = await prepare_eval_workspace(
            self._database, self._workspaces, owner_workspace_id
        )
        replayer = TrajectoryReplayer(adapter)
        evaluation_run_id = new_id("evr")
        # Opened *before* the replay, because the replayer records each call
        # into `evaluation_events`, which references the run. 007's runner opens
        # its row the same way and for the same reason. It starts as `error`:
        # if this process dies mid-replay, what survives says the attempt did
        # not finish rather than claiming a verdict nobody reached.
        await self._open_run(
            evaluation_run_id,
            trial,
            owner_workspace_id=owner_workspace_id,
            execution_workspace_id=workspace_id,
        )

        try:
            before = await self._restore(adapter, workspace_id, trial.scenario)
            outcome = await replayer.replay_steps(
                workspace_id,
                steps,
                identity=trial.external_trial_id,
                eval_run_id=evaluation_run_id,
                before=before,
                consent=_NoConsent(),
                work_factory=lambda: self._database.transaction(),
            )
        except ApiError as refused:
            return self._excluded(
                trial, ExclusionReason.HARNESS_ERROR, refused.message, workspace_id
            )
        except Exception as failure:  # pragma: no cover - defensive
            return self._excluded(
                trial, ExclusionReason.HARNESS_ERROR, type(failure).__name__, workspace_id
            )

        if outcome.after is None:
            # §5's rail: an observation failure is an explicit non-pass, never a
            # degradation to success.
            return self._excluded(
                trial, ExclusionReason.OUTCOME_NOT_REACHED, "no final observation", workspace_id
            )

        result = self._judge(trial.contract, outcome)
        await self._finish_run(evaluation_run_id, trial, result=result)
        return ReplayedTrial(
            external_trial_id=trial.external_trial_id,
            outcome_result=result,
            eligibility=TrialEligibility.ELIGIBLE,
            exclusion_reason=None,
            evaluation_run_id=evaluation_run_id,
            execution_workspace_id=workspace_id,
        )

    # -- internals ------------------------------------------------------------

    async def _restore(self, adapter: Any, workspace_id: str, scenario: ScenarioSelection) -> Any:
        """Reseed through the adapter and observe the starting state.

        No fixture is verified here, unlike an eval case's restore: a benchmark
        trial carries no recorded fixture of its own — §24.7 step 1 puts the
        fixture in the *scenario*, which the suite supplies. Checking a fixture
        the trial never recorded would fail every import.
        """
        await adapter.prepare(workspace_id, {}, scenario)
        return await adapter.observation_provider().capture(workspace_id)

    def _judge(self, contract: OutcomeContract, outcome: Any) -> OutcomeTrialResult:
        """§24.7 step 5: the deterministic engine decides the outcome layer.

        The same engine the live path uses. A benchmark that scored outcomes its
        own way would be a second source of truth for the thing the product
        exists to state.
        """
        findings = classify_assertion_failures(
            evaluate_assertions(
                contract.assertions,
                initial=outcome.before.as_context(),
                final=outcome.after.as_context(),
            ),
            contract.assertions,
            events=outcome.events,
            effect_map={},
            initial=outcome.before.as_context(),
        )
        return outcome_from_layer_result(aggregate(findings))

    def _excluded(
        self,
        trial: TrialReplayInput,
        reason: ExclusionReason,
        detail: str = "",
        workspace_id: str | None = None,
    ) -> ReplayedTrial:
        return ReplayedTrial(
            external_trial_id=trial.external_trial_id,
            # `not_reached` rather than `error`: the outcome layer produced no
            # verdict, which is not the same as producing a failing one.
            outcome_result=OutcomeTrialResult.NOT_REACHED,
            eligibility=TrialEligibility.EXCLUDED,
            exclusion_reason=reason,
            evaluation_run_id=None,
            execution_workspace_id=workspace_id,
            detail=detail,
        )

    async def _open_run(
        self,
        evaluation_run_id: str,
        trial: TrialReplayInput,
        *,
        owner_workspace_id: str,
        execution_workspace_id: str,
    ) -> None:
        """§17.1's evaluation run, opened incomplete.

        Written against the widened `evaluation_runs` (migration 4), with
        `benchmark_trial_id` set and `evaluation_case_id` null — the CHECK there
        makes "exactly one origin" a schema fact rather than this method's good
        intentions.
        """
        async with self._database.transaction() as work:
            await work.execute(
                """
                INSERT INTO evaluation_runs (
                    id, owner_workspace_id, execution_workspace_id, evaluation_case_id,
                    benchmark_trial_id, evaluation_case_content_hash, mode,
                    environment_profile, implementation_version, status, started_at
                ) VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_run_id,
                    owner_workspace_id,
                    execution_workspace_id,
                    trial.trial_row_id,
                    trial.contract.content_hash(),
                    "imported_trajectory_replay",
                    "current",
                    "008",
                    "error",
                    work.now(),
                ),
            )

    async def _finish_run(
        self, evaluation_run_id: str, trial: TrialReplayInput, *, result: OutcomeTrialResult
    ) -> None:
        """Close the run and make the trial eligible, in one transaction.

        The trial's `evaluation_run_id` is set only here, so a run that never
        completed is never linked from an eligible trial — the attempt stays on
        record as `error`, which is the honest thing to keep, while the trial
        still reports itself as excluded.
        """
        async with self._database.transaction() as work:
            await work.execute(
                "UPDATE evaluation_runs SET status = ?, overall_result = ?, completed_at = ? "
                "WHERE id = ?",
                ("completed", result.value, work.now(), evaluation_run_id),
            )
            await work.execute(
                "UPDATE benchmark_trials SET evaluation_run_id = ?, outcome_result = ?, "
                "eligibility = ?, exclusion_reason = NULL WHERE id = ?",
                (
                    evaluation_run_id,
                    result.value,
                    TrialEligibility.ELIGIBLE.value,
                    trial.trial_row_id,
                ),
            )


@dataclass(frozen=True, slots=True)
class RepeatedTrial:
    """One repetition, as it was recorded and then judged."""

    external_trial_id: str
    repetition_index: int
    outcome_result: OutcomeTrialResult
    eligibility: TrialEligibility
    exclusion_reason: ExclusionReason | None
    evaluation_run_id: str | None
    detail: str = ""


class RepeatedTrialService:
    """Run one bound variant N times, each repetition on its own (§26.5).

    §26.5's Tier 3 showcase is "six intent variants with five repeated trials
    each", and a single sample cannot characterise a non-deterministic agent: it
    can only report what happened once. Repetition is what turns "the observed
    state disagreed with the evaluator" into a rate somebody can act on.

    **Every repetition is a whole trial.** Its own row, its own eval workspace,
    its own restored fixture, its own evaluation run. A batch that reused one
    workspace would be measuring how a target behaves after four previous
    journeys, and reporting it as five independent observations.

    **The row exists before the replay does.** `record_repetition` commits first,
    so a batch that is cancelled or that dies halfway leaves the repetitions it
    started visible as `excluded` / `not_reached` rather than absent — the
    constitution requires a partially completed operation to stay visible rather
    than be silently retried, and nothing in this class retries anything.

    **No transaction spans a replay.** ADR-0003: the write that records a
    repetition and the write that closes it are separate short transactions
    around I/O that holds neither. The workspace lock is not held either, for the
    same reason `/replay` does not hold it.
    """

    def __init__(
        self,
        database: Database,
        registry: Any,
        workspaces: Any,
        *,
        replays: Any | None = None,
    ) -> None:
        self._database = database
        # Injectable so a test can make one repetition of a batch fail or be
        # cancelled deterministically. Defaulted rather than required, so the
        # route cannot accidentally be handed a replayer that is not the real one.
        self._replays = replays or BenchmarkReplayService(database, registry, workspaces)

    async def run(
        self,
        plan: RepetitionPlan,
        *,
        workspace_id: str,
        contract: OutcomeContract,
        adapter_id: str,
    ) -> list[RepeatedTrial]:
        """Record and replay each repetition in the plan, in order.

        Sequential rather than concurrent, deliberately. Each repetition drives
        the same target through its adapter, and running them at once would let
        one repetition observe state another one produced — the isolation FR-083
        buys with a fresh workspace is per-workspace, not per-target-process.
        Sequential also means a cancellation lands between two repetitions rather
        than in the middle of several.
        """
        completed: list[RepeatedTrial] = []
        for repetition_index in plan.repetition_ids():
            async with self._database.transaction() as work:
                trial_row_id = await BenchmarkService(work, workspace_id).record_repetition(
                    plan, repetition_index
                )
            external_trial_id = plan.external_trial_id(repetition_index)
            # `CancelledError` is a `BaseException` and passes straight through
            # this call, out of this loop, and out of the request — which is what
            # cancellation propagating means. The repetitions already recorded
            # stay committed and stay excluded, and none of them is retried.
            replayed = await self._replays.replay(
                TrialReplayInput(
                    trial_row_id=trial_row_id,
                    external_trial_id=external_trial_id,
                    trajectory=stored_trajectory(plan.metadata_json),
                    contract=contract,
                    scenario=ScenarioSelection(
                        scenario_mode=plan.scenario_mode or "post_fix",
                        fault_profile=plan.failure_profile,
                    ),
                ),
                owner_workspace_id=workspace_id,
                adapter_id=adapter_id,
            )
            if replayed.eligibility is TrialEligibility.EXCLUDED:
                await self._record_exclusion(trial_row_id, replayed)
            completed.append(
                RepeatedTrial(
                    external_trial_id=external_trial_id,
                    repetition_index=repetition_index,
                    outcome_result=replayed.outcome_result,
                    eligibility=replayed.eligibility,
                    exclusion_reason=replayed.exclusion_reason,
                    evaluation_run_id=replayed.evaluation_run_id,
                    detail=replayed.detail,
                )
            )
        return completed

    async def _record_exclusion(self, trial_row_id: str, replayed: ReplayedTrial) -> None:
        """Say *why* a repetition produced no verdict.

        The replay writes the row back only when it succeeds; an exclusion is
        returned rather than stored, because `BenchmarkReplayService` is also used
        where the caller decides what an exclusion means. Here it means the same
        thing it means everywhere in FR-092 — coverage, not a business failure —
        and the reason is written so a reader can tell a harness that broke from a
        target that never reached a verdict. Left unwritten, every failed
        repetition would read as the generic `outcome_not_reached` the row was
        created with.
        """
        if replayed.exclusion_reason is None:  # pragma: no cover - excluded implies a reason
            return
        async with self._database.transaction() as work:
            await work.execute(
                "UPDATE benchmark_trials SET outcome_result = ?, eligibility = ?, "
                "exclusion_reason = ? WHERE id = ?",
                (
                    replayed.outcome_result.value,
                    TrialEligibility.EXCLUDED.value,
                    replayed.exclusion_reason.value,
                    trial_row_id,
                ),
            )


def _steps(trajectory: Sequence[Mapping[str, Any]]) -> tuple[TrajectoryStep, ...]:
    """The imported calls, in the shape the shared replayer reads.

    Numbered from one and densely, because that is what `TrajectoryStep`
    requires and what the recorded order means. A malformed step yields nothing
    rather than a partial trajectory: replaying some of a journey and reporting
    the result as the whole one is the failure mode this refuses.
    """
    steps: list[TrajectoryStep] = []
    for index, step in enumerate(trajectory, start=1):
        name = step.get("name")
        arguments = step.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
            return ()
        steps.append(TrajectoryStep(sequence=index, tool=name, arguments=dict(arguments)))
    return tuple(steps)


def stored_trajectory(metadata_json: str | None) -> tuple[Mapping[str, Any], ...]:
    """The trajectory a trial row carries, read back out of its metadata."""
    try:
        stored = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:  # pragma: no cover - written by this service
        return ()
    trajectory = stored.get("trajectory")
    if not isinstance(trajectory, list):
        return ()
    return tuple(step for step in trajectory if isinstance(step, Mapping))
