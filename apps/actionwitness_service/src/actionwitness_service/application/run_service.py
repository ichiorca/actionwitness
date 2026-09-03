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
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.engine.assertions import evaluate_preconditions
from actionwitness_core.engine.enums import CheckStatus
from actionwitness_core.evidence.effects import redacted_observation
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState, SnapshotPhase
from actionwitness_core.journeys.transitions import is_terminal
from actionwitness_core.ports import ScenarioReportingAdapter
from actionwitness_core.ports.models import Observation
from actionwitness_core.reports.enums import RunMode
from actionwitness_core.security.canonical import content_hash
from actionwitness_core.security.redaction import RedactionPolicy

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry, TargetUnavailable
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.application.guidance_service import GuidanceRecorder, current_guidance
from actionwitness_service.application.limits import WorkspaceCeilings
from actionwitness_service.application.self_witness import (
    capture_target_state,
    ensure_observed_workspace,
    observes_a_separate_workspace,
)
from actionwitness_service.application.workspace_service import NONTERMINAL_RUN_STATES
from actionwitness_service.application.workspaces import WorkspaceStore
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.locks import WorkspaceLocks
from actionwitness_service.persistence.repositories import (
    EventRepository,
    SnapshotRepository,
    new_id,
)

__all__ = ["ArmedRun", "RunService", "WorkspaceConfiguration"]

#: The implementation version copied into every run (§17.1 `runs`). Runs are
#: comparable only within one implementation, so this is recorded at arming and
#: never updated afterwards.
IMPLEMENTATION_VERSION: Final = "0.1.0"


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
    #: FR-172's other workspace, for a target that observes one. `None` for every
    #: ordinary target, whose observed workspace *is* the recording workspace.
    #:
    #: Deliberately absent from `controlled_inputs` below. That tuple is the set
    #: of choices an operator makes and a concurrent request could change
    #: underneath an observation in flight; this one is minted by the server,
    #: written once, and never reassigned — so comparing it would only ever
    #: report the difference between "not provisioned yet" and "provisioned by
    #: this same call", refusing an arm that nothing had actually raced.
    observed_workspace_id: str | None = None

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

    async def arm(
        self,
        workspace_id: str,
        *,
        mode: str = RunMode.VERIFICATION.value,
        comparison_source_run_id: str | None = None,
    ) -> ArmedRun:
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

        # FR-011 lets a profile be chosen before a contract, so the target that
        # would have to inject it may not have existed at selection time. This
        # is where that becomes answerable, and it is the last moment it can be
        # asked: everything after this writes the profile into a run's evidence,
        # and a report naming an active fault the target never injects is the
        # false claim this harness exists to catch (012-T8, §13.3).
        self._require_injectable_profile(selected)

        # FR-172, before anything is observed. A self-witnessing target needs a
        # second workspace, and the run must be refused *here* if it cannot have
        # one legitimately — after the capture below it would already have read
        # the state its own run was producing, which is the loop rather than a
        # report about it.
        selected = await self._isolate_observer(selected, workspace_id)

        # Phase 2 — the one authoritative read. Outside every lock.
        observation = await self._capture(selected, workspace_id)
        # And, from the same target and in the same phase, what it says about
        # the defect it is running (§23.1). I/O, so it belongs here rather than
        # inside the transaction below.
        fault_active = await self._fault_state(selected, workspace_id)

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
            await self._require_eligible_source(work, workspace_id, comparison_source_run_id)
            await WorkspaceCeilings(work, workspace_id).guard_new_run()

            _validate_preconditions(confirmed, observation)
            return await self._write(
                work,
                workspace_id,
                confirmed,
                observation,
                comparison_source_run_id,
                fault_active=fault_active,
            )

    async def _isolate_observer(
        self, selected: WorkspaceConfiguration, workspace_id: str
    ) -> WorkspaceConfiguration:
        """Give a self-witnessing run the separate workspace FR-172 requires.

        A no-op for every ordinary target: its observed workspace is the
        recording one, so there is no second workspace to provision and none to
        keep apart. Only a provider that asks *which* workspace to read reaches
        the mint, which is why the question is asked of the protocol rather than
        of the target's name.

        Its own short transaction, and not folded into phase 3's. Phase 3 opens
        after the observation, and the observed workspace has to exist before the
        observation is taken — there is nothing to read otherwise.
        """
        adapter = self._registry.adapter(selected.adapter_id)
        if not observes_a_separate_workspace(adapter):
            return selected

        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            observed = await ensure_observed_workspace(
                work, WorkspaceStore(self._database), workspace_id
            )
        return replace(selected, observed_workspace_id=observed)

    def _require_injectable_profile(self, selected: WorkspaceConfiguration) -> None:
        """Refuse to arm against a fault the selected target cannot inject.

        §13.3 names six profiles and this build ships some of them. The
        recognised-but-unbuilt case is the dangerous one, and it is refused here
        rather than downgraded: a run armed with it would produce a report
        naming an active defect while the target behaved honestly, which is
        precisely the disagreement between claim and state that the product
        exists to surface. Manufacturing one would be worse than any bug it
        could hide.
        """
        profile = selected.failure_profile
        if profile is None:
            return
        if self._registry.injects_fault_profile(selected.target_id, profile):
            return
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            f"The selected target cannot inject the {profile!r} fault profile, so a run "
            "armed against it would report a defect that was never produced. It is "
            "recognised by the specification and not implemented in this build.",
            details=[{"path": "failure_profile", "message": "recognised but not implemented"}],
        )

    async def _fault_state(self, selected: WorkspaceConfiguration, workspace_id: str) -> bool:
        """Ask the target whether its injected defect is on (§23.1, AC-20).

        AC-20 asks a run to record that the fault was "active only for the
        `pre_fix` run". The harness must not answer that itself: §9.1 forbids the
        core from interpreting scenario-mode names, and §23.1 assigns the
        derivation to the adapter. Inferring it from `mode == "pre_fix"` would
        record a defect as running because it was *requested*, which is the same
        substitution of claim for observation the whole product is against.

        Two outcomes, and no third:

        * The adapter can report — take its answer, whatever the selection says.
        * The adapter cannot, and no fault is selected — `False`, which is a true
          statement about a target that injects nothing.

        A selected fault with no way to confirm it raises. `_require_injectable_
        profile` has already established that the target advertises the profile,
        so an adapter that then cannot say whether it is running is a build
        inconsistency, and arming through it would produce a report naming an
        active defect on nobody's authority (§16.1).
        """
        adapter = self._registry.adapter(selected.adapter_id)
        if isinstance(adapter, ScenarioReportingAdapter):
            state = await adapter.scenario_state(workspace_id)
            return state.fault_active

        profile = selected.failure_profile
        if profile is None or profile == AdapterRegistry.NO_FAULT:
            return False
        raise ApiError(
            ApiErrorCode.TARGET_UNAVAILABLE,
            f"The selected target cannot report whether the {profile!r} fault is "
            "running, so a run armed against it would describe an injected defect "
            "that nothing confirmed.",
            details=[{"path": "failure_profile", "message": "target reports no scenario state"}],
        )

    # -- phase 1 and 3: the selected configuration ---------------------------

    async def _configuration(self, work: UnitOfWork, workspace_id: str) -> WorkspaceConfiguration:
        row = await work.fetch_one(
            "SELECT selected_contract_id, selected_target_id, scenario_mode, failure_profile, "
            "observed_workspace_id FROM workspaces WHERE id = ?",
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
            observed_workspace_id=row["observed_workspace_id"],
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
        observation = await capture_target_state(
            adapter, workspace_id, selected.observed_workspace_id
        )

        # §20.3: redacted before persistence, hashing, or export — and before
        # evaluation too, so the baseline a verdict rests on is byte-for-byte
        # the baseline a reader of the evidence can see.
        return redacted_observation(observation, _policy_of(selected))

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

    async def _require_eligible_source(
        self, work: UnitOfWork, workspace_id: str, source_run_id: str | None
    ) -> None:
        """§15.3: the bound source must be "eligible" and immutable.

        Eligible means this workspace's own run (FR-006) and terminal — a
        source still in flight has no outcome to compare against, and binding
        one would produce a pair whose "before" side changes after the fact.

        Checked inside the arming transaction, so a source that terminates or
        is purged between the check and the insert cannot slip through.
        """
        if source_run_id is None:
            return
        source = await WorkspaceScope(work, workspace_id).run(source_run_id)
        if not is_terminal(RunState(source["status"])):
            raise ApiError(
                ApiErrorCode.RUN_IN_PROGRESS,
                "The comparison source run has not finished, so it has no outcome to "
                "compare against.",
                details=[
                    {"path": "comparison_source_run_id", "message": "source is still in flight"}
                ],
            )

    async def _write(
        self,
        work: UnitOfWork,
        workspace_id: str,
        selected: WorkspaceConfiguration,
        observation: Observation,
        comparison_source_run_id: str | None = None,
        *,
        fault_active: bool = False,
    ) -> ArmedRun:
        run_id = self._id_source()
        started_at = work.now()

        await work.execute(
            """
            INSERT INTO runs (
                id, workspace_id, contract_id, contract_content_hash,
                target_id, target_adapter_id, scenario_mode, failure_profile,
                fault_active, intent_content_hash, implementation_version, status,
                started_at, comparison_source_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                # The target's own answer (§23.1), not an inference from the mode.
                int(fault_active),
                content_hash({"intent": str(selected.document.get("intent", ""))}),
                IMPLEMENTATION_VERSION,
                str(RunState.ARMED.value),
                started_at,
                comparison_source_run_id,
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

        # FR-120: arming is a handoff — the operator armed, the agent acts next
        # — so it produces a guidance transition, recorded in the workspace
        # stream and linked into the run timeline by its own id (§12.13).
        #
        # Derived from state *after* the run row and the workspace pointer are
        # written, so it describes the workspace as it now is rather than as the
        # request found it.
        await GuidanceRecorder(work, workspace_id).transition(
            await current_guidance(work, workspace_id), run_id=run_id
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
    """§15.3's `mode`. `proposal` is refused rather than silently downgraded.

    BUILD_ORDER §7/M4 scopes this milestone to the verification slice, and
    proposal mode brings its own three run states, candidate derivation, and
    curation surface. Arming a verification run for someone who asked for a
    proposal would be worse than saying no — the 003 pattern for an
    unimplemented option, which is "refused rather than downgraded".

    The vocabulary is the core's `reports.enums.RunMode`, not a second copy: the
    report already has to name the mode, and two lists of the same two strings
    would eventually disagree.
    """
    if mode == RunMode.VERIFICATION.value:
        return
    if mode == RunMode.PROPOSAL.value:
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "Proposal-mode runs are not available in this build.",
            details=[{"path": "mode", "message": "only 'verification' is implemented"}],
        )
    raise ApiError(
        ApiErrorCode.CONTRACT_VALIDATION_FAILED,
        f"Unknown run mode {mode!r}.",
        details=[
            {
                "path": "mode",
                "message": f"expected one of {', '.join(m.value for m in RunMode)}",
            }
        ],
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


def _policy_of(selected: WorkspaceConfiguration) -> RedactionPolicy:
    """The contract's redaction paths, applied in addition to the defaults."""
    paths = ((selected.document.get("redaction") or {}).get("paths")) or []
    return RedactionPolicy.from_paths([str(path) for path in paths])
