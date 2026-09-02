"""Deterministic replay of one eval case (§24.3, FR-083–FR-087).

§24.3's pipeline, in order: load and validate, create an isolated workspace,
resolve the registered adapter, restore the fixture through it, replay the
allowlisted trajectory, capture state and events, evaluate, report.

Three rules shape every step.

**Isolation is a workspace, not a convention** (FR-083). The eval workspace is
an ordinary `kind: eval` workspace created through 004's own store, owned by the
workspace that asked. That makes it swept by the existing cleanup and scoped by
the existing machinery — a replay that reached into an interactive workspace
would let a CI job mutate somebody's live demo, and a bespoke isolation
mechanism would be a second thing to get right.

**Replay goes through the same adapter a browser run uses** (FR-084). The core
runner imports no target service and keeps no second implementation of its
behaviour. If replaying needed the engine to change, that would be a finding
worth surfacing rather than patching around — AC-15 says a replayed run must
classify identically to its source, and 005's classifier already treats `agent`
and `eval` alike for exactly this reason.

**Only allowlisted tools run** (FR-086). A case is data a CI job executes, so
the adapter's own allowlist is what stands between a case file and arbitrary
calls. A step naming an unpublished tool fails the run as an invalid definition
rather than being skipped — skipping would replay a different journey and report
its outcome as the case's.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from actionwitness_core.evals.models import RegressionEvalCase, TrajectoryStep
from actionwitness_core.evidence.effects import effect_context
from actionwitness_core.evidence.models import RunEvent
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType
from actionwitness_core.ports.models import ExecutionContext, Observation, ScenarioSelection

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.repositories import new_id

__all__ = ["ReplayOutcome", "TrajectoryReplayer", "prepare_eval_workspace"]


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """What the replay produced, before anything judges it."""

    before: Observation
    after: Observation | None
    steps: tuple[TrajectoryStep, ...]
    #: The replayed timeline, in the shape the engine reads. Built here because
    #: AC-15 requires a replayed run to classify identically to its source, and
    #: FR-055 attributes a false success from *per-call* effect evidence — which
    #: exists only if the replay observes after every step, exactly as the live
    #: invocation path does. A timeline without it reports a bare mismatch, and
    #: the eval then fails for the wrong reason.
    events: tuple[RunEvent, ...] = ()
    #: Recorded so the report can say a replay stopped early rather than
    #: silently reporting the outcome of a shorter journey.
    stopped_at: int | None = None
    detail: str = ""


async def prepare_eval_workspace(
    database: Database,
    workspaces: Any,
    owner_workspace_id: str,
) -> str:
    """FR-083: "every replay shall create a new eval workspace".

    A fresh one per run, not a reused one: a second replay inheriting the first
    one's target state would pass or fail for reasons belonging to a different
    case.
    """
    async with database.transaction() as work:
        return await workspaces.create_eval_workspace(work, owner_workspace_id)


class TrajectoryReplayer:
    """Restores a fixture and replays a case's calls through its adapter."""

    def __init__(
        self,
        adapter: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        id_source: Callable[[], str] | None = None,
    ) -> None:
        self._adapter = adapter
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_source = id_source or (lambda: new_id("evl"))

    async def restore(
        self, workspace_id: str, case: RegressionEvalCase, scenario: ScenarioSelection
    ) -> Observation:
        """§24.3 step D: restore the fixture *through the adapter*.

        Never by writing the target's storage directly. §9.1 makes the adapter
        the only way in, and a runner that reached around it would restore a
        state the target's own code never produced — which is precisely the
        class of difference a regression case exists to detect.

        **The fixture is verified, not injected.** An adapter declares what
        fixture content it accepts; the Buggy Store accepts none and reseeds to
        its own canonical empty state (003). So the reseed happens through
        `prepare`, and the resulting observation is then checked against the
        fixture the case recorded. A mismatch fails the run rather than
        replaying from a starting point the case does not describe — the
        alternative is a replay whose result belongs to a different journey.
        """
        try:
            await self._adapter.prepare(workspace_id, {}, scenario)
        except Exception as refused:
            # An adapter that cannot restore the fixture cannot run the case.
            # Reported as an invalid definition rather than a target failure:
            # nothing about the target was learned.
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                f"the target adapter could not restore this case's fixture: "
                f"{type(refused).__name__}",
            ) from refused

        observed = await self._adapter.observation_provider().capture(workspace_id)
        self._require_matching_fixture(case, observed)
        return observed

    def _require_matching_fixture(self, case: RegressionEvalCase, observed: Observation) -> None:
        """The restored state must be the one the case recorded.

        Compared on the paths the fixture carries rather than on the whole
        observation: a minimized fixture (§24.2 step 2) legitimately describes
        only part of the state, and demanding equality over everything would
        fail every minimized case.
        """
        payload = dict(observed.payload)
        for key, expected in case.fixture.target_state.items():
            if payload.get(key) != expected:
                raise ApiError(
                    ApiErrorCode.PRECONDITION_FAILED,
                    f"the restored target state does not match this case's fixture at "
                    f"{key!r}; the replay would start from a journey the case does not "
                    "describe",
                )

    async def replay(
        self,
        workspace_id: str,
        case: RegressionEvalCase,
        *,
        eval_run_id: str,
        before: Observation,
        consent: Any,
        work_factory: Callable[[], Any],
    ) -> ReplayOutcome:
        """§24.3 step E: replay the allowlisted trajectory, recording events.

        Each step is dispatched under `EventActor.EVAL`. AC-15 requires a
        replayed run to classify identically to its source, and 005's engine
        already counts `eval` alongside `agent` for exactly that reason — so
        nothing here adjusts the classifier, and if it needed to, that would be
        a finding rather than a patch.

        A case's own trajectory, handed to the shared step loop. 008's imported
        trajectories go through the same loop with a different source, so the
        allowlist check, the consent gate, and the per-call observation cannot
        drift apart between the two callers.
        """
        return await self.replay_steps(
            workspace_id,
            case.trajectory,
            identity=case.id,
            surface=case.surface,
            eval_run_id=eval_run_id,
            before=before,
            consent=consent,
            work_factory=work_factory,
        )

    async def replay_steps(
        self,
        workspace_id: str,
        steps: Sequence[TrajectoryStep],
        *,
        identity: str,
        surface: Any = None,
        eval_run_id: str,
        before: Observation,
        consent: Any,
        work_factory: Callable[[], Any],
    ) -> ReplayOutcome:
        """Replay an allowlisted trajectory, whatever recorded it.

        Extracted from `replay` when 008 needed to replay an *imported* trial's
        trajectory (FR-091). Both callers share one loop deliberately: the
        allowlist refusal, the deterministic consent gate, and the observation
        after every step are the safety properties, and two copies of them are
        two things to keep in step.
        """
        allowlisted = {spec.name for spec in self._adapter.tool_specs()}
        effect_paths = {spec.name: tuple(spec.effect_paths) for spec in self._adapter.tool_specs()}
        events: list[RunEvent] = []
        running_before = before
        replayed: list[TrajectoryStep] = []
        stopped_at: int | None = None
        detail = ""

        for step in steps:
            if step.tool not in allowlisted:
                # FR-086. Refused rather than skipped: skipping would replay a
                # different journey and report its outcome as this case's.
                raise ApiError(
                    ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                    f"step {step.sequence} names {step.tool!r}, which this adapter does "
                    "not publish; the case cannot be replayed as written",
                )

            correlation = f"{identity}-{step.sequence}"
            granted = await consent.grant_for(step, correlation)

            started = self._clock()
            try:
                result = await self._adapter.execute(
                    workspace_id,
                    step.tool,
                    dict(step.arguments),
                    ExecutionContext(
                        workspace_id=workspace_id,
                        run_id=identity,
                        invocation_id=self._id_source(),
                        request_id=_request_id(step, identity),
                        correlation_id=correlation,
                        idempotency_key=_request_id(step, identity),
                        actor=EventActor.EVAL,
                        human_consent_granted=granted,
                    ),
                )
            except Exception as failure:
                # A step that could not execute stops the replay. Continuing
                # would evaluate a journey the case does not describe.
                stopped_at = step.sequence
                detail = f"step {step.sequence} ({step.tool}) failed: {type(failure).__name__}"
                break

            # Observed immediately, per call. This is the evidence FR-055
            # attributes a false success from, and capturing it only at the end
            # would leave every step blamed for the final state.
            observed_after = await self._observe_or_none(workspace_id)
            events.append(
                _run_event(
                    step,
                    correlation=correlation,
                    result=result,
                    before=running_before,
                    after=observed_after,
                    effect_paths=effect_paths.get(step.tool, ()),
                    now=self._clock(),
                )
            )
            running_before = observed_after or running_before

            replayed.append(step)
            await self._record(
                work_factory,
                eval_run_id,
                step,
                correlation=correlation,
                result=result,
                duration_ms=max(0, int((self._clock() - started).total_seconds() * 1000)),
            )

        after = await self._observe_or_none(workspace_id)
        return ReplayOutcome(
            before=before,
            after=after,
            steps=tuple(replayed),
            # §24.3a: the recorded surface joins the replayed timeline as
            # events, because a headless replay cannot regenerate it and a
            # `tool_surface_poisoned` case "could never reproduce its own
            # classification and would fail permanently" without it.
            events=(
                *events,
                *surface_events(surface, step_count=len(steps), now=self._clock()),
            ),
            stopped_at=stopped_at,
            detail=detail,
        )

    async def _observe_or_none(self, workspace_id: str) -> Observation | None:
        """The final state, or nothing.

        An unavailable observation is not a pass: the caller turns it into an
        explicit non-pass, the same rule 005's verification follows.
        """
        try:
            return await self._adapter.observation_provider().capture(workspace_id)
        except Exception:
            return None

    async def _record(
        self,
        work_factory: Callable[[], Any],
        eval_run_id: str,
        step: TrajectoryStep,
        *,
        correlation: str,
        result: Any,
        duration_ms: int,
    ) -> None:
        """Append one `evaluation_events` row.

        A separate table from `events` on purpose: §16.1 says eval events
        "belong only to their `evaluation_run_id`; they never appear in the
        source outcome run", and sharing one table would put a replay's
        evidence inside the timeline it was cut from.
        """
        async with work_factory() as work:
            await _append_eval_event(
                work,
                run_id=eval_run_id,
                sequence=step.sequence,
                event_type=str(OutcomeEventType.TOOL_INVOCATION_COMPLETED.value),
                tool_name=step.tool,
                correlation_id=correlation,
                payload={
                    "arguments": dict(step.arguments),
                    "reported": {
                        "status": (
                            None
                            if result.reported_status is None
                            else str(result.reported_status.value)
                        ),
                        "summary": result.reported_summary,
                    },
                },
                duration_ms=duration_ms,
            )


def _request_id(step: TrajectoryStep, case_id: str) -> str:
    """The request id a replayed call uses.

    The recorded one when the case carries it, because §24.2 step 4 preserves
    repeated request ids specifically so an idempotency failure reproduces —
    inventing a fresh one here would make every replayed retry look like a
    first attempt and the bug would vanish.
    """
    recorded = step.arguments.get("request_id")
    if isinstance(recorded, str) and recorded:
        return recorded
    return f"req_{case_id[:20]}_{step.sequence}"


async def _append_eval_event(
    work: UnitOfWork,
    *,
    run_id: str,
    sequence: int,
    event_type: str,
    tool_name: str | None,
    correlation_id: str | None,
    payload: Mapping[str, Any],
    duration_ms: int | None = None,
    status: str | None = None,
) -> None:
    await work.execute(
        """
        INSERT INTO evaluation_events (
            id, evaluation_run_id, sequence_number, event_type, actor, tool_name,
            correlation_id, request_id, redacted_payload_json, status, duration_ms, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id("eve"),
            run_id,
            sequence,
            event_type,
            str(EventActor.EVAL.value),
            tool_name,
            correlation_id,
            None,
            json.dumps(dict(payload), sort_keys=True),
            status,
            duration_ms,
            work.now(),
        ),
    )


def surface_events(surface: Any, *, step_count: int, now: datetime) -> tuple[RunEvent, ...]:
    """§24.3a's `surface` section, replayed as the events it was recorded from.

    A case with no surface section produces nothing, and that is the honest
    result: `stable_tool_surface` then evaluates as `observation_unavailable`
    exactly as it does on a live run with no baseline (§16.1), rather than
    reading as satisfied.

    **Sequence numbers.** The baseline is 0 — it is captured at arming, before
    any step. Deltas are numbered after the trajectory rather than interleaved
    into it, so no two events share a number, and each carries the step
    sequence it was *recorded* against in its payload. Inventing a position in
    the trajectory for a delta would be asserting an ordering the recording
    does not contain.
    """
    if surface is None:
        return ()

    baseline = RunEvent(
        sequence_number=0,
        event_type=OutcomeEventType.TOOL_SURFACE_CAPTURED,
        actor=EventActor.EVAL,
        created_at=now,
        redacted_payload={"tools": list(surface.baseline)},
    )
    deltas = tuple(
        RunEvent(
            sequence_number=step_count + offset,
            event_type=OutcomeEventType.TOOL_SURFACE_CHANGED,
            actor=EventActor.EVAL,
            created_at=now,
            tool_name=delta.tool,
            # The same payload shape a live capture writes (014-T1), so one core
            # reader serves both. A replayed case carries no before/after
            # definitions — §24.3a never recorded them — and the policy does not
            # need them to classify; FR-169's side-by-side diff is evidence for a
            # human, and a replay's evidence is the case it came from.
            redacted_payload={
                "kind": delta.kind,
                "namespace": delta.partition,
                "tool_name": delta.tool or "",
                "before": None,
                "after": None,
                "recorded_sequence": delta.sequence,
            },
        )
        for offset, delta in enumerate(surface.deltas, start=1)
    )
    return (baseline, *deltas)


def _run_event(
    step: TrajectoryStep,
    *,
    correlation: str,
    result: Any,
    before: Observation,
    after: Observation | None,
    effect_paths: tuple[Any, ...],
    now: datetime,
) -> RunEvent:
    """One replayed call, in the shape the engine reads.

    Carries `post_call_effect_state` for the same reason 005's live invocation
    does: FR-055 attributes a false success to "the last relevant
    intended-effect action and its immediate authoritative post-call evidence".
    A timeline without it cannot produce the classification the case expects, so
    the eval would fail while the target behaved exactly as recorded.

    The reported status is the tool's own claim and the state hashes are the
    independent observation — kept in separate fields here for the same reason
    they are separate columns in `events`.
    """
    return RunEvent(
        sequence_number=step.sequence,
        event_type=OutcomeEventType.TOOL_INVOCATION_COMPLETED,
        actor=EventActor.EVAL,
        created_at=now,
        tool_name=step.tool,
        correlation_id=correlation,
        request_id=result.request_id,
        reported_status=result.reported_status,
        state_version_before=before.state_version,
        state_version_after=None if after is None else after.state_version,
        state_hash_before=before.content_hash(),
        state_hash_after=None if after is None else after.content_hash(),
        redacted_payload={"arguments": dict(step.arguments)},
        post_call_effect_state=effect_context(
            list(effect_paths), None if after is None else after.as_context()
        ),
    )
