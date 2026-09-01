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
from actionwitness_service.application.eval_runner import (
    TrajectoryReplayer,
    prepare_eval_workspace,
)
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.repositories import new_id

__all__ = ["BenchmarkReplayService", "ReplayedTrial", "TrialReplayInput"]


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
