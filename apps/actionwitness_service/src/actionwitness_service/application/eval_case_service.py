"""Generating a regression eval case from a stored run (§24.2, FR-080–FR-082).

This is the I/O half; `actionwitness_core.evals.factory` is the judgement half.
The split matters because FR-080's idempotence is a property of the case's
*content*: the factory is pure, so the only way two generations differ is if the
evidence differs, and this module's job is to load that evidence faithfully and
write the result exactly once.

**Idempotence is enforced by the database, not by a read-then-write.** §17.1
puts a unique constraint on `(source_run_id, contract_content_hash,
generator_schema_version)`, and two concurrent generators would both pass a
"does it exist?" check before either inserted. So the insert is attempted and a
constraint violation means somebody else won — at which point the existing row
is returned with `created: false`, which is exactly what FR-080 asks for.

**Only a terminal failed or warning-bearing run is eligible.** A run still in
flight has no final classification set, so a case cut from one would embed a
prediction rather than an observation; a passing run has no failure to
reproduce. A proposal run is refused by name (`PROPOSAL_RUN_NOT_ELIGIBLE`,
§24.3a).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from actionwitness_core.contracts.enums import AssertionSeverity
from actionwitness_core.contracts.models import OutcomeContract
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.evals.enums import ConfirmationStrategy
from actionwitness_core.evals.factory import build_case
from actionwitness_core.evals.minimize import minimize_fixture, prune_trajectory
from actionwitness_core.evals.models import (
    CASE_SCHEMA_VERSION,
    RegressionEvalCase,
    SourceFinding,
)
from actionwitness_core.evals.substitution import substitute_redacted
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState, SnapshotPhase
from actionwitness_core.kernel import CoreError
from actionwitness_core.reports.enums import LayerResult

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import new_id

__all__ = ["EvalCaseService", "GeneratedCase"]

#: Terminal states a case may be cut from. `cancelled` and `error` are terminal
#: too but carry no verdict — a case built from one would have no classification
#: set to expect, which is not the same as expecting an empty one.
_ELIGIBLE_RUN_STATES: frozenset[str] = frozenset(
    {
        str(RunState.FAILED.value),
        str(RunState.PASSED_WITH_WARNINGS.value),
    }
)

#: §16's proposal lifecycle. A run in any of these derived assertion candidates
#: rather than judging a contract, so it has no outcome to reproduce.
_PROPOSAL_RUN_STATES: frozenset[str] = frozenset(
    {
        str(RunState.PROPOSING.value),
        str(RunState.CAPTURING.value),
        str(RunState.PROPOSED.value),
    }
)


@dataclass(frozen=True, slots=True)
class GeneratedCase:
    """The case, and whether this call is the one that created it."""

    case: RegressionEvalCase
    case_id: str
    created: bool


class EvalCaseService:
    """Loads a run's immutable evidence and turns it into a portable case."""

    def __init__(self, work: UnitOfWork, workspace_id: str, registry: Any = None) -> None:
        self._work = work
        self._workspace_id = workspace_id
        #: Optional so a caller that only reads cases needs no adapter registry.
        #: Generation without one keeps the trajectory whole rather than
        #: guessing which calls are read-only.
        self._registry = registry

    async def generate(self, run_id: str) -> GeneratedCase:
        """§24.2, end to end. Refuses rather than guessing."""
        run = await self._eligible_run(run_id)
        contract, stored_hash = await self._contract_of(run)
        fixture_state, fixture_version = await self._initial_fixture(run_id)
        trajectory = await self._trajectory(run_id)
        findings = await self._findings(run_id)

        # §24.2 steps 2-3. Minimization happens here, where the adapter is
        # reachable: which tools are read-only is the adapter's published
        # metadata, and a core that guessed from tool names would be inventing
        # target knowledge.
        minimized, is_complete = minimize_fixture(fixture_state, contract)
        trajectory = prune_trajectory(trajectory, contract, self._read_only_tools(run))

        try:
            case = build_case(
                name=str(contract.name),
                source_run_id=run_id,
                implementation_version=str(run["implementation_version"]),
                build_commit=_optional(run["build_commit"]),
                scenario_mode=_optional(run["scenario_mode"]),
                failure_profile=_optional(run["failure_profile"]),
                source_result=LayerResult(str(run["overall_result"])),
                source_classifications=_critical(findings),
                target_type="managed_application",
                target_id=str(run["target_id"]),
                adapter_id=str(run["target_adapter_id"]),
                contract=contract,
                stored_contract_hash=stored_hash,
                fixture_state=minimized,
                fixture_state_version=fixture_version,
                fixture_is_complete=is_complete,
                trajectory=trajectory,
                source_findings=findings,
                confirmation_strategy=_strategy_for(contract),
                generator_version=CASE_SCHEMA_VERSION,
            )
        except CoreError as rejected:
            # The core's refusals are about the *evidence*, so they reach the
            # caller as a precondition failure rather than a harness error.
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                str(rejected),
                details=[{"path": "run_id", "message": "this run cannot produce a case"}],
            ) from rejected

        return await self._persist(run_id, case, stored_hash)

    # -- evidence ------------------------------------------------------------

    async def _eligible_run(self, run_id: str) -> Mapping[str, Any]:
        """FR-080's eligibility, checked before anything is loaded."""
        run = await WorkspaceScope(self._work, self._workspace_id).run(run_id)

        status = str(run["status"])

        # §24.3a: "a proposal run is not eligible for eval generation and is
        # refused with `PROPOSAL_RUN_NOT_ELIGIBLE`." Recognised by run state
        # rather than a `mode` column, because `runs` has none — 005 declared
        # proposal mode and refuses it at arming, so the only way such a run
        # could exist is a later milestone shipping it. Named separately from
        # the terminal-state check below because it is a different reason: a
        # caller told "not terminal" would wait, and this run will never have a
        # verdict to wait for.
        if status in _PROPOSAL_RUN_STATES:
            raise ApiError(
                ApiErrorCode.PROPOSAL_RUN_NOT_ELIGIBLE,
                "A proposal run derives assertion candidates and carries no verdict, "
                "so there is no outcome for a case to reproduce.",
            )

        if status not in _ELIGIBLE_RUN_STATES:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "Only a failed or warning-bearing terminal run can produce a regression "
                f"eval case; this run is {status}.",
                details=[{"path": "run_id", "message": status}],
            )
        return run

    async def _contract_of(self, run: Mapping[str, Any]) -> tuple[OutcomeContract, str]:
        """The armed contract and the hash the run recorded for it.

        Both come from the run's own columns rather than from whatever the
        workspace holds now: FR-025 locked the contract at arming, and a case
        built from a later selection would reproduce a different journey.
        """
        contract_id = run.get("contract_id")
        if not contract_id:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This run was armed without a contract, so there is nothing to reproduce.",
            )
        row = await self._work.fetch_one(
            "SELECT document_json, content_hash FROM contracts WHERE id = ?", (str(contract_id),)
        )
        if row is None:  # pragma: no cover - contracts are immutable once armed
            raise ApiError(ApiErrorCode.HARNESS_ERROR, "The armed contract has gone.")
        document = OutcomeContract.model_validate(json.loads(row["document_json"]))
        return document, str(row["content_hash"])

    async def _initial_fixture(self, run_id: str) -> tuple[Mapping[str, Any], int]:
        """The `before` snapshot — the state a replay restores.

        The snapshot repository verifies its hash on read, so a tampered
        snapshot fails here rather than becoming a fixture that silently
        replays something else.
        """
        row = await self._work.fetch_one(
            "SELECT redacted_state_json, state_version, content_hash FROM snapshots "
            "WHERE run_id = ? AND phase = ?",
            (run_id, str(SnapshotPhase.BEFORE.value)),
        )
        if row is None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This run has no initial snapshot, so no fixture can be restored.",
            )
        state = json.loads(row["redacted_state_json"])
        version = row["state_version"]
        return state, int(version) if version is not None and str(version).isdigit() else 0

    async def _trajectory(self, run_id: str) -> list[tuple[int, str, Mapping[str, Any]]]:
        """The agent's calls, in order, with the arguments they were made with.

        Start events, because §10.3 builds the observed trajectory from them and
        a case must replay what the agent *attempted* — a call that started and
        failed is still part of the journey being reproduced.

        The arguments come from the start event's redacted payload, which is
        where §20.3 required them to be written. They are already redacted;
        nothing here re-reads an unredacted source, because none is kept.
        """
        rows = await self._work.fetch_all(
            "SELECT tool_name, redacted_payload_json FROM events "
            "WHERE run_id = ? AND event_type = ? AND actor = ? ORDER BY sequence_number",
            (
                run_id,
                str(OutcomeEventType.TOOL_INVOCATION_STARTED.value),
                str(EventActor.AGENT.value),
            ),
        )
        trajectory: list[tuple[int, str, Mapping[str, Any]]] = []
        for index, row in enumerate(rows, start=1):
            payload = json.loads(row["redacted_payload_json"] or "{}")
            arguments = payload.get("arguments") or {}
            # §24.2 step 5. The stored arguments were redacted at record time,
            # and `[REDACTED]` is not an email address — a replay sending the
            # marker would fail argument validation at the target and reproduce
            # that instead of the regression. The substitute is deterministic
            # and unmistakably fake; no original value is recovered, because
            # none was ever persisted.
            arguments = substitute_redacted(arguments)
            trajectory.append((index, str(row["tool_name"]), arguments))
        return trajectory

    async def _findings(self, run_id: str) -> tuple[SourceFinding, ...]:
        """Every finding the run recorded, in a stable order."""
        rows = await self._work.fetch_all(
            "SELECT check_id, classification, severity, status, path, expected_json, actual_json "
            "FROM findings WHERE run_id = ? ORDER BY check_id",
            (run_id,),
        )
        return tuple(
            SourceFinding(
                check_id=str(row["check_id"]),
                classification=(
                    None
                    if row["classification"] is None
                    else FailureClassification(str(row["classification"]))
                ),
                severity=AssertionSeverity(str(row["severity"])),
                status=CheckStatus(str(row["status"])),
                path=_optional(row["path"]),
                expected=_loads(row["expected_json"]),
                actual=_loads(row["actual_json"]),
            )
            for row in rows
        )

    def _read_only_tools(self, run: Mapping[str, Any]) -> frozenset[str]:
        """Which of this target's tools change nothing, per the adapter itself.

        Read from the adapter's published specs rather than inferred from a
        name: §9.1 makes the adapter the authority on its own surface, and a
        harness that decided `get_cart` sounded harmless would be guessing about
        a target it is not allowed to know.

        An unavailable adapter yields an empty set, which drops nothing — the
        safe direction, since a trajectory kept whole always replays.
        """
        from actionwitness_core.ports.enums import SideEffectClass

        slot = self._registry.resolve(str(run["target_adapter_id"])) if self._registry else None
        if slot is None or slot.factory is None:
            return frozenset()
        return frozenset(
            spec.name
            for spec in slot.factory().tool_specs()
            if spec.side_effect is SideEffectClass.READ_ONLY
        )

    # -- persistence ---------------------------------------------------------

    async def _persist(
        self, run_id: str, case: RegressionEvalCase, contract_hash: str
    ) -> GeneratedCase:
        """Insert, or return what a previous identical call already wrote.

        The unique constraint is the mechanism. A read-then-insert would let two
        concurrent generators both find nothing and both write, and FR-080's
        "never mints a duplicate" would hold only when nobody was in a hurry.
        """
        existing = await self._work.fetch_one(
            "SELECT id, case_json FROM evaluation_cases WHERE source_run_id = ? "
            "AND contract_content_hash = ? AND generator_schema_version = ?",
            (run_id, contract_hash, CASE_SCHEMA_VERSION),
        )
        if existing is not None:
            return GeneratedCase(
                case=RegressionEvalCase.model_validate(
                    _without_hash(json.loads(existing["case_json"]))
                ),
                case_id=str(existing["id"]),
                created=False,
            )

        stored = case.as_stored_document()
        row_id = new_id("evc")
        await self._work.execute(
            """
            INSERT INTO evaluation_cases (
                id, workspace_id, source_run_id, contract_content_hash,
                generator_schema_version, schema_version, name, content_hash,
                case_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                self._workspace_id,
                run_id,
                contract_hash,
                CASE_SCHEMA_VERSION,
                case.schema_version,
                case.name,
                case.content_hash(),
                # The canonical text, so the stored bytes are the bytes that
                # were hashed — the same rule 005's artifact store follows.
                _canonical_json(stored),
                self._work.now(),
            ),
        )
        return GeneratedCase(case=case, case_id=row_id, created=True)

    async def get(self, case_id: str) -> tuple[RegressionEvalCase, Mapping[str, Any]]:
        """One case this workspace owns, parsed back from its stored bytes."""
        row = await self._work.fetch_one(
            "SELECT * FROM evaluation_cases WHERE id = ? AND workspace_id = ?",
            (case_id, self._workspace_id),
        )
        if row is None:
            raise ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, "No such eval case.")
        document = json.loads(row["case_json"])
        return RegressionEvalCase.model_validate(_without_hash(document)), dict(row)

    async def list_cases(self) -> Sequence[Mapping[str, Any]]:
        rows = await self._work.fetch_all(
            "SELECT id, source_run_id, name, content_hash, schema_version, created_at "
            "FROM evaluation_cases WHERE workspace_id = ? ORDER BY created_at DESC, id DESC",
            (self._workspace_id,),
        )
        return [dict(row) for row in rows]


def _canonical_json(document: Mapping[str, Any]) -> str:
    from actionwitness_core.security.canonical import canonical_text

    return canonical_text(dict(document))


def _without_hash(document: Mapping[str, Any]) -> dict[str, Any]:
    """A stored case, ready to re-validate.

    The stored document carries its own `content_hash`, which the model does not
    declare — it is computed. Dropping it here is what lets a round trip through
    storage produce an equal object rather than a validation error.
    """
    return {key: value for key, value in document.items() if key != "content_hash"}


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)


def _loads(value: Any) -> Any:
    return None if value is None else json.loads(value)


def _critical(findings: Sequence[SourceFinding]) -> tuple[FailureClassification, ...]:
    """The distinct critical classifications the run failed on (§22).

    Read from the recorded findings rather than recomputed, so the expectation
    is what the run's own verdict said.
    """
    return tuple(
        {
            finding.classification
            for finding in findings
            if finding.classification is not None
            and finding.status is CheckStatus.FAILED
            and finding.severity is AssertionSeverity.CRITICAL
        }
    )


def _strategy_for(contract: OutcomeContract) -> ConfirmationStrategy:
    """Which interaction provider a case defaults to (§24.5).

    A contract with no `requires_confirmation` policy has nothing to decide, so
    `no_confirmation` is both correct and the safe default. A contract that
    protects a tool keeps the same default deliberately: FR-087 forbids
    inferring consent, and choosing `recorded_approval` here would be the
    harness deciding, on the operator's behalf, that a recorded approval
    existed. T7 reads the recording and sets it from evidence.
    """
    return ConfirmationStrategy.NO_CONFIRMATION
