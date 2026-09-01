"""Benchmark suites: create, import, bind, and seal (FR-090, FR-091, §16.4).

This is where an imported report becomes a suite somebody can reason about. The
core owns the arithmetic and the state table; the adapter owns the reporter's
JSON; this module owns the workspace-bound bookkeeping between them.

**Binding is explicit or it does not happen.** FR-091: "the importer shall never
guess a binding from list position, similar text, or timestamps alone." There is
deliberately no code path here that pairs a trial with a run — every binding
arrives naming both sides, and the only thing this module does is *refuse* the
ones it must. A convenience that bound "the obvious" candidate would commit
exactly the error the product exists to catch: attributing one execution's
outcome to another execution's call evidence.

**`ready` seals the bindings.** §16.4: "bindings become immutable when the suite
enters `ready`", and "a changed manifest, adapter, binding, or source artifact
requires a new suite". So `seal` is one-way and every mutating method below
refuses once it has run.

**A refused binding leaves the trial excluded, never rebound.** FR-091 again: a
trial without sufficient evidence for its declared mode is "`excluded`, not
silently downgraded or rebound". Refusals here change nothing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from actionwitness_core.benchmarks.enums import (
    BenchmarkStatus,
    CorrelationMode,
    ExclusionReason,
    SourceKind,
    TrialEligibility,
)
from actionwitness_core.benchmarks.models import (
    MANIFEST_SCHEMA_VERSION,
    BenchmarkManifest,
    NormalizedTrial,
    TrialBinding,
)
from actionwitness_core.benchmarks.states import require_transition
from actionwitness_core.security.canonical import canonical_text

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.benchmark_metrics import BenchmarkSummary, summarize
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import new_id

__all__ = ["BenchmarkService", "ImportedSuite"]

#: Run states an `executed_browser` binding may point at. §17.1 requires "the
#: exact completed outcome `run_id`", so an in-flight run is not bindable: its
#: verdict does not exist yet, and binding to it would reserve a result.
_BINDABLE_RUN_STATES: frozenset[str] = frozenset(
    {"passed", "passed_with_warnings", "failed", "error"}
)

#: Statuses in which bindings may still change (§16.4).
_MUTABLE_STATUSES: frozenset[BenchmarkStatus] = frozenset({BenchmarkStatus.DRAFT})


@dataclass(frozen=True, slots=True)
class ImportedSuite:
    """A suite and the trials one import put into it."""

    benchmark_id: str
    source_artifact_id: str
    trials: tuple[NormalizedTrial, ...]

    @property
    def unaddressable_trial_ids(self) -> tuple[str, ...]:
        return tuple(trial.external_trial_id for trial in self.trials if not trial.addressable)


class BenchmarkService:
    """One workspace's benchmark suites."""

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    # -- creation -------------------------------------------------------------

    async def create(
        self,
        *,
        source_kind: SourceKind,
        correlation_mode: CorrelationMode,
        manifest_fields: Mapping[str, Any] | None = None,
        normalizer_version: str = "1",
    ) -> str:
        """A new suite in `draft` (§16.4's entry state).

        The manifest is written now and hashed now, because FR-093 makes it the
        reproducibility record: a manifest assembled at finalization would
        describe the environment at *that* moment rather than the one the trials
        came from.
        """
        benchmark_id = new_id("bench")
        manifest = BenchmarkManifest(
            source_kind=source_kind,
            correlation_mode=correlation_mode,
            benchmark_id=benchmark_id,
            **dict(manifest_fields or {}),
        )
        await self._work.execute(
            """
            INSERT INTO benchmark_suites (
                id, workspace_id, schema_version, source_kind, manifest_content_hash,
                manifest_json, correlation_mode, status, normalized_adapter_version,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                benchmark_id,
                self._workspace_id,
                MANIFEST_SCHEMA_VERSION,
                source_kind.value,
                manifest.content_hash(),
                canonical_text(manifest.canonical_document()),
                correlation_mode.value,
                BenchmarkStatus.DRAFT.value,
                normalizer_version,
                self._work.now(),
            ),
        )
        return benchmark_id

    async def record_import(
        self,
        benchmark_id: str,
        *,
        source_artifact_id: str,
        trials: Sequence[NormalizedTrial],
    ) -> ImportedSuite:
        """Store one import's normalized trials against a draft suite.

        Every trial arrives excluded — normalization cannot make one eligible,
        because the outcome layer has not run. What lands here is the call-level
        half plus whatever coverage reason already applies.
        """
        suite = await self._draft(benchmark_id)
        declared = CorrelationMode(str(suite["correlation_mode"]))
        for trial in trials:
            if trial.correlation_mode is not declared:
                # §9.9: the two mode populations "shall never be aggregated into
                # one rate". A suite holding both could not report either.
                raise ApiError(
                    ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                    f"this suite is {declared.value}; trial "
                    f"{trial.external_trial_id} is {trial.correlation_mode.value}",
                )
            await self._insert_trial(benchmark_id, source_artifact_id, trial)
        return ImportedSuite(
            benchmark_id=benchmark_id,
            source_artifact_id=source_artifact_id,
            trials=tuple(trials),
        )

    # -- binding --------------------------------------------------------------

    async def bind(
        self,
        benchmark_id: str,
        binding: TrialBinding,
        *,
        acknowledge_unaddressable: bool = False,
    ) -> None:
        """Save one explicit one-to-one binding, or refuse it.

        Every refusal below leaves the trial exactly as it was. FR-091 forbids
        silently downgrading or rebinding, and a partial update here would be
        the same thing wearing a different name.

        `acknowledge_unaddressable` is what makes FR-091's "explicit one-to-one
        developer choice" enforceable rather than decorative. A trial with no
        stable address of its own carries a *positional* id, and a caller who
        passed `#0` believing it identified something would be binding by list
        position — the exact inference the requirement forbids. Requiring the
        acknowledgement means the caller has to say, in the request, that they
        know they are choosing rather than addressing.
        """
        suite = await self._draft(benchmark_id)
        declared = CorrelationMode(str(suite["correlation_mode"]))
        if binding.correlation_mode is not declared:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                f"this suite is {declared.value}; the binding declares "
                f"{binding.correlation_mode.value}",
            )

        trial = await self._trial(benchmark_id, binding.external_trial_id)
        if trial is None:
            raise ApiError(
                ApiErrorCode.RESOURCE_NOT_FOUND,
                f"no trial {binding.external_trial_id!r} in this benchmark",
            )
        if not _addressable(trial) and not acknowledge_unaddressable:
            raise ApiError(
                ApiErrorCode.TRIAL_BINDING_AMBIGUOUS,
                f"trial {binding.external_trial_id!r} has no stable address of its "
                "own — its id is positional. FR-091 permits binding it only by an "
                "explicit one-to-one choice, which this request must acknowledge.",
                details=[
                    {
                        "path": "external_trial_id",
                        "message": "resend with the unaddressable acknowledgement to "
                        "confirm this is a deliberate choice, not a position",
                    }
                ],
            )
        if str(trial["outcome_run_id"] or "") or str(trial["evaluation_run_id"] or ""):
            raise ApiError(
                ApiErrorCode.TRIAL_ALREADY_BOUND,
                f"trial {binding.external_trial_id!r} is already bound; changing a "
                "binding requires a new suite (§16.4)",
            )

        if binding.correlation_mode is CorrelationMode.EXECUTED_BROWSER:
            await self._check_outcome_run(benchmark_id, binding)
            await self._save_binding(benchmark_id, binding, column="outcome_run_id")
            return
        await self._save_binding(benchmark_id, binding, column="evaluation_run_id")

    async def _check_outcome_run(self, benchmark_id: str, binding: TrialBinding) -> None:
        """The run must exist, be terminal, be in this workspace, and be unused.

        Workspace membership is checked by *querying within the workspace*
        rather than by reading a row and comparing: a run belonging to someone
        else must be indistinguishable from one that does not exist, which is
        the same rule 004 applies to every other resource.
        """
        row = await self._work.fetch_one(
            "SELECT status FROM runs WHERE id = ? AND workspace_id = ?",
            (binding.outcome_run_id, self._workspace_id),
        )
        if row is None:
            raise ApiError(
                ApiErrorCode.RESOURCE_NOT_FOUND,
                f"no run {binding.outcome_run_id!r} in this workspace",
            )
        status = str(row["status"])
        if status not in _BINDABLE_RUN_STATES:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                f"run {binding.outcome_run_id!r} is {status}; §17.1 binds a trial to "
                "the exact *completed* outcome run, and an in-flight run has no "
                "verdict to bind to",
            )
        used = await self._work.fetch_one(
            "SELECT external_trial_id FROM benchmark_trials "
            "WHERE benchmark_suite_id = ? AND outcome_run_id = ?",
            (benchmark_id, binding.outcome_run_id),
        )
        if used is not None:
            raise ApiError(
                ApiErrorCode.TRIAL_ALREADY_BOUND,
                f"run {binding.outcome_run_id!r} is already bound to trial "
                f"{used['external_trial_id']!r}; §17.1 forbids counting one source "
                "run twice in a benchmark",
            )

    async def _save_binding(self, benchmark_id: str, binding: TrialBinding, *, column: str) -> None:
        # The column name is chosen from two literals above, never from input.
        reference = (
            binding.outcome_run_id if column == "outcome_run_id" else binding.evaluation_run_id
        )
        await self._work.execute(
            f"UPDATE benchmark_trials SET {column} = ? "
            "WHERE benchmark_suite_id = ? AND external_trial_id = ?",
            (reference, benchmark_id, binding.external_trial_id),
        )

    # -- sealing --------------------------------------------------------------

    async def seal(self, benchmark_id: str) -> BenchmarkStatus:
        """`draft` → `ready`, after which bindings are immutable (§16.4).

        An unaddressable trial that nobody bound does not block this: FR-091
        makes it `excluded`, and a suite is allowed to have coverage gaps as
        long as it reports them. What it must not do is invent the binding.
        """
        suite = await self._draft(benchmark_id)
        target = require_transition(
            BenchmarkStatus(str(suite["status"])),
            BenchmarkStatus.READY,
            correlation_mode=CorrelationMode(str(suite["correlation_mode"])),
        )
        await self._mark_unbound_excluded(benchmark_id)
        await self._work.execute(
            "UPDATE benchmark_suites SET status = ? WHERE id = ? AND workspace_id = ?",
            (target.value, benchmark_id, self._workspace_id),
        )
        return target

    async def _mark_unbound_excluded(self, benchmark_id: str) -> None:
        """FR-091: an unbound trial is `excluded`, and the reason says why.

        Written at sealing rather than at import because until the suite closes
        for binding, "unbound" means "not yet", and recording it as a coverage
        gap early would make the gap look permanent while it was still fillable.
        """
        await self._work.execute(
            "UPDATE benchmark_trials SET eligibility = ?, exclusion_reason = ? "
            "WHERE benchmark_suite_id = ? AND outcome_run_id IS NULL "
            "AND evaluation_run_id IS NULL AND exclusion_reason = ?",
            (
                TrialEligibility.EXCLUDED.value,
                ExclusionReason.UNBOUND.value,
                benchmark_id,
                ExclusionReason.OUTCOME_NOT_REACHED.value,
            ),
        )

    # -- reads ----------------------------------------------------------------

    async def get(self, benchmark_id: str) -> Mapping[str, Any]:
        suite = await self._work.fetch_one(
            "SELECT * FROM benchmark_suites WHERE id = ? AND workspace_id = ?",
            (benchmark_id, self._workspace_id),
        )
        if suite is None:
            raise ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, f"no benchmark {benchmark_id!r} here")
        return suite

    async def summarize(self, benchmark_id: str) -> BenchmarkSummary:
        """FR-092's counts, metrics, and breakdowns for this suite.

        Read from the stored trials every time rather than cached on the suite:
        until finalization the numbers are a *view*, and a cached copy would go
        stale the moment a replay landed. FR-094 makes the finalized artifact
        the immutable one; this is what it is computed from.
        """
        return summarize(await self.trials(benchmark_id))

    async def trials(self, benchmark_id: str) -> tuple[Mapping[str, Any], ...]:
        await self.get(benchmark_id)
        rows = await self._work.fetch_all(
            "SELECT * FROM benchmark_trials WHERE benchmark_suite_id = ? "
            "ORDER BY created_at, external_trial_id",
            (benchmark_id,),
        )
        return tuple(rows)

    # -- internals ------------------------------------------------------------

    async def _draft(self, benchmark_id: str) -> Mapping[str, Any]:
        """The suite, but only while it still accepts changes."""
        suite = await self.get(benchmark_id)
        status = BenchmarkStatus(str(suite["status"]))
        if status not in _MUTABLE_STATUSES:
            raise ApiError(
                ApiErrorCode.BENCHMARK_BINDINGS_SEALED,
                f"this benchmark is {status.value}; §16.4 makes bindings immutable "
                "from `ready` onward, and a change requires a new suite",
            )
        return suite

    async def _trial(self, benchmark_id: str, external_trial_id: str) -> Mapping[str, Any] | None:
        return await self._work.fetch_one(
            "SELECT * FROM benchmark_trials WHERE benchmark_suite_id = ? AND external_trial_id = ?",
            (benchmark_id, external_trial_id),
        )

    async def _insert_trial(
        self, benchmark_id: str, source_artifact_id: str, trial: NormalizedTrial
    ) -> None:
        await self._work.execute(
            """
            INSERT INTO benchmark_trials (
                id, benchmark_suite_id, external_source_artifact_id, external_trial_id,
                scenario_id, contract_content_hash, scenario_mode, failure_profile,
                correlation_mode, call_level_result, outcome_result, eligibility,
                exclusion_reason, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("trial"),
                benchmark_id,
                source_artifact_id,
                trial.external_trial_id,
                trial.scenario_id,
                trial.contract_content_hash,
                trial.scenario_mode,
                trial.failure_profile,
                trial.correlation_mode.value,
                trial.call_level_result.value,
                trial.outcome_result.value,
                trial.eligibility.value,
                None if trial.exclusion_reason is None else trial.exclusion_reason.value,
                # The trajectory travels with the trial so replay has something
                # to execute, beside the `null` metadata FR-093 preserves.
                json.dumps(
                    {
                        "metadata": dict(trial.metadata),
                        "trajectory": [dict(step) for step in trial.trajectory],
                        "addressable": trial.addressable,
                    }
                ),
                self._work.now(),
            ),
        )


def _addressable(trial: Mapping[str, Any]) -> bool:
    """Whether this stored trial carried a stable address at import.

    Read back out of the stored metadata rather than re-derived, because the
    report it came from is no longer in hand — and re-deriving would mean
    guessing from the id's *shape*, which is the thing that must never be
    evidence.
    """
    try:
        stored = json.loads(str(trial["metadata_json"]) or "{}")
    except json.JSONDecodeError:  # pragma: no cover - written by this module
        return False
    return bool(stored.get("addressable", False))
