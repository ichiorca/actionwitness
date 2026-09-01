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

from actionwitness_core.benchmarks.approval import freeze
from actionwitness_core.benchmarks.enums import (
    BenchmarkStatus,
    CorrelationMode,
    ExclusionReason,
    SourceKind,
    TrialEligibility,
)
from actionwitness_core.benchmarks.models import (
    BENCHMARK_SCHEMA_VERSION,
    MANIFEST_SCHEMA_VERSION,
    BenchmarkManifest,
    BenchmarkReport,
    NormalizedTrial,
    ScenarioDefinition,
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
        scenarios: Sequence[ScenarioDefinition] = (),
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
            scenarios=tuple(scenarios),
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
        manifest_fields: Mapping[str, Any] | None = None,
    ) -> ImportedSuite:
        """Store one import's normalized trials against a draft suite.

        Every trial arrives excluded — normalization cannot make one eligible,
        because the outcome layer has not run. What lands here is the call-level
        half plus whatever coverage reason already applies.
        """
        suite = await self._draft(benchmark_id)
        declared = CorrelationMode(str(suite["correlation_mode"]))
        # §24.7 step 1: the scenario carries the target configuration, not the
        # evaluator report. Stamped on here so a replay runs against the mode
        # and fault the benchmark declared rather than whatever the target
        # happens to default to.
        scenarios = self._scenarios_of(suite)
        stamped: list[NormalizedTrial] = []
        for trial in trials:
            if trial.correlation_mode is not declared:
                # §9.9: the two mode populations "shall never be aggregated into
                # one rate". A suite holding both could not report either.
                raise ApiError(
                    ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                    f"this suite is {declared.value}; trial "
                    f"{trial.external_trial_id} is {trial.correlation_mode.value}",
                )
            configured = self._with_scenario(trial, scenarios)
            await self._insert_trial(benchmark_id, source_artifact_id, configured)
            stamped.append(configured)
        # FR-093's evaluator half is only knowable once a report has been read:
        # the reporter schema, the normalizer version, and whatever model
        # metadata the report carried. Merged into the manifest here rather than
        # at finalization, so the record describes what produced these trials.
        if manifest_fields:
            await self._merge_manifest(benchmark_id, suite, manifest_fields)
        return ImportedSuite(
            benchmark_id=benchmark_id,
            source_artifact_id=source_artifact_id,
            trials=tuple(stamped),
        )

    async def _merge_manifest(
        self, benchmark_id: str, suite: Mapping[str, Any], fields: Mapping[str, Any]
    ) -> None:
        """Add what the import learned, without overwriting what was declared.

        The scenarios and the source kind were the operator's decision at
        creation; the evaluator and model metadata are the report's. A blanket
        overwrite would let a report rename the populations it was imported
        into.
        """
        stored = json.loads(str(suite["manifest_json"]))
        declared = {"source_kind", "correlation_mode", "benchmark_id", "scenarios"}
        stored.update({key: value for key, value in fields.items() if key not in declared})
        manifest = BenchmarkManifest.model_validate(stored)
        await self._work.execute(
            "UPDATE benchmark_suites SET manifest_json = ?, manifest_content_hash = ? "
            "WHERE id = ? AND workspace_id = ?",
            (
                canonical_text(manifest.canonical_document()),
                manifest.content_hash(),
                benchmark_id,
                self._workspace_id,
            ),
        )

    def _scenarios_of(self, suite: Mapping[str, Any]) -> Mapping[str, ScenarioDefinition]:
        manifest = BenchmarkManifest.model_validate(json.loads(str(suite["manifest_json"])))
        return {scenario.scenario_id: scenario for scenario in manifest.scenarios}

    def _with_scenario(
        self, trial: NormalizedTrial, scenarios: Mapping[str, ScenarioDefinition]
    ) -> NormalizedTrial:
        """A trial plus the target configuration its scenario declares.

        A scenario the manifest does not describe leaves the trial as it is —
        `null`, never inferred (FR-093). The replay then runs against the
        target default and the trial says so, rather than being quietly
        attributed to a configuration nobody chose.
        """
        scenario = scenarios.get(trial.scenario_id)
        if scenario is None:
            return trial
        return trial.model_copy(
            update={
                "scenario_mode": scenario.scenario_mode,
                "failure_profile": scenario.failure_profile,
                "contract_content_hash": scenario.contract_content_hash,
            }
        )

    async def freeze_variants(self, benchmark_id: str, approved: Any) -> str:
        """FR-100: freeze approved variants into the manifest before trials.

        **Only while the suite is `draft`.** "Before trials begin" is the
        requirement, and `draft` is the only state in which no trial has been
        imported or replayed — so the state machine is what enforces the timing
        rather than a check on a clock.

        **Once, and not again.** FR-100 adds "generation is not rerun between
        repetitions": a suite whose variants changed midway would have
        repetitions that measured different things, and the manifest hash would
        describe none of them. A second freeze is refused rather than
        overwriting, because overwriting is precisely the rerun the requirement
        forbids.
        """
        suite = await self._draft(benchmark_id)
        stored = json.loads(str(suite["manifest_json"]))
        if stored.get("frozen_variants") is not None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "this benchmark already has frozen variants; FR-100 forbids "
                "rerunning generation between repetitions, so a different set "
                "requires a new suite",
            )

        frozen = freeze(approved)
        stored["frozen_variants"] = frozen.canonical_document()
        manifest = BenchmarkManifest.model_validate(stored)
        await self._work.execute(
            "UPDATE benchmark_suites SET manifest_json = ?, manifest_content_hash = ? "
            "WHERE id = ? AND workspace_id = ?",
            (
                canonical_text(manifest.canonical_document()),
                manifest.content_hash(),
                benchmark_id,
                self._workspace_id,
            ),
        )
        return frozen.content_hash()

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

    # -- running and finalizing -----------------------------------------------

    async def start(self, benchmark_id: str) -> BenchmarkStatus:
        """`ready` → `running` (§16.4), before replays execute.

        Only an `imported_trajectory_replay` suite needs this: §16.4 lets an
        `executed_browser` suite finalize straight from `ready` because its
        outcome runs already exist, while a replay suite's outcome evidence does
        not exist until the replay produces it.
        """
        suite = await self.get(benchmark_id)
        target = require_transition(BenchmarkStatus(str(suite["status"])), BenchmarkStatus.RUNNING)
        await self._set_status(benchmark_id, target)
        return target

    async def finalize(self, benchmark_id: str, store: Any) -> str:
        """FR-094: one immutable derived artifact, committed with the suite.

        **Atomic in the sense §16.4 means it.** Everything that could refuse —
        the transition, the trials, the report's own validators — happens before
        anything is written, so a refusal leaves no partial result to clean up.
        The artifact row and the suite's `result_artifact_id` then go into the
        same transaction as this call, so a reader never sees a completed suite
        pointing at nothing, or an artifact no suite claims.

        The report *references* its sources by hash and never contains them.
        §7's non-goal is explicit that an immutable source outcome report is
        never rewritten to embed evaluator data; recalculating later creates a
        new artifact beside the old sources rather than editing them.
        """
        suite = await self.get(benchmark_id)
        mode = CorrelationMode(str(suite["correlation_mode"]))
        target = require_transition(
            BenchmarkStatus(str(suite["status"])),
            BenchmarkStatus.COMPLETED,
            correlation_mode=mode,
        )

        rows = await self.trials(benchmark_id)
        summary = summarize(rows)
        sources = self._source_artifacts(rows)
        manifest = self._manifest_of(suite, summary, await self._hashes_of(sources))

        # Constructed before anything is written: `BenchmarkReport`'s own
        # validators refuse a mixed-mode population, and a refusal must not
        # leave a file or a row behind.
        report = BenchmarkReport(
            benchmark_id=benchmark_id,
            manifest=manifest,
            counts=summary.counts,
            metrics=summary.metrics,
            by_scenario=summary.by_scenario,
            by_failure_profile=summary.by_failure_profile,
            trials=summary.trials,
        )

        written = store.write(
            self._workspace_id,
            benchmark_id,
            report.as_stored_document(),
            artifact_type="benchmark_report",
            schema_version=BENCHMARK_SCHEMA_VERSION,
        )
        artifact_id = await store.record(
            self._work,
            self._workspace_id,
            None,
            written,
            metadata={"correlation_mode": mode.value, "source_kind": str(suite["source_kind"])},
            benchmark_suite_id=benchmark_id,
            # FR-094's derived→source link. One report may draw on one imported
            # artifact today; the first is recorded here and every hash is in
            # the manifest, so nothing is lost if that ever becomes several.
            source_artifact_id=sources[0] if sources else None,
        )
        await self._work.execute(
            "UPDATE benchmark_suites SET status = ?, result_artifact_id = ?, "
            "completed_at = ? WHERE id = ? AND workspace_id = ?",
            (target.value, artifact_id, self._work.now(), benchmark_id, self._workspace_id),
        )
        return artifact_id

    async def mark_error(self, benchmark_id: str) -> BenchmarkStatus:
        """§16.4: "the suite enters `error` without a partial result".

        Called by a caller whose finalization refused, in a *fresh* transaction
        — the one that failed has rolled back, and writing the error status into
        it would roll back too.
        """
        suite = await self.get(benchmark_id)
        target = require_transition(BenchmarkStatus(str(suite["status"])), BenchmarkStatus.ERROR)
        await self._set_status(benchmark_id, target)
        return target

    async def _set_status(self, benchmark_id: str, status: BenchmarkStatus) -> None:
        await self._work.execute(
            "UPDATE benchmark_suites SET status = ? WHERE id = ? AND workspace_id = ?",
            (status.value, benchmark_id, self._workspace_id),
        )

    def _source_artifacts(self, rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
        """The immutable evaluator artifacts this suite was computed from.

        Order-preserving and de-duplicated rather than a set, so the recorded
        order is the order the trials referenced them in and the manifest hashes
        identically across runs.
        """
        seen: list[str] = []
        for row in rows:
            artifact_id = str(row["external_source_artifact_id"])
            if artifact_id not in seen:
                seen.append(artifact_id)
        return tuple(seen)

    async def _hashes_of(self, artifact_ids: Sequence[str]) -> tuple[str, ...]:
        """Each source artifact's own content hash.

        Read from the artifact rows rather than recomputed: FR-094 makes the
        derived artifact reference "immutable source evaluator and outcome
        artifacts", and a hash this method recalculated would be a claim about
        the source rather than a reference to what was actually stored.
        """
        hashes: list[str] = []
        for artifact_id in artifact_ids:
            row = await self._work.fetch_one(
                "SELECT content_hash FROM artifacts WHERE id = ? AND workspace_id = ?",
                (artifact_id, self._workspace_id),
            )
            if row is not None:
                hashes.append(str(row["content_hash"]))
        return tuple(hashes)

    def _manifest_of(
        self, suite: Mapping[str, Any], summary: Any, source_hashes: Sequence[str]
    ) -> BenchmarkManifest:
        """The manifest recorded at creation, plus what finalization learned.

        The evaluator and model metadata are *not* re-read from configuration
        here: FR-093 records what produced the trials, and a manifest refreshed
        at finalization would describe this moment instead of that one.
        """
        stored = json.loads(str(suite["manifest_json"]))
        stored["scenario_ids"] = sorted({trial.scenario_id for trial in summary.trials})
        stored["source_artifact_hashes"] = list(source_hashes)
        return BenchmarkManifest.model_validate(stored)

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
