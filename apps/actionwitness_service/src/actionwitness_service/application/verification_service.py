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

**Evaluation reads the stored evidence, not live state.** The events fed to the
trajectory and policy engines are read back out of the database and rebuilt into
the core's `RunEvent` models. FR-050 defines policy determinism over "the same
snapshots and the same recorded event stream" — so evaluating against anything
the timeline does not hold would produce a verdict a replay could not reach.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from actionwitness_core.contracts.models import OutcomeContract, parse_contract
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.assertions import evaluate_assertions
from actionwitness_core.engine.findings import Finding, aggregate, primary_failure
from actionwitness_core.engine.policies import PolicyEvidence, evaluate_policies
from actionwitness_core.engine.trajectory import evaluate_expected_tools
from actionwitness_core.evidence.effects import redacted_observation
from actionwitness_core.evidence.models import RunEvent
from actionwitness_core.journeys.enums import (
    EventActor,
    OutcomeEventType,
    RunState,
    SnapshotPhase,
)
from actionwitness_core.journeys.guidance import derive_guidance, phase_for
from actionwitness_core.ports.models import Observation
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.security.redaction import RedactionPolicy

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry
from actionwitness_service.application.guidance_service import GuidanceRecorder
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


@dataclass(frozen=True)
class VerificationOutcome:
    run_id: str
    status: str
    overall_result: str
    findings: tuple[Finding, ...]
    primary_failure_check_id: str | None
    final_state_version: str | None
    next_action: Mapping[str, object]


class VerificationService:
    """Captures final state, evaluates, and seals the run."""

    def __init__(
        self,
        database: Database,
        registry: AdapterRegistry,
        locks: WorkspaceLocks,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._locks = locks
        self._clock = clock or (lambda: datetime.now(UTC))

    async def verify(self, workspace_id: str, run_id: str) -> VerificationOutcome:
        """FR-038's gate, FR-041's capture, FR-053's verdict, in that order."""
        # 1 — win the race, or lose it before anything is observed.
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            run = await VerificationGate(work, workspace_id).begin(run_id)
            contract, policy = await self._contract_of(work, run)
            events = await self._timeline(work, run_id)
            initial = await self._initial_context(work, run_id)

        # 2 — FR-041's capture. I/O, so nothing is held (ADR-0003).
        adapter = self._registry.adapter(str(run["target_adapter_id"]))
        final = await self._capture(adapter, workspace_id, policy)

        # 3 — the core decides. Pure, and given only recorded evidence.
        findings = _evaluate(contract, adapter, events, initial=initial, final=final.as_context())
        result = aggregate(findings)

        # 4 — the whole verdict commits together.
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            await self._seal(work, workspace_id, run_id, final, findings, result)

        return VerificationOutcome(
            run_id=run_id,
            status=str(_TERMINAL_STATE[result].value),
            overall_result=str(result.value),
            findings=findings,
            primary_failure_check_id=_primary_check_id(findings),
            final_state_version=final.state_version,
            next_action=derive_guidance(
                phase_for(has_contract=True, run_state=_TERMINAL_STATE[result]),
                correlation_id=run_id,
            ).next_action(),
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
        observation = await adapter.observation_provider().capture(workspace_id)
        return redacted_observation(observation, policy)

    # -- writing the verdict -------------------------------------------------

    async def _seal(
        self,
        work: UnitOfWork,
        workspace_id: str,
        run_id: str,
        final: Observation,
        findings: Sequence[Finding],
        result: LayerResult,
    ) -> None:
        """Snapshot, findings, events, and the terminal transition, together."""
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

        terminal = _TERMINAL_STATE[result]
        await work.execute(
            "UPDATE runs SET status = ?, overall_result = ?, completed_at = ? "
            "WHERE id = ? AND workspace_id = ?",
            (str(terminal.value), str(result.value), work.now(), run_id, workspace_id),
        )
        await work.execute(
            "UPDATE workspaces SET active_run_id = NULL WHERE id = ?", (workspace_id,)
        )

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
        await GuidanceRecorder(work, workspace_id).append(
            derive_guidance(
                phase_for(has_contract=True, run_state=terminal), correlation_id=run_id
            ),
            run_id=run_id,
        )


def _evaluate(
    contract: OutcomeContract,
    adapter: Any,
    events: Sequence[RunEvent],
    *,
    initial: Mapping[str, Any] | None,
    final: Mapping[str, Any] | None,
) -> tuple[Finding, ...]:
    """Every layer the core owns, in one place and in contract order.

    Assembled here and decided there: this function chooses *what* to evaluate
    and the engine decides *how* each one turns out.
    """
    assertion_findings = evaluate_assertions(contract.assertions, initial=initial, final=final)
    trajectory = evaluate_expected_tools(contract.expected_tools, events)
    policy_findings = evaluate_policies(
        contract.policies,
        PolicyEvidence(
            events=tuple(events),
            effect_map=_effect_map(adapter),
            contract_paths=_contract_paths(contract),
            # FR-157's full-state diff is not produced yet, and `None` is what
            # says so: a policy needing it reports `not_evaluated` with a reason
            # rather than reading as satisfied (§12.2).
            changed_paths=None,
        ),
    )
    return (*assertion_findings, trajectory, *policy_findings)


def _effect_map(adapter: Any) -> Mapping[str, tuple[ObservationPath, ...]]:
    """§13.4's declared prefixes, parsed into the core's path type."""
    return {
        tool: tuple(ObservationPath.parse(str(path)) for path in paths)
        for tool, paths in adapter.effect_map().items()
    }


def _contract_paths(contract: OutcomeContract) -> tuple[ObservationPath, ...]:
    """Every path the contract resolves (§9.10(a)).

    Preconditions count: a path the contract read at arming is a path it cares
    about, and `no_undeclared_changes` compares against everything the contract
    touches rather than only what it asserts at the end.
    """
    seen: dict[str, ObservationPath] = {}
    for term in (*contract.preconditions, *contract.assertions):
        seen[str(term.path)] = term.path
    return tuple(seen.values())


def _check_event(finding: Finding) -> OutcomeEventType:
    """§16.1's per-check event, chosen by what the finding is about."""
    from actionwitness_core.engine.enums import CheckType

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
