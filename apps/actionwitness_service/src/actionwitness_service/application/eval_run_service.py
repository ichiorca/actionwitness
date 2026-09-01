"""Running one eval case end to end (§24.3, §24.4, §9.8, FR-088).

The pipeline §24.3 draws, with the evaluation done by the same engine a browser
run uses: load and validate, isolate, restore, replay, capture, evaluate,
compare, report, clean up.

**Eval status is expectation matching, not business outcome.** This is the
module where that distinction becomes structural. `overall_result` records what
the target did; `status` records whether the case's expectation held. A
`reproduce_source` run that faithfully reproduces a recorded `failed` outcome is
`status: passed` — reproducing the failure is what the case asked for — and
anyone reading only one of the two fields will misunderstand the run, which is
why the report carries both and never collapses them.

**Cleanup happens after the report is persisted**, in that order. FR-009 sweeps
eval workspaces anyway; doing it before the report existed would risk losing the
evidence that explains the run, and an unswept workspace is a smaller problem
than an unexplained result.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.evals.enums import EvalEnvironment, EvalStatus
from actionwitness_core.evals.interaction import provider_for
from actionwitness_core.evals.models import (
    EvalReport,
    RegressionEvalCase,
    expectation_matches,
)
from actionwitness_core.reports.enums import LayerResult

from actionwitness_service.api.errors import ApiError
from actionwitness_service.application.eval_runner import (
    ReplayOutcome,
    TrajectoryReplayer,
    prepare_eval_workspace,
)
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.repositories import new_id

__all__ = ["EvalRunOutcome", "EvalRunService"]


@dataclass(frozen=True, slots=True)
class EvalRunOutcome:
    """One completed eval run."""

    eval_run_id: str
    report: EvalReport


class EvalRunService:
    """Replays a case in an isolated workspace and judges the result."""

    def __init__(
        self,
        database: Database,
        registry: Any,
        workspaces: Any,
        *,
        implementation_version: str = "0.1.0",
        build_commit: str | None = None,
        clock: Callable[[], datetime] | None = None,
        scenario_resolver: Callable[..., Any] | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._workspaces = workspaces
        self._implementation_version = implementation_version
        self._build_commit = build_commit
        self._clock = clock or (lambda: datetime.now(UTC))
        #: §24.4's mapping is target knowledge. Injected so the core never
        #: learns what `pre_fix` means and a second target can supply its own.
        self._scenario_resolver = scenario_resolver

    async def run(
        self,
        case: RegressionEvalCase,
        *,
        owner_workspace_id: str,
        environment: EvalEnvironment = EvalEnvironment.CURRENT,
        evaluate: Callable[..., Any] | None = None,
    ) -> EvalRunOutcome:
        """§24.3, end to end."""
        eval_run_id = new_id("evr")
        started = self._clock()

        # Resolved by the target id the registry indexes, then checked against
        # the adapter the case names. Falling back from an unknown adapter to
        # whatever the target id resolves to would replay the case against a
        # different implementation than it was cut for and report the result as
        # that case's — the quietest way for a regression suite to lie.
        slot = self._registry.resolve(case.target.adapter) or self._registry.resolve(case.target.id)
        if slot is not None and not _names_the_same_adapter(slot, case.target.adapter):
            slot = None
        if slot is None or slot.factory is None:
            await self._open(
                eval_run_id,
                case,
                environment,
                owner_workspace_id,
                execution_workspace_id="",
                started=started,
            )
            # No adapter, no run. `error` rather than `failed`: nothing about
            # the target was learned, and FR-088 gives that its own exit code.
            return await self._finish(
                eval_run_id,
                case,
                environment,
                owner_workspace_id,
                execution_workspace_id="",
                started=started,
                report=self._error_report(
                    case,
                    environment,
                    f"no registered adapter for {case.target.adapter!r}",
                ),
            )

        adapter = slot.factory()
        execution_workspace = await prepare_eval_workspace(
            self._database, self._workspaces, owner_workspace_id
        )
        await self._open(
            eval_run_id,
            case,
            environment,
            owner_workspace_id,
            execution_workspace_id=execution_workspace,
            started=started,
        )

        try:
            outcome = await self._replay(
                adapter, case, environment, execution_workspace, eval_run_id
            )
        except ApiError as refused:
            return await self._finish(
                eval_run_id,
                case,
                environment,
                owner_workspace_id,
                execution_workspace_id=execution_workspace,
                started=started,
                report=self._error_report(case, environment, refused.message),
            )

        report = self._judge(case, environment, outcome, evaluate, _effect_map(adapter))
        return await self._finish(
            eval_run_id,
            case,
            environment,
            owner_workspace_id,
            execution_workspace_id=execution_workspace,
            started=started,
            report=report,
        )

    # -- pipeline ------------------------------------------------------------

    async def _replay(
        self,
        adapter: Any,
        case: RegressionEvalCase,
        environment: EvalEnvironment,
        workspace_id: str,
        eval_run_id: str,
    ) -> ReplayOutcome:
        scenario = self._scenario_for(case, environment)
        replayer = TrajectoryReplayer(adapter, clock=self._clock)
        before = await replayer.restore(workspace_id, case, scenario)

        consent = provider_for(case.replay.confirmation_strategy, case.replay.recorded_decisions)
        return await replayer.replay(
            workspace_id,
            case,
            eval_run_id=eval_run_id,
            before=before,
            consent=consent,
            work_factory=self._database.transaction,
        )

    def _scenario_for(self, case: RegressionEvalCase, environment: EvalEnvironment) -> Any:
        if self._scenario_resolver is None:
            from integrations.buggy_store.environments import scenario_for

            resolver = scenario_for
        else:
            resolver = self._scenario_resolver
        return resolver(
            environment,
            source_scenario_mode=case.source.scenario_mode,
            source_failure_profile=case.source.failure_profile,
        )

    def _judge(
        self,
        case: RegressionEvalCase,
        environment: EvalEnvironment,
        outcome: ReplayOutcome,
        evaluate: Callable[..., Any] | None,
        effect_map: Any = None,
    ) -> EvalReport:
        """Evaluate the contract, then compare against the expectation.

        Two separate steps, and the order matters: the verdict about the
        *target* is reached first and without reference to what the case
        expected, so an expectation can never influence the finding it is about
        to be compared with.
        """
        if outcome.after is None:
            # Constitution §5: an observation failure "produces an explicit
            # non-pass result; it never degrades to success."
            return self._error_report(
                case, environment, "the target could not be observed after replay"
            )
        if outcome.stopped_at is not None:
            return self._error_report(case, environment, outcome.detail)

        actual_result, actual_classifications = self._evaluate(case, outcome, evaluate, effect_map)

        expectation = case.expected.for_environment(environment)
        # §24.3a: a policy that could not be evaluated is excluded from *both*
        # sides and named in the report, so a passing eval never quietly means
        # "not checked".
        excluded = frozenset(case.non_replayable_policies)
        actual = tuple(c for c in actual_classifications if c.value not in excluded)
        expected = tuple(c for c in expectation.required_classifications if c.value not in excluded)

        matched = expectation_matches(
            expectation.model_copy(update={"required_classifications": expected}),
            actual_result=actual_result,
            actual_classifications=actual,
            non_replayable=tuple(excluded),
        )

        return EvalReport(
            eval_case_id=case.id,
            eval_case_hash=case.content_hash(),
            implementation_version=self._implementation_version,
            build_commit=self._build_commit,
            environment=environment,
            status=EvalStatus.PASSED if matched else EvalStatus.FAILED,
            overall_result=actual_result,
            actual_classifications=actual,
            expected_classifications=expected,
            classification_match=frozenset(actual) == frozenset(expected),
            replayed_trajectory=outcome.steps,
            final_state=dict(outcome.after.payload),
            non_replayable_policies=case.non_replayable_policies,
            detail=(
                "reproduced the recorded outcome"
                if matched and actual_result is not LayerResult.PASSED
                else ""
            ),
        )

    def _evaluate(
        self,
        case: RegressionEvalCase,
        outcome: ReplayOutcome,
        evaluate: Callable[..., Any] | None,
        effect_map: Any = None,
    ) -> tuple[LayerResult, tuple[FailureClassification, ...]]:
        """Ask the engine what the replayed state means.

        Injected so a test can drive the comparison without a full engine run;
        the default is the same evaluation path a browser run takes, because
        FR-084 forbids a second implementation of the target's behaviour and
        AC-15 requires a replayed run to classify identically to its source.
        """
        if evaluate is not None:
            return evaluate(case, outcome)

        # The same entry points 005's verification uses. FR-084 forbids a
        # second implementation of the target's behaviour, and AC-15 requires a
        # replayed run to classify identically to its source — so this calls the
        # engine rather than reproducing its reasoning.
        from actionwitness_core.engine.assertions import evaluate_assertions
        from actionwitness_core.engine.classification import classify_assertion_failures
        from actionwitness_core.engine.findings import aggregate

        # Both contexts, as 005 passes them: an assertion may compare the final
        # state against the initial one, and supplying only the final would
        # silently change what a delta assertion means.
        #
        # Then classified, for the reason AC-15 exists: a bare mismatch is not
        # the same finding as a `false_success_or_state_mismatch`, and a case
        # whose expectation names the second would fail against a replay that
        # only produced the first — while the target behaved exactly as
        # recorded.
        findings = classify_assertion_failures(
            evaluate_assertions(
                case.contract.document.assertions,
                initial=outcome.before.as_context(),
                final=outcome.after.as_context(),
            ),
            case.contract.document.assertions,
            events=outcome.events,
            effect_map=effect_map,
            initial=outcome.before.as_context(),
        )
        result = aggregate(findings)
        classifications = tuple(
            {
                finding.classification
                for finding in findings
                if finding.classification is not None and finding.status is CheckStatus.FAILED
            }
        )
        return result, classifications

    # -- persistence ---------------------------------------------------------

    async def _open(
        self,
        eval_run_id: str,
        case: RegressionEvalCase,
        environment: EvalEnvironment,
        owner_workspace_id: str,
        *,
        execution_workspace_id: str,
        started: datetime,
    ) -> None:
        """Open the run row before anything writes evidence against it.

        `evaluation_events` hang off this row (§17.1), so it has to exist
        before the first replayed step records one — and §17.1's
        `started_at`/`completed_at` pair already says a run is a row that
        outlives its own execution.

        It opens as `error` with no completion. A process that dies mid-replay
        therefore leaves a run that says exactly what happened: nothing was
        learned about the target. A row that opened as `passed` would make a
        crash look like a success.
        """
        async with self._database.transaction() as work:
            await work.execute(
                """
                INSERT INTO evaluation_runs (
                    id, owner_workspace_id, execution_workspace_id, evaluation_case_id,
                    evaluation_case_content_hash, mode, environment_profile,
                    implementation_version, build_commit, status, overall_result,
                    started_at, completed_at, report_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    eval_run_id,
                    owner_workspace_id,
                    execution_workspace_id or owner_workspace_id,
                    case.id,
                    case.content_hash(),
                    "replay",
                    environment.value,
                    self._implementation_version,
                    self._build_commit,
                    EvalStatus.ERROR.value,
                    None,
                    started.isoformat(),
                    None,
                    None,
                ),
            )

    async def _finish(
        self,
        eval_run_id: str,
        case: RegressionEvalCase,
        environment: EvalEnvironment,
        owner_workspace_id: str,
        *,
        execution_workspace_id: str,
        started: datetime,
        report: EvalReport,
    ) -> EvalRunOutcome:
        """Persist the immutable report, then clean the mutable eval state.

        In that order. The report is what explains the run; the workspace is
        recoverable noise, and an unswept workspace is a smaller problem than a
        result nobody can account for.
        """
        async with self._database.transaction() as work:
            await work.execute(
                """
                UPDATE evaluation_runs
                   SET status = ?, overall_result = ?, completed_at = ?, report_json = ?
                 WHERE id = ?
                """,
                (
                    report.status.value,
                    None if report.overall_result is None else report.overall_result.value,
                    self._clock().isoformat(),
                    json.dumps(report.as_stored_document(), sort_keys=True),
                    eval_run_id,
                ),
            )

        if execution_workspace_id:
            await self._clean(execution_workspace_id)
        return EvalRunOutcome(eval_run_id=eval_run_id, report=report)

    async def _clean(self, execution_workspace_id: str) -> None:
        """FR-009's second rule, after the report persists.

        Delegates to `purge_eval_workspace_state`, which 004 wrote for this
        caller: it removes the workspace's *mutable* state and leaves the row.
        The row has to stay — `evaluation_runs.execution_workspace_id`
        references it (§17.1), and deleting it would orphan the run's own
        account of where it executed.
        """
        from actionwitness_service.application.cleanup import purge_eval_workspace_state

        async with self._database.transaction() as work:
            await purge_eval_workspace_state(work, execution_workspace_id)

    def _error_report(
        self, case: RegressionEvalCase, environment: EvalEnvironment, detail: str
    ) -> EvalReport:
        """§9.8's `error`: the harness could not reach a verdict.

        Never `failed`. A failure is a statement about the target; this is a
        statement about the run, and FR-088 gives it a different exit code
        precisely so CI can tell "your code broke" from "the case was invalid".
        """
        return EvalReport(
            eval_case_id=case.id,
            eval_case_hash=case.content_hash(),
            implementation_version=self._implementation_version,
            build_commit=self._build_commit,
            environment=environment,
            status=EvalStatus.ERROR,
            expected_classifications=case.expected.for_environment(
                environment
            ).required_classifications,
            non_replayable_policies=case.non_replayable_policies,
            detail=detail[:1024],
        )


def _names_the_same_adapter(slot: Any, declared: str) -> bool:
    """Whether a resolved slot is the adapter the case declares.

    The registry is keyed by module (`buggy_store`) while a case records the
    importable adapter path (`integrations.buggy_store`), so the comparison is
    on the tail rather than on equality — two vocabularies for one thing, the
    same mismatch 005's registry already documents.
    """
    return declared == slot.name or declared.endswith(f".{slot.name}")


def _effect_map(adapter: Any) -> Mapping[str, tuple[Any, ...]]:
    """§13.4's declared effect paths per tool, from the adapter itself.

    The classifier needs them to blame the action that was supposed to cause a
    change. An adapter declaring none loses causal attribution and nothing else
    (§12.2): the mismatch is still reported, just without a named cause.
    """
    return {spec.name: tuple(spec.effect_paths) for spec in adapter.tool_specs()}
