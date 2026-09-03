"""Verification: final observation, evaluation, findings, terminal transition.

FR-041: "The selected observation provider shall capture authoritative target
state immediately before verification." FR-053 decides the outcome. §16.1 fixes
the events. §23.1 owns the report, which is a separate task.

**The core owns the verdict; this owns the I/O.** Every judgement below comes
from `actionwitness_core.engine` — assertions, the trajectory check, policies,
and the aggregation into a layer result. Nothing here decides whether a check
passed, and nothing re-derives a verdict the core already produced. That split
is what makes the verdict replayable: the same evidence through the same pure
functions gives the same answer on any machine, which is what §24 rests on.

The sequencing repeats the shape the rest of this milestone uses, for the same
reason. The gate runs in its own transaction (FR-038), the final observation is
I/O and therefore holds nothing, and the whole verdict — snapshot, findings,
events, terminal status — commits together. A run that recorded findings but
never reached a terminal state, or reached one without its findings, would be a
report that disagrees with its own evidence.

**Nothing is held across the capture, so the run can move underneath it.** That
window is deliberate (ADR-0003 forbids holding a lock across I/O) and it has two
consequences this module has to answer for rather than assume away.

A workspace reset is legal from every state (§16) and cancels every non-terminal
run, so a reset landing in the window commits `cancelled` while this task is
still holding an in-memory verdict. §16 permits only `reset` out of `cancelled`,
so the seal re-reads the run and routes the move through
`validate_run_transition` before writing anything — and its terminal `UPDATE`
carries `AND status = 'verifying'` so the guarantee is the database's, not this
function's. When the seal loses that race the cancellation stands and the caller
is refused: an operator who cancelled a run must not find a verdict on it.

And the capture itself can fail. Constitution §5: "observation failure produces
an explicit non-pass result; it never degrades to success" — which is not
satisfied by letting the exception escape, because the gate has already
committed `verifying` and every retry then loses to FR-038's own rejection. So
the failure is caught and the run is taken to `error` carrying §22's
`observation_unavailable`, which is a state an operator can read and act on.

**Evaluation reads the stored evidence, not live state.** The events fed to the
trajectory and policy engines are read back out of the database and rebuilt into
the core's `RunEvent` models. FR-050 defines policy determinism over "the same
snapshots and the same recorded event stream" — so evaluating against anything
the timeline does not hold would produce a verdict a replay could not reach.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from actionwitness_core.contracts.enums import AssertionSeverity, PolicyType
from actionwitness_core.contracts.models import OutcomeContract, parse_contract
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.assertions import evaluate_assertions
from actionwitness_core.engine.classification import (
    classify_assertion_failures,
    execution_findings,
    tool_execution_layer,
)
from actionwitness_core.engine.diff import StateChange, changed_paths_of, diff_states
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.engine.findings import Finding, aggregate, primary_failure
from actionwitness_core.engine.policies import (
    PolicyEvidence,
    declared_contract_paths,
    evaluate_policies,
    identity_mismatches,
    surface_evidence,
)
from actionwitness_core.engine.trajectory import evaluate_expected_tools
from actionwitness_core.evidence.effects import redacted_observation
from actionwitness_core.evidence.models import RunEvent
from actionwitness_core.journeys.enums import (
    EventActor,
    OutcomeEventType,
    RunState,
    SnapshotPhase,
)
from actionwitness_core.journeys.guidance import GuidanceState, derive_guidance, phase_for
from actionwitness_core.journeys.transitions import validate_run_transition
from actionwitness_core.kernel import TransitionError
from actionwitness_core.ports.models import Observation
from actionwitness_core.reports.enums import LayerResult, RunMode
from actionwitness_core.reports.models import (
    ContractReference,
    ExternalTargetReference,
    GuidanceReference,
    OutcomeReport,
    ScenarioReference,
    TargetReference,
    UndeclaredChangesBlock,
    compose_outcome_report,
    recorded_warnings,
    undeclared_changes_from,
)
from actionwitness_core.security.redaction import RedactionPolicy

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry, TargetUnavailable
from actionwitness_service.application.artifacts import OUTCOME_REPORT, ArtifactStore
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.application.comparison_service import (
    ComparisonService,
    comparable_run,
)
from actionwitness_service.application.guidance_service import GuidanceRecorder
from actionwitness_service.application.self_witness import capture_scoped
from actionwitness_service.application.verification_gate import VerificationGate
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.locks import WorkspaceLocks
from actionwitness_service.persistence.repositories import (
    EventRepository,
    FindingRepository,
    SnapshotRepository,
)

__all__ = ["VerificationOutcome", "VerificationService"]

#: §16's terminal states, keyed by the layer result that produces each (FR-053).
_TERMINAL_STATE: Mapping[LayerResult, RunState] = {
    LayerResult.PASSED: RunState.PASSED,
    LayerResult.PASSED_WITH_WARNINGS: RunState.PASSED_WITH_WARNINGS,
    LayerResult.FAILED: RunState.FAILED,
}


def _with_recorded_warnings(result: LayerResult, findings: Sequence[Finding]) -> LayerResult:
    """Let a check that *held* but recorded a warning move the run's state.

    `aggregate` decides from failures alone, which is right for every layer
    result it feeds. But §9.5's `description_change` is deliberately a warning on
    a **passing** check, so a run whose only news was a warning aggregated to
    `passed` while §23.1's report — which counts recorded warnings — resolved to
    `passed_with_warnings`. The row and the artifact then disagreed about the
    same run, which is the one disagreement this module is arranged to prevent.

    Reuses `recorded_warnings`, the same reader the report's own counts use, so
    the two cannot drift apart by being computed from different rules. A warning
    can only ever move `passed` — it must not soften a failure, and §16's
    terminal set has nowhere else for it to go.
    """
    if result is not LayerResult.PASSED:
        return result
    if any(recorded_warnings(finding) for finding in findings):
        return LayerResult.PASSED_WITH_WARNINGS
    return result


#: How an `ObservationProvider` actually fails (`actionwitness_core.ports`).
#:
#: The protocol declares `capture` and no exception type, so this is the set its
#: implementations can raise, named rather than swept up by `except Exception`:
#:
#: * `TargetUnavailable` — the registry no longer serves this run's adapter, so
#:   there is no provider left to ask.
#: * `httpx.HTTPError` — every transport, timeout, and non-2xx failure of a
#:   provider that reads its target over HTTP, which both shipped providers do.
#: * `ValueError` — a provider that received a partial or malformed state
#:   document and refused to manufacture an observation from it. Covers
#:   `json.JSONDecodeError` and Pydantic's `ValidationError`, which are both
#:   `ValueError` subclasses, so a payload that will not construct an
#:   `Observation` lands here too.
#: * `OSError` — a socket- or file-level failure beneath a transport that does
#:   not wrap it.
#:
#: `asyncio.CancelledError` is deliberately absent, and absent for free: it is a
#: `BaseException` in 3.12, so a cancelled verification propagates rather than
#: being recorded as an unobservable target (constitution §5 — cancellation
#: propagates through I/O).
_OBSERVATION_FAILURES: Final[tuple[type[BaseException], ...]] = (
    TargetUnavailable,
    httpx.HTTPError,
    ValueError,
    OSError,
)

#: The check id the final-observation failure is recorded under. Not a contract
#: term, so it cannot collide with an assertion id or a policy type — which is
#: what keeps §22's `check_id` tie-break total across the whole finding set.
_FINAL_OBSERVATION_CHECK_ID: Final = "final_observation"

#: What the operator is told when the final observation could not be taken. A
#: fixed sentence rather than the exception's text: §15.8 keeps internal detail
#: out of anything a browser tool reads, and this string is persisted as finding
#: evidence, which is exactly where an adapter's message would leak a host, a
#: path, or a credential in a URL (constitution §4).
_UNOBSERVABLE_REASON: Final = (
    "the authoritative observation provider could not supply the final state, so no "
    "verdict rests on it"
)


@dataclass(frozen=True)
class VerificationOutcome:
    run_id: str
    status: str
    overall_result: str
    findings: tuple[Finding, ...]
    primary_failure_check_id: str | None
    final_state_version: str | None
    report: OutcomeReport
    next_action: Mapping[str, object]


class VerificationService:
    """Captures final state, evaluates, and seals the run."""

    def __init__(
        self,
        database: Database,
        # `None` is a real caller: the Shopify path passes it because an
        # `external_webmcp` run has no adapter to capture through (§9.1), and
        # `verify` skips every registry lookup when `external_observation` is
        # given. The runtime already accepted it; only this annotation was
        # narrower than the behaviour, which made every honest caller a type error.
        registry: AdapterRegistry | None,
        locks: WorkspaceLocks,
        artifacts: ArtifactStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._locks = locks
        self._artifacts = artifacts
        self._clock = clock or (lambda: datetime.now(UTC))

    async def verify(
        self,
        workspace_id: str,
        run_id: str,
        *,
        external_observation: Observation | None = None,
        external_target: ExternalTargetReference | None = None,
        on_seal: Callable[[UnitOfWork, LayerResult], Awaitable[None]] | None = None,
    ) -> VerificationOutcome:
        """FR-038's gate, FR-041's capture, FR-053's verdict, in that order.

        **`external_observation` is §16's `external_webmcp` exception, and only
        that.** A Shopify trial's final state is read inside the shopper's own
        browser session and arrives in the request body; there is no adapter for
        this service to capture through, because §9.1 forbids the harness from
        impersonating an external target's tools "through a second
        implementation". So the capture step is skipped rather than faked — and
        it is skipped *with a value already in hand*, which is the difference
        between an observation that came from somewhere and one that was not
        taken. Everything after it — the core's evaluation, the report, the seal
        — is identical, because the verdict must not depend on how the evidence
        reached the server.

        **`on_seal` runs inside the sealing transaction.** §16.5 requires a
        pairing's terminal state and its run's to "commit together, or neither",
        and a caller that updated its own row after this method returned would
        satisfy the words while leaving exactly the mismatch the clause forbids
        if the process died in between.
        """
        if (external_observation is None) is not (external_target is None):
            raise ValueError(
                "external observation and external target provenance must be provided together"
            )

        # 1 — win the race, or lose it before anything is observed.
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            run = await VerificationGate(work, workspace_id).begin(run_id)
            contract, policy = await self._contract_of(work, run)
            events = await self._timeline(work, run_id)
            initial = await self._initial_context(work, run_id)

        # 2 — FR-041's capture. I/O, so nothing is held (ADR-0003).
        #
        # Wrapped because the gate has already committed `verifying`. An escaping
        # exception would leave the run there permanently: every retry loses to
        # FR-038's `RUN_ALREADY_VERIFYING` and only a reset would free it, which
        # discards the run's evidence to recover from the harness's own failure.
        # Constitution §5 asks for the opposite — an observation failure is "an
        # explicit non-pass result", and a state nobody can leave is not explicit.
        #
        # The external branch does no I/O and cannot fail this way: its
        # observation was validated and redacted before the gate was even
        # claimed, so there is nothing here to catch.
        adapter: Any = None
        if external_observation is not None:
            final = external_observation
        else:
            try:
                adapter = self._registry.adapter(str(run["target_adapter_id"]))
                final = await self._capture(adapter, workspace_id, policy)
            except _OBSERVATION_FAILURES as failure:
                await self._abandon_unobservable(workspace_id, run_id)
                raise ApiError(
                    ApiErrorCode.TARGET_UNAVAILABLE,
                    "The target could not be observed, so this run has no verdict. It is "
                    "recorded as `error` with an `observation_unavailable` finding; reset "
                    "the workspace and arm again once the target answers.",
                ) from failure

        # 3 — the core decides. Pure, and given only recorded evidence.
        evaluation = _evaluate(contract, adapter, events, initial=initial, final=final.as_context())
        findings = evaluation.all()
        result = _with_recorded_warnings(aggregate(findings), findings)
        terminal = _TERMINAL_STATE[result]
        guidance = derive_guidance(
            phase_for(has_contract=True, run_state=terminal), correlation_id=run_id
        )

        # §23.1's report, derived from the evidence just evaluated. Composed
        # before the seal so the artifact and the verdict describe the same
        # moment, and written to disk here because file I/O must not happen
        # inside the transaction (ADR-0003).
        report = _compose(
            run_id,
            run,
            contract,
            evaluation,
            events,
            guidance,
            external_target=external_target,
        )
        written = self._artifacts.write(
            workspace_id,
            run_id,
            report.as_stored_document(),
            artifact_type=OUTCOME_REPORT,
            schema_version=report.schema_version,
        )

        # 4 — the whole verdict commits together.
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            await self._seal(work, workspace_id, run_id, run, final, findings, result)
            await self._artifacts.record(
                work,
                workspace_id,
                run_id,
                written,
                metadata={"overall_result": str(result.value)},
            )
            # §16.5's other terminal state, in the same transaction as this run's
            # and the artifact row that is the report reference. Last, so it
            # cannot commit over a seal that was itself refused.
            if on_seal is not None:
                await on_seal(work, result)

        return VerificationOutcome(
            run_id=run_id,
            status=str(terminal.value),
            overall_result=str(result.value),
            findings=findings,
            primary_failure_check_id=_primary_check_id(findings),
            final_state_version=final.state_version,
            report=report,
            next_action=guidance.next_action(),
        )

    # -- reading the recorded evidence ---------------------------------------

    async def _contract_of(
        self, work: UnitOfWork, run: Mapping[str, Any]
    ) -> tuple[OutcomeContract, RedactionPolicy]:
        """The contract this run was armed against, by its own identifier.

        FR-025 locks it, so this reads the run's `contract_id` rather than the
        workspace's current selection — a run must be judged by the contract it
        was armed with even if the workspace has moved on.
        """
        contract_id = run.get("contract_id")
        if not contract_id:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                "This run was armed without a contract, so there is nothing to verify it against.",
            )
        row = await work.fetch_one(
            "SELECT document_json FROM contracts WHERE id = ?", (str(contract_id),)
        )
        if row is None:  # pragma: no cover - contracts are insert-only
            raise ApiError(ApiErrorCode.HARNESS_ERROR, "The armed contract is missing.")
        document = json.loads(row["document_json"])
        paths = ((document.get("redaction") or {}).get("paths")) or []
        return parse_contract(document), RedactionPolicy.from_paths([str(path) for path in paths])

    async def _timeline(self, work: UnitOfWork, run_id: str) -> tuple[RunEvent, ...]:
        """Rebuild the core's event models from the stored rows.

        FR-050 defines determinism over "the same recorded event stream", so the
        engine is given what the timeline holds rather than anything held in
        memory from the requests that wrote it.
        """
        rows = await work.fetch_all(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence_number", (run_id,)
        )
        return tuple(_run_event(dict(row)) for row in rows)

    async def _initial_context(self, work: UnitOfWork, run_id: str) -> Mapping[str, Any] | None:
        """The `before` snapshot as an evaluation context, or `None`.

        `None` propagates to `observation_unavailable` rather than being
        replaced by an empty context, which would make every `absent` assertion
        pass against a target nobody could see (constitution §5).
        """
        observation = await SnapshotRepository(work).get(run_id, SnapshotPhase.BEFORE)
        return None if observation is None else observation.as_context()

    async def _capture(
        self, adapter: Any, workspace_id: str, policy: RedactionPolicy
    ) -> Observation:
        """FR-041's final observation, redacted before it is hashed or stored."""
        observation = await capture_scoped(self._database, adapter, workspace_id)
        return redacted_observation(observation, policy)

    # -- writing an explicit non-pass ----------------------------------------

    async def _abandon_unobservable(self, workspace_id: str, run_id: str) -> None:
        """Take a run whose final observation failed to `error` (§22, §16).

        **`observation_unavailable`, not `harness_error`.** §22 distinguishes
        them by whose failure it was: `observation_unavailable` is "required
        state provider could not supply a value", and `harness_error` is "the
        harness itself failed to complete verification". Everything caught here
        came out of the provider or the transport underneath it — the harness
        asked correctly and got nothing back — so blaming the harness would
        misdirect whoever reads the finding, and would make the one
        classification that means "we could not see the target" unreachable for
        the case it was written for. `harness_error` stays for a failure in this
        service's own work, which is what the generic 500 path already reports.
        `error` is the run state because §16 offers `verifying` no other
        non-verdict exit, and a verdict is precisely what is not available.

        Writes in the same shape the seal does — one transaction, under the
        workspace lock — so the finding, the events, and the terminal status
        arrive together or not at all. The run is re-checked first: a reset may
        have cancelled it while the capture was failing, and a cancellation
        outranks this.
        """
        finding = _unobservable_finding()
        guidance = derive_guidance(
            phase_for(has_contract=True, run_state=RunState.ERROR), correlation_id=run_id
        )

        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            await _require_transition_still_legal(work, workspace_id, run_id, RunState.ERROR)

            events = EventRepository(work)
            await events.append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.VERIFICATION_STARTED.value),
                    "actor": str(EventActor.HARNESS.value),
                },
            )
            # No `snapshot_captured`: there is no snapshot. §16.1's per-check
            # event still fires, because a finding nobody can find on the
            # timeline is a finding a reader has to already know to look for.
            await events.append(
                run_id,
                {
                    "event_type": str(_check_event(finding).value),
                    "actor": str(EventActor.HARNESS.value),
                    "status": str(finding.status.value),
                    "redacted_payload": {
                        "check_id": finding.check_id,
                        "check_type": str(finding.check_type.value),
                        "severity": str(finding.severity.value),
                        "classification": str(FailureClassification.OBSERVATION_UNAVAILABLE.value),
                    },
                },
            )
            await FindingRepository(work).add_all(run_id, [_finding_row(finding)])

            # `overall_result` stays NULL, as it does on FR-008's ceiling trip:
            # the column carries a business verdict and this run reached none.
            # Writing one here — even "failed" — would say the contract was
            # judged and lost, when it was never judged at all.
            updated = await work.execute(
                "UPDATE runs SET status = ?, completed_at = ? "
                "WHERE id = ? AND workspace_id = ? AND status = ?",
                (
                    str(RunState.ERROR.value),
                    work.now(),
                    run_id,
                    workspace_id,
                    str(RunState.VERIFYING.value),
                ),
            )
            if updated.rowcount == 0:
                raise _verification_overtaken(None)

            await events.append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.VERIFICATION_COMPLETED.value),
                    "actor": str(EventActor.HARNESS.value),
                    "status": str(RunState.ERROR.value),
                    "redacted_payload": {
                        "classification": str(FailureClassification.OBSERVATION_UNAVAILABLE.value),
                        "reason": _UNOBSERVABLE_REASON,
                    },
                },
            )
            await GuidanceRecorder(work, workspace_id).transition(guidance, run_id=run_id)

    # -- writing the verdict -------------------------------------------------

    async def _seal(
        self,
        work: UnitOfWork,
        workspace_id: str,
        run_id: str,
        run: Mapping[str, Any],
        final: Observation,
        findings: Sequence[Finding],
        result: LayerResult,
    ) -> None:
        """Snapshot, findings, events, and the terminal transition, together.

        The run is re-read and the move re-validated before anything is written.
        The seal began from a `verifying` run, but it began *before* the capture
        and the report write, and nothing was held across those (ADR-0003). A
        workspace reset is legal from every state (§16) and cancels every
        non-terminal run, so a reset that landed in that window has already
        committed `cancelled` — out of which §16 permits only `reset`. Sealing
        over the top of it would resurrect a run the operator ended and produce a
        timeline reading `run_cancelled`, then `verification_started`.
        """
        terminal = _TERMINAL_STATE[result]
        await _require_transition_still_legal(work, workspace_id, run_id, terminal)

        events = EventRepository(work)
        await events.append(
            run_id,
            {
                "event_type": str(OutcomeEventType.VERIFICATION_STARTED.value),
                "actor": str(EventActor.HARNESS.value),
            },
        )

        await SnapshotRepository(work).add(run_id, SnapshotPhase.AFTER, final)
        await events.append(
            run_id,
            {
                "event_type": str(OutcomeEventType.SNAPSHOT_CAPTURED.value),
                "actor": str(EventActor.HARNESS.value),
                "state_version_after": final.state_version,
                "state_hash_after": final.content_hash(),
                "redacted_payload": {
                    "phase": str(SnapshotPhase.AFTER.value),
                    "provider": final.provider_id,
                },
            },
        )

        # §16.1: "one contract assertion produced a result", one event each. The
        # budget for these was reserved at every invocation start (FR-008), so
        # they cannot push the run past its ceiling here.
        for finding in findings:
            await events.append(
                run_id,
                {
                    "event_type": str(_check_event(finding).value),
                    "actor": str(EventActor.HARNESS.value),
                    "status": str(finding.status.value),
                    "redacted_payload": {
                        "check_id": finding.check_id,
                        "check_type": str(finding.check_type.value),
                        "severity": str(finding.severity.value),
                        "classification": (
                            None
                            if finding.classification is None
                            else str(finding.classification.value)
                        ),
                    },
                },
            )

        await FindingRepository(work).add_all(run_id, [_finding_row(f) for f in findings])

        # §17.1: `comparison_key_hash` is "nullable until the run is terminal".
        # Computed here from the controlled inputs the run copied in at arming,
        # so two runs configured identically carry the same key and a reader can
        # recompute it from the stored columns (FR-019).
        comparison_key = comparable_run(
            {**dict(run), "status": str(terminal.value)},
            trajectory=await ComparisonService(work, workspace_id).trajectory(run_id),
        ).comparison_key()
        # `AND status = 'verifying'` makes the guard above the database's rather
        # than this function's. The re-read cannot be stale inside this
        # transaction, but a conditional write is what survives a future caller
        # that reaches the seal by another route — an unconditional UPDATE is
        # how the verdict overwrote a cancellation in the first place.
        updated = await work.execute(
            "UPDATE runs SET status = ?, overall_result = ?, completed_at = ?, "
            "comparison_key_hash = ? WHERE id = ? AND workspace_id = ? AND status = ?",
            (
                str(terminal.value),
                str(result.value),
                work.now(),
                comparison_key,
                run_id,
                workspace_id,
                str(RunState.VERIFYING.value),
            ),
        )
        if updated.rowcount == 0:
            raise _verification_overtaken(None)
        # `active_run_id` is deliberately *not* cleared. §11.5's diagram keeps a
        # workspace in `Passed`/`PassedWarnings`/`Failed` — showing that run's
        # findings — and leaves those states only by reset, which is where
        # FR-013 clears the pointer. Clearing it here made the verify response
        # say "review the findings" while `GET /workspace` said "arm a run":
        # two answers to whose turn it is, which is the one thing FR-120
        # forbids. A terminal run does not block arming another, because the
        # lease only counts non-terminal states.

        await events.append(
            run_id,
            {
                "event_type": str(OutcomeEventType.VERIFICATION_COMPLETED.value),
                "actor": str(EventActor.HARNESS.value),
                "status": str(result.value),
                "redacted_payload": {
                    "overall_result": str(result.value),
                    "primary_failure": _primary_check_id(findings),
                },
            },
        )
        # Derived after the terminal transition and the cleared active-run
        # pointer, so the recorded handoff describes the workspace the caller
        # is about to see rather than the one it had a moment ago.
        await GuidanceRecorder(work, workspace_id).transition(
            derive_guidance(
                phase_for(has_contract=True, run_state=terminal), correlation_id=run_id
            ),
            run_id=run_id,
        )


async def _require_transition_still_legal(
    work: UnitOfWork, workspace_id: str, run_id: str, target: RunState
) -> None:
    """Refuse to write a terminal state §16 no longer permits for this run.

    The run is read through `WorkspaceScope`, so it is this workspace's run or it
    is nothing (FR-006), and the move is judged by `validate_run_transition` —
    the one authority on §16's table, and the check every other transition writer
    already goes through. Deciding here instead would be a second opinion, and
    the two would drift.
    """
    row = await WorkspaceScope(work, workspace_id).run(run_id)
    current = RunState(str(row["status"]))
    try:
        validate_run_transition(current, target)
    except TransitionError as invalid:
        raise _verification_overtaken(current) from invalid


def _verification_overtaken(current: RunState | None) -> ApiError:
    """The refusal when the run moved out of `verifying` under a verification.

    `RUN_IN_PROGRESS` because `api/errors.py` already maps the core's
    `INVALID_STATE_TRANSITION` onto it, quoting §16's "invalid non-reset state
    transitions shall return HTTP 409" — so this is the code a client already
    branches on for an illegal run transition, and allocating a second one would
    fork that vocabulary. It is 409 and not retryable, which is the honest
    answer: repeating the request cannot succeed, because the run this verdict
    belonged to no longer exists in a state that can receive one.

    The cancellation stands. Nothing in this transaction is written, so the run
    keeps the terminal state the reset gave it and the verdict is discarded
    rather than applied — an operator who cancelled a run must not come back to
    find it passed.
    """
    state = "no longer verifying" if current is None else f"now {current.value}"
    return ApiError(
        ApiErrorCode.RUN_IN_PROGRESS,
        f"This run is {state}, so the verification that was in flight has no run to "
        "seal. Its result was discarded and the run keeps the state it was moved to; "
        "arm a new run to verify again.",
        details=[{"path": "run.status", "message": "the run left `verifying` mid-verification"}],
    )


def _unobservable_finding() -> Finding:
    """§22's `observation_unavailable`, as a finding an operator can read.

    Critical and `observation_unavailable` rather than `failed`: §16.1 and the
    `CheckStatus` vocabulary both keep "the check did not hold" apart from "the
    evidence never arrived", and `aggregate` counts the second as failing
    anyway, so the run can never read as passed on it.
    """
    return Finding(
        check_id=_FINAL_OBSERVATION_CHECK_ID,
        check_type=CheckType.POLICY,
        status=CheckStatus.OBSERVATION_UNAVAILABLE,
        severity=AssertionSeverity.CRITICAL,
        classification=FailureClassification.OBSERVATION_UNAVAILABLE,
        evidence={"reason": _UNOBSERVABLE_REASON},
    )


@dataclass(frozen=True)
class Evaluation:
    """The findings, kept in the groups §23.1's layers are drawn from.

    Separate rather than one flat tuple because the layers are *not* the same
    question: `business_outcome` aggregates assertions only, `safety_policy`
    aggregates policies only, and `observed_trajectory` is one finding. Flatten
    them and a failing policy would drag the business outcome down with it,
    which is precisely the conflation §23.1's five layers exist to prevent.
    """

    assertions: tuple[Finding, ...]
    trajectory: Finding
    policies: tuple[Finding, ...]
    #: §22's `tool_execution_error`, one per unexpected invocation failure.
    #: Named separately because it answers §23.1's `tool_execution` question —
    #: did the calls themselves work — rather than whether a contract term held,
    #: and because no contract declares it: it is derived from the timeline. See
    #: `_compose` for why the report is nonetheless handed these alongside the
    #: policy findings.
    execution: tuple[Finding, ...] = ()
    #: FR-157's full-state diff, or `None` when a snapshot was missing and no
    #: diff could be computed. Carried rather than recomputed at report time,
    #: because a report derived from a second diff could disagree with the
    #: finding that judged the first one.
    changes: tuple[StateChange, ...] | None = None

    def all(self) -> tuple[Finding, ...]:
        """Every finding, for the run-level aggregate and for persistence.

        Execution findings belong here and not only in the `tool_execution`
        layer. §22 lists `tool_execution_error` among the classifications a run
        can carry, and a classification that never reaches a stored finding
        cannot be chosen as `primary_failure`, cannot be read through
        `get_run_findings`, and cannot enter a generated eval case's expected
        set — which is to say it exists in the vocabulary and nowhere else.

        No sort here on purpose. §22's total order — severity, then causal event
        sequence, then `check_id` — is `Finding.sort_key`, applied by
        `order_failures` and `primary_failure` wherever the order matters. Each
        execution finding carries the sequence number of the invocation that
        failed, which is what places it against the assertions that failure
        caused.
        """
        return (*self.assertions, self.trajectory, *self.policies, *self.execution)


def _evaluate(
    contract: OutcomeContract,
    adapter: Any,
    events: Sequence[RunEvent],
    *,
    initial: Mapping[str, Any] | None,
    final: Mapping[str, Any] | None,
) -> Evaluation:
    """Every layer the core owns, in one place and in contract order.

    Assembled here and decided there: this function chooses *what* to evaluate
    and the engine decides *how* each one turns out.
    """
    effect_map = _effect_map(adapter)

    # FR-055: a failed assertion is refined into a causal classification before
    # anything reads it. It has to happen here rather than at report time,
    # because §22 orders failures *by* classification — picking the primary
    # failure from unclassified findings would choose by the wrong key, and the
    # persisted findings would disagree with the report that summarises them.
    assertions = classify_assertion_failures(
        evaluate_assertions(contract.assertions, initial=initial, final=final),
        contract.assertions,
        events=events,
        effect_map=effect_map,
        initial=initial,
    )

    # FR-157: a complete recursive diff of the two canonical snapshots,
    # independent of which paths the contract names. `None` — not an empty tuple
    # — when a snapshot is missing: "nothing changed" and "we could not tell"
    # are different answers, and only the second may leave a policy
    # `not_evaluated` (§12.2, §16.1).
    changes = None if initial is None or final is None else diff_states(initial, final)
    baseline_recorded, observed_deltas = surface_evidence(events)
    mismatched_tools = identity_mismatches(events)

    return Evaluation(
        assertions=assertions,
        trajectory=evaluate_expected_tools(contract.expected_tools, events),
        policies=evaluate_policies(
            contract.policies,
            PolicyEvidence(
                events=tuple(events),
                effect_map=effect_map,
                contract_paths=declared_contract_paths(contract),
                changed_paths=None if changes is None else changed_paths_of(changes),
                # FR-159's "with redacted before and after values". The diff
                # already computed bounded excerpts either side of every changed
                # path; passing only the path names threw them away here, which
                # is why the finding could name a path but never say what it
                # changed from. Both snapshots were redacted before persistence
                # (�20.3), so these carry no value redaction would have removed.
                changes=changes,
                # FR-159's "with redacted before and after values". The diff
                # already computed bounded excerpts either side of every changed
                # path; passing only the path names threw them away here, which
                # is why the finding could name a path but never say what it
                # changed from. Both snapshots were redacted before persistence
                # (§20.3), so these carry no value redaction would have removed.
                # 014-T4. Read from the recorded timeline by the same core
                # function §24 replay uses, so a replayed run and its source
                # judge the same events the same way. Absent captures leave
                # `surface_baseline_recorded` false, which §16.1 requires to
                # fail closed rather than pass.
                surface_baseline_recorded=baseline_recorded,
                observed_surface_deltas=observed_deltas,
                identity_mismatches=mismatched_tools,
            ),
        ),
        # §22's `tool_execution_error`, from the same recorded timeline the
        # `tool_execution` layer is derived from. The layer says an invocation
        # failed; this is the finding that says *which* one, and it is the only
        # form of that fact a report, a comparison, or an eval expectation can
        # read (FR-033's safe blocks produce none, which the core decides).
        execution=execution_findings(events),
        changes=changes,
    )


def _effect_map(adapter: Any) -> Mapping[str, tuple[ObservationPath, ...]]:
    """§13.4's declared prefixes, parsed into the core's path type.

    Empty for an external target, which is passed as `None`: §9.1 says an
    `external_webmcp` target "runs its own tools", so the harness declares no
    effect map for tools it never dispatches. Empty is the honest value —
    `classify_assertion_failures` reads it to attribute a failure to a call, and
    there are no calls here to attribute one to.
    """
    if adapter is None:
        return {}
    return {
        tool: tuple(ObservationPath.parse(str(path)) for path in paths)
        for tool, paths in adapter.effect_map().items()
    }


def _undeclared_changes_block(evaluation: Evaluation) -> UndeclaredChangesBlock | None:
    """§23.1's `undeclared_changes`, or `None` when there is nothing to report.

    Two conditions, and both are about not overstating what is known. The policy
    has to be in the contract at all — a run that never asked about undeclared
    change should not carry a block implying it did — and the diff has to exist,
    because a block full of zeros is indistinguishable from "nothing changed"
    when the truth is that nothing was compared.
    """
    if evaluation.changes is None:
        return None
    finding = next(
        (
            candidate
            for candidate in evaluation.policies
            if candidate.check_id == PolicyType.NO_UNDECLARED_CHANGES.value
        ),
        None,
    )
    if finding is None or finding.status is CheckStatus.NOT_EVALUATED:
        return None
    return undeclared_changes_from(finding, changed_paths=len(evaluation.changes))


def _check_event(finding: Finding) -> OutcomeEventType:
    """§16.1's per-check event, chosen by what the finding is about."""
    if finding.check_type is CheckType.POLICY:
        return OutcomeEventType.POLICY_EVALUATED
    return OutcomeEventType.ASSERTION_EVALUATED


def _finding_row(finding: Finding) -> Mapping[str, Any]:
    """One finding, shaped for §17.1's `findings` table."""
    return {
        "check_id": finding.check_id,
        "check_type": str(finding.check_type.value),
        "classification": (
            None if finding.classification is None else str(finding.classification.value)
        ),
        "severity": str(finding.severity.value),
        "status": str(finding.status.value),
        "path": None if finding.path is None else str(finding.path),
        "paths": [str(path) for path in finding.paths],
        "applied_exemptions": [str(path) for path in finding.applied_exemptions],
        "attributed_cause": finding.attributed_cause,
        "expected": finding.expected,
        "actual": finding.actual,
        "evidence": dict(finding.evidence),
    }


def _primary_check_id(findings: Sequence[Finding]) -> str | None:
    primary = primary_failure(findings)
    return None if primary is None else primary.check_id


def _run_event(row: Mapping[str, Any]) -> RunEvent:
    """One stored row as the core's model.

    The payload's `post_call_effect_state` is lifted onto the model rather than
    left inside the payload: the engine reads it as a field, and burying the
    engine's own input in a free-form mapping would make FR-055 depend on a key
    nobody validates.
    """
    payload = json.loads(row["redacted_payload_json"] or "{}")
    return RunEvent(
        sequence_number=int(row["sequence_number"]),
        event_type=OutcomeEventType(row["event_type"]),
        actor=EventActor(row["actor"]),
        created_at=_instant(row["created_at"]),
        tool_name=row["tool_name"],
        correlation_id=row["correlation_id"],
        request_id=row["request_id"],
        reported_status=row["reported_status"],
        state_version_before=row["state_version_before"],
        state_version_after=row["state_version_after"],
        state_hash_before=row["state_hash_before"],
        state_hash_after=row["state_hash_after"],
        duration_ms=row["duration_ms"],
        redacted_payload=payload,
        post_call_effect_state=payload.get("post_call_effect_state"),
    )


def _instant(stored: str) -> datetime:
    return datetime.fromisoformat(stored.replace("Z", "+00:00")).astimezone(UTC)


def _compose(
    run_id: str,
    run: Mapping[str, Any],
    contract: OutcomeContract,
    evaluation: Evaluation,
    events: Sequence[RunEvent],
    guidance: GuidanceState,
    *,
    external_target: ExternalTargetReference | None = None,
) -> OutcomeReport:
    """§23.1's layered report, from evidence the core has already judged.

    Nothing is re-evaluated: `compose_outcome_report` derives every layer and
    count from the findings and events it is handed, so a report cannot disagree
    with the verdict it summarises. `model_tool_selection` is not passed because
    it cannot be — §23.1 finalizes it as `not_evaluated` in a source report and
    a Tier 2 import must not update it, so the core offers no parameter for it.
    """
    return compose_outcome_report(
        run_id=run_id,
        target=TargetReference(id=str(run["target_id"]), adapter_id=str(run["target_adapter_id"])),
        scenario=ScenarioReference(
            mode=str(run["scenario_mode"] or "unspecified"),
            fault_profile=run["failure_profile"],
            # Recorded by the adapter, not chosen here (§12.2). Still false
            # until scenario selection reaches the target through the adapter.
            fault_active=bool(run["fault_active"]),
        ),
        external_target=external_target,
        contract=ContractReference(
            id=str(run["contract_id"]),
            schema_version=contract.schema_version,
            content_hash=str(run["contract_content_hash"]),
        ),
        assertion_findings=evaluation.assertions,
        # Execution findings ride with the policies because the report has to
        # see every finding the run was sealed on. `compose_outcome_report`
        # derives `status`, `counts.critical_failures`, and `primary_failure`
        # from what it is handed, and `_seal` aggregates `evaluation.all()` — so
        # withholding these would produce a report reading `passed` over a run
        # row reading `failed`, which is the disagreement this whole module is
        # arranged to prevent. The core already types them `check_type: policy`,
        # and they are what a `safety_policy` reader has to account for: the
        # `tool_execution` layer names the same failure separately, so nothing
        # is hidden by the pairing.
        policy_findings=(*evaluation.policies, *evaluation.execution),
        trajectory_finding=evaluation.trajectory,
        # §23.1's partition block. Present only when the policy was actually
        # evaluated: a block reading "0 changed, 0 undeclared" on a run with no
        # snapshots would say "nothing changed" where the truth is "nothing was
        # compared", which is the confusion §16.1 exists to prevent.
        undeclared_changes=_undeclared_changes_block(evaluation),
        # §23.1's execution layer: did the calls themselves work, separately
        # from whether they achieved anything. Derived by the core from the
        # timeline rather than from any status this service holds.
        tool_execution=tool_execution_layer(events),
        events=events,
        guidance_at_finalization=GuidanceReference(
            actor=guidance.active_actor,
            action=str(guidance.action_code.value) if guidance.action_code else "wait",
            reason=guidance.reason,
        ),
        mode=RunMode.VERIFICATION,
    )
