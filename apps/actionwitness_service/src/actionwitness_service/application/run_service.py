"""Arming an outcome run (FR-030, FR-012, FR-025, FR-040, §16).

FR-030: "Arming shall read canonical state once inside the workspace
transaction, validate preconditions against that exact value, persist it as the
initial snapshot, create the `armed` run, and return a `run_id`. A failed
precondition shall return a structured `PRECONDITION_FAILED` response and create
neither a run nor a partial snapshot."

**The one place this departs from the literal wording, and why.** Capturing
canonical state is an HTTP call to the target, and ADR-0003 — Accepted and
binding — says nothing async holds a lock or a transaction across a wait. Doing
the capture inside the SQLite transaction would hold the single write lock for
the duration of an external call, so one slow target would stall every other
workspace in the process and start tripping busy timeouts that have nothing to
do with it.

It would also buy nothing. Canonical state lives in the *target*, not in this
database, so a SQLite transaction gives no isolation against it changing; the
literal reading protects nothing that the reading below does not. So the state
is read **once**, preconditions are validated against **that exact value**, and
**that exact value** is persisted — every substantive clause of FR-030 — with
the transaction opened after the read and closed before anything else waits.

What the transaction does own is the part that is genuinely racy: between
reading the workspace's configuration and writing the run, another request could
change the selected contract or arm a run of its own. So the configuration is
re-read inside the transaction and compared, and a mismatch refuses rather than
arming a run against a configuration nobody selected. FR-012 fixes the
configuration at arming; this is what makes that true under concurrency rather
than only in a single-client test.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.engine.assertions import evaluate_preconditions
from actionwitness_core.engine.enums import CheckStatus
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState, SnapshotPhase
from actionwitness_core.ports.models import Observation
from actionwitness_core.security.canonical import content_hash

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry, TargetUnavailable
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.application.limits import WorkspaceCeilings
from actionwitness_service.application.workspace_service import NONTERMINAL_RUN_STATES
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.locks import WorkspaceLocks
from actionwitness_service.persistence.repositories import (
    EventRepository,
    SnapshotRepository,
    new_id,
)

__all__ = ["ArmedRun", "RunMode", "RunService", "WorkspaceConfiguration"]

#: The implementation version copied into every run (§17.1 `runs`). Runs are
#: comparable only within one implementation, so this is recorded at arming and
#: never updated afterwards.
IMPLEMENTATION_VERSION: Final = "0.1.0"


class RunMode:
    """§15.3's `mode`. `proposal` is declared here and refused below.

    BUILD_ORDER §7/M4 scopes this milestone to the verification slice, and
    proposal mode brings its own three states, candidate derivation, and
    curation surface. Declaring the value and refusing it explicitly is the
    003 pattern for an unimplemented option: an unimplemented profile is
    "refused rather than downgraded", because silently arming a verification run
    for someone who asked for a proposal is worse than saying no.
    """

    VERIFICATION: Final = "verification"
    PROPOSAL: Final = "proposal"
    ALL: Final = (VERIFICATION, PROPOSAL)


@dataclass(frozen=True)
class WorkspaceConfiguration:
    """The controlled inputs FR-012 fixes at arming.

    Compared by value inside the arming transaction against the values read
    before the observation, which is how a concurrent change is caught.
    """

    contract_id: str
    contract_content_hash: str
    target_id: str
    adapter_id: str
    scenario_mode: str | None
    failure_profile: str | None
    document: Mapping[str, Any]

    def controlled_inputs(self) -> tuple[str | None, ...]:
        """Everything that must not have moved while the target was observed."""
        return (
            self.contract_id,
            self.contract_content_hash,
            self.target_id,
            self.scenario_mode,
            self.failure_profile,
        )


@dataclass(frozen=True)
class ArmedRun:
    run_id: str
    status: str
    contract_id: str
    target_id: str
    scenario_mode: str | None
    failure_profile: str | None
    state_version: str | None
    snapshot_content_hash: str


class RunService:
    """Arms outcome runs. One public method; the rest is its sequencing."""

    def __init__(
        self,
        database: Database,
        registry: AdapterRegistry,
        locks: WorkspaceLocks,
        *,
        id_source: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._locks = locks
        #: Injected so a replayed run can be given the identity it had the first
        #: time (constitution §1). Defaults to a random identifier.
        self._id_source = id_source or (lambda: new_id("run"))
        self._clock = clock

    async def arm(self, workspace_id: str, *, mode: str = RunMode.VERIFICATION) -> ArmedRun:
        """FR-030. Three phases, and the boundaries between them are the design.

        1. Read the workspace's selected configuration. No lock, no write.
        2. Capture the authoritative initial observation. **I/O**, so no lock and
           no transaction is held (ADR-0003).
        3. Under the workspace lock and one transaction: re-read the
           configuration and refuse if it moved, refuse if a run is already
           active, check the run ceiling, validate preconditions against the
           captured value, and write the run, the snapshot, and the events.
        """
        _require_supported_mode(mode)

        # Phase 1 — what this workspace has selected.
        async with self._database.reading() as work:
            selected = await self._configuration(work, workspace_id)

        # Phase 2 — the one authoritative read. Outside every lock.
        observation = await self._capture(selected, workspace_id)

        # Phase 3 — validate against that exact value, and commit or refuse.
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            confirmed = await self._configuration(work, workspace_id)
            if confirmed.controlled_inputs() != selected.controlled_inputs():
                raise ApiError(
                    ApiErrorCode.RUN_IN_PROGRESS,
                    "The workspace configuration changed while the target was being "
                    "observed. Nothing was armed; retry to arm against the current "
                    "selection.",
                )
            await self._require_no_active_run(work, workspace_id)
            await WorkspaceCeilings(work, workspace_id).guard_new_run()

            _validate_preconditions(confirmed, observation)
            return await self._write(work, workspace_id, confirmed, observation)

    # -- phase 1 and 3: the selected configuration ---------------------------

    async def _configuration(self, work: UnitOfWork, workspace_id: str) -> WorkspaceConfiguration:
        row = await work.fetch_one(
            "SELECT selected_contract_id, selected_target_id, scenario_mode, failure_profile "
            "FROM workspaces WHERE id = ?",
            (workspace_id,),
        )
        if row is None:  # pragma: no cover - the middleware creates it first
            raise ApiError(ApiErrorCode.HARNESS_ERROR, "The workspace disappeared mid-request.")

        contract_id = row["selected_contract_id"]
        if not contract_id:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                "No contract is selected. Select one before arming a run.",
                details=[{"path": "selected_contract_id", "message": "no contract is selected"}],
            )

        # Read through the same scope as every other workspace-owned read, so a
        # contract that is not this workspace's cannot be armed even if it
        # somehow reached the column (FR-006).
        contract = await WorkspaceScope(work, workspace_id).contract(contract_id)
        document = json.loads(contract["document_json"])

        target_id = row["selected_target_id"] or str(document.get("target_id", ""))
        slot = self._registry.resolve(target_id)
        if slot is None or not slot.is_available:
            raise TargetUnavailable(
                target_id or "unknown",
                slot.state.reason if slot else "No adapter is registered for it.",
            )

        return WorkspaceConfiguration(
            contract_id=contract_id,
            contract_content_hash=contract["content_hash"],
            target_id=target_id,
            adapter_id=slot.name,
            scenario_mode=row["scenario_mode"],
            failure_profile=row["failure_profile"],
            document=document,
        )

    # -- phase 2: the one authoritative read ---------------------------------

    async def _capture(self, selected: WorkspaceConfiguration, workspace_id: str) -> Observation:
        """FR-040: the selected observation provider captures target state.

        A provider that fails raises rather than returning a partial value, and
        that failure is allowed to propagate: constitution §5 makes an
        observation failure an explicit non-pass, and arming against an
        observation nobody could take would produce a run whose baseline is a
        guess.
        """
        adapter = self._registry.adapter(selected.adapter_id)
        return await adapter.observation_provider().capture(workspace_id)

    # -- phase 3: the write --------------------------------------------------

    async def _require_no_active_run(self, work: UnitOfWork, workspace_id: str) -> None:
        """FR-039's lease, at its first gate.

        Checked inside the arming transaction rather than before it, because a
        check outside would be a read whose answer could change before the
        insert — which is exactly how two tabs end up with two armed runs.
        """
        placeholders = ",".join("?" for _ in NONTERMINAL_RUN_STATES)
        row = await work.fetch_one(
            f"SELECT id FROM runs WHERE workspace_id = ? AND status IN ({placeholders})",
            (workspace_id, *sorted(NONTERMINAL_RUN_STATES)),
        )
        if row is not None:
            raise ApiError(
                ApiErrorCode.RUN_IN_PROGRESS,
                "This workspace already has a run in progress. Reset the workspace "
                "before arming another.",
            )

    async def _write(
        self,
        work: UnitOfWork,
        workspace_id: str,
        selected: WorkspaceConfiguration,
        observation: Observation,
    ) -> ArmedRun:
        run_id = self._id_source()
        started_at = work.now()

        await work.execute(
            """
            INSERT INTO runs (
                id, workspace_id, contract_id, contract_content_hash,
                target_id, target_adapter_id, scenario_mode, failure_profile,
                intent_content_hash, implementation_version, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                workspace_id,
                selected.contract_id,
                selected.contract_content_hash,
                selected.target_id,
                selected.adapter_id,
                selected.scenario_mode,
                selected.failure_profile,
                content_hash({"intent": str(selected.document.get("intent", ""))}),
                IMPLEMENTATION_VERSION,
                str(RunState.ARMED.value),
                started_at,
            ),
        )
        await work.execute(
            "UPDATE workspaces SET active_run_id = ? WHERE id = ?", (run_id, workspace_id)
        )

        events = EventRepository(work)
        # `run_armed` first. The timeline belongs to the run, so its own
        # creation is its first event; a `snapshot_captured` at sequence 1 would
        # describe a run that, by its own timeline, did not yet exist.
        await events.append(
            run_id,
            {
                "event_type": str(OutcomeEventType.RUN_ARMED.value),
                "actor": str(EventActor.HUMAN.value),
                "redacted_payload": {
                    "contract_id": selected.contract_id,
                    "contract_content_hash": selected.contract_content_hash,
                    "target_id": selected.target_id,
                    "scenario_mode": selected.scenario_mode,
                    "failure_profile": selected.failure_profile,
                },
            },
        )

        await SnapshotRepository(work).add(run_id, SnapshotPhase.BEFORE, observation)
        await events.append(
            run_id,
            {
                "event_type": str(OutcomeEventType.SNAPSHOT_CAPTURED.value),
                "actor": str(EventActor.HARNESS.value),
                "state_version_after": observation.state_version,
                "state_hash_after": observation.content_hash(),
                "redacted_payload": {
                    "phase": str(SnapshotPhase.BEFORE.value),
                    "provider": observation.provider_id,
                },
            },
        )

        return ArmedRun(
            run_id=run_id,
            status=str(RunState.ARMED.value),
            contract_id=selected.contract_id,
            target_id=selected.target_id,
            scenario_mode=selected.scenario_mode,
            failure_profile=selected.failure_profile,
            state_version=observation.state_version,
            snapshot_content_hash=observation.content_hash(),
        )


def _require_supported_mode(mode: str) -> None:
    if mode == RunMode.VERIFICATION:
        return
    if mode == RunMode.PROPOSAL:
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "Proposal-mode runs are not available in this build.",
            details=[{"path": "mode", "message": "only 'verification' is implemented"}],
        )
    raise ApiError(
        ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        f"Unknown run mode {mode!r}.",
        details=[{"path": "mode", "message": f"expected one of {', '.join(RunMode.ALL)}"}],
    )


def _validate_preconditions(selected: WorkspaceConfiguration, observation: Observation) -> None:
    """FR-030's refusal, evaluated by the core against the captured value.

    Raised before anything is written, so "create neither a run nor a partial
    snapshot" holds because there is nothing to undo rather than because a
    rollback was remembered. The failing findings become §15.8's `details`, so a
    caller learns every unmet precondition at once instead of one per attempt.
    """
    contract = parse_contract(selected.document)
    findings = evaluate_preconditions(contract.preconditions, initial=observation.as_context())
    failures = [finding for finding in findings if finding.status is not CheckStatus.PASSED]
    if not failures:
        return

    raise ApiError(
        ApiErrorCode.PRECONDITION_FAILED,
        "The target's current state does not satisfy this contract's preconditions.",
        details=_precondition_details(failures),
    )


def _precondition_details(failures: Sequence[Any]) -> list[dict[str, str]]:
    return [
        {
            "path": str(finding.path) if finding.path is not None else finding.check_id,
            "message": _precondition_message(finding),
        }
        for finding in failures
    ]


def _precondition_message(finding: Any) -> str:
    if finding.status is CheckStatus.OBSERVATION_UNAVAILABLE:
        return "the authoritative observation was unavailable for this path"
    return f"expected {finding.expected!r}, observed {finding.actual!r}"
