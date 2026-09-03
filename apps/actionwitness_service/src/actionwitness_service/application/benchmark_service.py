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

**A binding is only half of FR-091 — sealing is where it pays.** FR-091 binds an
`executed_browser` trial "one-to-one to the exact completed outcome `run_id`",
and FR-092 then needs that run's verdict as the trial's outcome layer, or the
two-by-two has nothing to count. `seal` is where the run's verdict is read
across and written into the trial; `_derive_bound_outcomes` says why that moment
and not another.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from actionwitness_core.benchmarks.approval import freeze
from actionwitness_core.benchmarks.enums import (
    BenchmarkStatus,
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    OutcomeTrialResult,
    SourceKind,
    TrialEligibility,
    outcome_from_layer_result,
)
from actionwitness_core.benchmarks.matrix import exclusion_for
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
from actionwitness_core.journeys.enums import RunState
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.security.canonical import canonical_text

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.benchmark_metrics import (
    BenchmarkSummary,
    summarize,
    trial_from_row,
)
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import new_id

__all__ = [
    "MAX_TRIAL_REPETITIONS",
    "BenchmarkService",
    "ImportedSuite",
    "PreparedFinalization",
    "PreparedImport",
    "RepetitionPlan",
    "write_benchmark_report",
]

#: How many repetitions of one variant a single request may run (§26.5).
#:
#: The Tier 3 showcase runs "six intent variants with five repeated trials
#: each"; this doubles that per request so a suite can be deepened without
#: changing the number, and refuses anything above rather than truncating to it.
#:
#: A named ceiling rather than whatever a caller sends, because the cost is not
#: the caller's to choose: each repetition mints its own eval workspace, restores
#: a fixture through the adapter, and replays a whole journey. An unbounded `n`
#: would let one request hold a target and a database for an unbounded time and
#: leave an unbounded number of workspaces behind — FR-008's ceilings exist for
#: exactly that class of request, and this is the benchmark-shaped one.
MAX_TRIAL_REPETITIONS = 10

#: `NormalizedTrial.external_trial_id` is bounded at 128 characters, and a
#: repetition's id is composed from its source's. A source id long enough to
#: overflow the composition is refused up front rather than stored as a row that
#: `trial_from_row` would later fail to read back.
_MAX_TRIAL_ID_CHARS = 128

#: Run states an `executed_browser` binding may point at. §17.1 requires "the
#: exact completed outcome `run_id`", so an in-flight run is not bindable: its
#: verdict does not exist yet, and binding to it would reserve a result.
_BINDABLE_RUN_STATES: frozenset[str] = frozenset(
    {
        RunState.PASSED.value,
        RunState.PASSED_WITH_WARNINGS.value,
        RunState.FAILED.value,
        RunState.ERROR.value,
    }
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


@dataclass(frozen=True, slots=True)
class PreparedImport:
    """What an import needs from the suite, read before anything is written.

    ADR-0003 puts the source artifact's file write *between* the read and the
    recording transaction, so the refusals that used to happen after the write
    have to happen before it. Reading the mode here is what lets normalization
    and the write run outside a transaction; `record_import` re-checks the same
    draft rule when it commits.
    """

    correlation_mode: CorrelationMode
    source_kind: str


@dataclass(frozen=True, slots=True)
class RepetitionPlan:
    """Everything N repetitions of one variant need, decided before any write.

    Assembled by `plan_repetitions`, which is where every refusal lives, and
    consumed one repetition at a time by `record_repetition`. The split is
    ADR-0003's: the replay between two repetitions is I/O, so no transaction may
    span the batch, and a plan is what survives across the gap.

    `call_level_result` travels on the plan because it is *copied*, never
    re-measured. The evaluator scored this variant's intent once; every
    repetition carries that same self-report, and `source_external_trial_id`
    records which trial it came from. Re-deriving it per repetition would turn
    one evaluator statement into N, which is precisely the promotion of a
    self-report to independent evidence the constitution forbids.
    """

    benchmark_id: str
    source_external_trial_id: str
    source_artifact_id: str
    scenario_id: str
    correlation_mode: CorrelationMode
    call_level_result: CallLevelResult
    contract_content_hash: str | None
    scenario_mode: str | None
    failure_profile: str | None
    #: The source trial's stored metadata, carried across verbatim. It holds the
    #: trajectory a repetition replays; re-deriving it would need the evaluator
    #: report, which is no longer in hand.
    metadata_json: str
    #: Which frozen variant this repeats, or `None` when the suite froze none.
    variant_index: int | None
    count: int
    #: Where this batch's numbering starts, so a second batch continues the
    #: sequence instead of colliding with the first one's identifiers.
    first_repetition_index: int

    def repetition_ids(self) -> tuple[int, ...]:
        return tuple(range(self.first_repetition_index, self.first_repetition_index + self.count))

    def external_trial_id(self, repetition_index: int) -> str:
        """This repetition's identity within the suite.

        Derived from the source id so a reader can see at a glance which trial a
        repetition repeats, and numbered rather than randomised so the same batch
        run twice against the same suite collides loudly instead of quietly
        producing a second population.
        """
        return f"{self.source_external_trial_id}#repetition-{repetition_index}"


@dataclass(frozen=True, slots=True)
class PreparedFinalization:
    """Everything finalization decided, before anything is written.

    §16.4's atomicity is unchanged by the split: every refusal — the transition,
    the trials, `BenchmarkReport`'s own validators — still happens here, before
    a byte reaches disk, so a refusal leaves no file and no row.
    """

    benchmark_id: str
    report: BenchmarkReport
    correlation_mode: CorrelationMode
    source_kind: str
    target_status: BenchmarkStatus
    source_artifact_id: str | None


def write_benchmark_report(store: Any, workspace_id: str, prepared: PreparedFinalization) -> Any:
    """FR-094's derived artifact, written outside every transaction (ADR-0003).

    Deliberately a free function rather than a `BenchmarkService` method: the
    service is bound to a `UnitOfWork`, and this must run when no unit of work is
    open. A file write inside `BEGIN IMMEDIATE` holds SQLite's single writer
    against every other workspace for as long as the write takes.
    """
    return store.write(
        workspace_id,
        prepared.benchmark_id,
        prepared.report.as_stored_document(),
        artifact_type="benchmark_report",
        schema_version=BENCHMARK_SCHEMA_VERSION,
    )


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

    async def prepare_import(self, benchmark_id: str) -> PreparedImport:
        """What normalization needs, refused here if the suite is past `draft`.

        ADR-0003 moves the source artifact's file write out of the recording
        transaction, which means the draft check has to run before it: a suite
        that refuses the import must not leave an evaluator report on disk that
        no trial references. `record_import` re-applies the same rule when it
        commits, so this is an early refusal rather than the only one.
        """
        suite = await self._draft(benchmark_id)
        return PreparedImport(
            correlation_mode=CorrelationMode(str(suite["correlation_mode"])),
            source_kind=str(suite["source_kind"]),
        )

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

    # -- repeated trials ------------------------------------------------------

    async def plan_repetitions(
        self,
        benchmark_id: str,
        *,
        source_external_trial_id: str,
        count: int,
        variant_index: int | None = None,
    ) -> RepetitionPlan:
        """Decide N repetitions of one variant, and write nothing (§26.5).

        **Every refusal is here.** The ceiling, the suite's state, the mode, the
        source trial, the variant, the composed identifier length: all of them
        happen before a byte is written, so a refused batch leaves no half-run
        population behind. `record_repetition` re-checks the state when it
        commits, because between two repetitions there is a replay, and ADR-0003
        forbids a transaction spanning it.

        **Only while the suite is `draft`.** §16.4 closes the population at
        `ready` — that is the moment `seal` derives every trial's outcome layer
        and FR-092's denominator stops moving. Adding repetitions afterwards
        would change the denominator of a matrix somebody has already read.

        **Only in `imported_trajectory_replay` mode.** An `executed_browser`
        trial is bound one-to-one to a browser execution that already happened;
        there is no second execution to repeat, and manufacturing one would
        attribute a fresh outcome to evidence from a different run (FR-091).

        **`variant_index` is named, never inferred.** FR-100 froze the variants
        in a definite order; which one a batch exercises is the caller's
        statement about what they are measuring, and guessing it from the source
        trial's text would be the same class of error as guessing a binding.
        """
        if count < 1 or count > MAX_TRIAL_REPETITIONS:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                f"a repeated-trial batch runs between 1 and {MAX_TRIAL_REPETITIONS} "
                f"trials; this one asked for {count}. Run several batches if you need "
                "a deeper population — each one is recorded, so nothing is lost.",
                details=[{"path": "trials", "message": "outside the repetition ceiling"}],
            )

        suite = await self._draft(benchmark_id)
        mode = CorrelationMode(str(suite["correlation_mode"]))
        if mode is not CorrelationMode.IMPORTED_TRAJECTORY_REPLAY:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                f"this suite is {mode.value}; only an imported_trajectory_replay suite "
                "repeats a trial, because an executed_browser trial is bound to a "
                "browser execution that already happened and cannot be run again "
                "(FR-091).",
            )

        source = await self._trial(benchmark_id, source_external_trial_id)
        if source is None:
            raise ApiError(
                ApiErrorCode.RESOURCE_NOT_FOUND,
                f"no trial {source_external_trial_id!r} in this benchmark",
            )
        if source["repetition_index"] is not None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                f"trial {source_external_trial_id!r} is itself a repetition. Repeat the "
                "imported trial it came from, so every repetition in a population is a "
                "repetition of the same recorded intent.",
            )
        if not _has_trajectory(source["metadata_json"]):
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                f"trial {source_external_trial_id!r} carries no replayable trajectory, "
                "so there is nothing to run again.",
            )

        self._check_variant(suite, variant_index)
        first = await self._next_repetition_index(benchmark_id, source_external_trial_id)
        plan = RepetitionPlan(
            benchmark_id=benchmark_id,
            source_external_trial_id=source_external_trial_id,
            source_artifact_id=str(source["external_source_artifact_id"]),
            scenario_id=str(source["scenario_id"]),
            correlation_mode=mode,
            call_level_result=CallLevelResult(str(source["call_level_result"])),
            contract_content_hash=_optional(source["contract_content_hash"]),
            scenario_mode=_optional(source["scenario_mode"]),
            failure_profile=_optional(source["failure_profile"]),
            metadata_json=str(source["metadata_json"]),
            variant_index=variant_index,
            count=count,
            first_repetition_index=first,
        )
        longest = plan.external_trial_id(first + count - 1)
        if len(longest) > _MAX_TRIAL_ID_CHARS:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                f"repetitions of {source_external_trial_id!r} would need identifiers "
                f"longer than {_MAX_TRIAL_ID_CHARS} characters, which this suite cannot "
                "store or read back.",
                details=[{"path": "source_external_trial_id", "message": "too long to repeat"}],
            )
        return plan

    def _check_variant(self, suite: Mapping[str, Any], variant_index: int | None) -> None:
        """A named variant must exist in the set FR-100 froze.

        A suite with no frozen set may still be repeated — a Tier 2 fixture has
        no variants and its repetitions group by scenario — but a caller who
        names an index into a set that does not exist is describing a population
        this suite cannot have.
        """
        if variant_index is None:
            return
        stored = json.loads(str(suite["manifest_json"]))
        frozen = stored.get("frozen_variants")
        variants = frozen.get("variants") if isinstance(frozen, Mapping) else None
        available = len(variants) if isinstance(variants, list) else 0
        if variant_index < 0 or variant_index >= available:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                f"this suite has {available} frozen variants, so variant "
                f"{variant_index} is not one of them. FR-100 freezes the set before "
                "trials begin; a different set requires a new suite.",
                details=[{"path": "variant_index", "message": "no such frozen variant"}],
            )

    async def _next_repetition_index(self, benchmark_id: str, source_trial_id: str) -> int:
        """Where this batch's numbering starts, counted from what is stored.

        Read from the rows rather than from a counter the caller supplies, so a
        second batch continues the first one's sequence even if the two requests
        know nothing about each other. Numbering from one, because "repetition 1
        of 5" is what a reader is being shown.
        """
        row = await self._work.fetch_one(
            "SELECT MAX(repetition_index) AS highest FROM benchmark_trials "
            "WHERE benchmark_suite_id = ? AND source_external_trial_id = ?",
            (benchmark_id, source_trial_id),
        )
        highest = None if row is None else row["highest"]
        return 1 if highest is None else int(highest) + 1

    async def record_repetition(self, plan: RepetitionPlan, repetition_index: int) -> str:
        """Store one repetition, before it runs. Returns its row id.

        **Written before the replay, not after.** A row that appeared only on
        success would make a batch interrupted halfway look like a shorter batch
        that finished — and constitution §5 requires a partially completed
        operation to stay visible rather than be silently retried. This row lands
        as `excluded` / `not_reached`, which is the truth at the moment it is
        written: the outcome layer has not run. The replay makes it eligible, or
        it stays exactly this and says so.

        The suite's state is re-checked here rather than trusted from the plan.
        The batch does not hold the workspace lock across its replays (ADR-0003
        forbids holding a lock across a wait), so a suite that is sealed midway
        stops the batch at the next repetition instead of growing a population
        that was already closed.
        """
        await self._draft(plan.benchmark_id)
        trial_row_id = new_id("trial")
        await self._work.execute(
            """
            INSERT INTO benchmark_trials (
                id, benchmark_suite_id, external_source_artifact_id, external_trial_id,
                scenario_id, contract_content_hash, scenario_mode, failure_profile,
                correlation_mode, call_level_result, outcome_result, eligibility,
                exclusion_reason, metadata_json, variant_index, repetition_index,
                source_external_trial_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                trial_row_id,
                plan.benchmark_id,
                # The same immutable evaluator artifact the source trial cites.
                # A repetition adds no new evaluator evidence, so it must not
                # look like it references any.
                plan.source_artifact_id,
                plan.external_trial_id(repetition_index),
                plan.scenario_id,
                plan.contract_content_hash,
                plan.scenario_mode,
                plan.failure_profile,
                plan.correlation_mode.value,
                plan.call_level_result.value,
                OutcomeTrialResult.NOT_REACHED.value,
                TrialEligibility.EXCLUDED.value,
                ExclusionReason.OUTCOME_NOT_REACHED.value,
                plan.metadata_json,
                plan.variant_index,
                repetition_index,
                plan.source_external_trial_id,
                self._work.now(),
            ),
        )
        return trial_row_id

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

        Sealing is where the population closes, so it is also where each trial's
        outcome layer is settled: bound trials take their run's verdict, unbound
        ones become the coverage gap FR-091 requires. Both writes happen in this
        method's transaction, alongside the status change, so a suite is never
        `ready` with an outcome layer that only half ran.

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
        await self._derive_bound_outcomes(benchmark_id)
        await self._mark_unbound_excluded(benchmark_id)
        await self._work.execute(
            "UPDATE benchmark_suites SET status = ? WHERE id = ? AND workspace_id = ?",
            (target.value, benchmark_id, self._workspace_id),
        )
        return target

    async def _derive_bound_outcomes(self, benchmark_id: str) -> None:
        """FR-091's binding, turned into FR-092's outcome layer.

        FR-091 binds an `executed_browser` trial "one-to-one to the exact
        completed outcome `run_id`"; FR-092 then needs that run's verdict as the
        trial's outcome half, or the two-by-two counts nothing and every rate is
        `null`. This is the step that carries one across to the other. Without
        it a binding is a stored pointer nobody reads, and a suite finalizes with
        an all-zero matrix that looks like a clean result.

        **The verdict comes from the run, never from the report.** §12.10 and
        §5's rail keep an imported evaluator result a self-report: it is the
        channel under test, and the call-level axis is already where it is
        counted. Promoting it to the outcome axis as well would make both cells
        of the two-by-two the same source, and the matrix would be incapable of
        showing the disagreement it exists to show. Nothing here reads
        `call_level_result` except to let an evaluator error keep its own
        exclusion reason.

        **Why at sealing, not at binding or at finalization.**

        - Not at binding, because until the suite closes for binding the
          population is still growing, and eligibility is what FR-092 divides
          by. A draft that reported rates over a half-bound suite would be
          publishing a denominator that changes under the reader — the same
          reason `_mark_unbound_excluded` waits until here rather than calling a
          fillable gap permanent at import.
        - Not at finalization, because §16.4 makes that step atomic and
          ADR-0003 splits it into a read-only phase, a file write with no
          transaction open, and a commit. `prepare_finalize` runs on a reading
          connection and has nothing to write with, and moving the derivation
          into `seal_finalize` would put it after the report it feeds.
        - At sealing it is safe to read once and keep: `bind` accepts only a
          terminal run, and §16 gives terminal runs no outgoing transition, so
          the verdict this reads is the verdict finalization would have read.

        Naturally a no-op for an `imported_trajectory_replay` suite: `TrialBinding`
        forbids an outcome run in that mode, so no row matches. Those trials get
        their outcome layer from the replay, which is the only place it exists.
        """
        rows = await self._work.fetch_all(
            "SELECT * FROM benchmark_trials WHERE benchmark_suite_id = ? "
            "AND outcome_run_id IS NOT NULL ORDER BY external_trial_id",
            (benchmark_id,),
        )
        for row in rows:
            run = await self._work.fetch_one(
                "SELECT status, overall_result FROM runs WHERE id = ? AND workspace_id = ?",
                (str(row["outcome_run_id"]), self._workspace_id),
            )
            if run is None:  # pragma: no cover - `bind` refused a run not in this workspace
                outcome = OutcomeTrialResult.NOT_REACHED
            else:
                outcome = _outcome_of_run(str(run["status"]), run["overall_result"])
            # Eligibility is FR-092's own rule, so it is asked of the core rather
            # than restated here: an evaluator error keeps its own reason, an
            # excluded outcome says which kind, and anything else is a trial both
            # layers actually judged.
            reason = exclusion_for(
                trial_from_row(row).model_copy(update={"outcome_result": outcome})
            )
            await self._work.execute(
                "UPDATE benchmark_trials SET outcome_result = ?, eligibility = ?, "
                "exclusion_reason = ? WHERE id = ?",
                (
                    outcome.value,
                    (
                        TrialEligibility.EXCLUDED
                        if reason is not None
                        else TrialEligibility.ELIGIBLE
                    ).value,
                    None if reason is None else reason.value,
                    str(row["id"]),
                ),
            )

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

    async def prepare_finalize(self, benchmark_id: str) -> PreparedFinalization:
        """Decide the whole finalization, and write nothing (FR-094, §16.4).

        **Atomic in the sense §16.4 means it.** Everything that could refuse —
        the transition, the trials, the report's own validators — happens here,
        before anything is written, so a refusal leaves no partial result to
        clean up. `write_benchmark_report` then writes the file with no
        transaction open, and `seal_finalize` commits the artifact row and the
        suite's `result_artifact_id` together, so a reader never sees a completed
        suite pointing at nothing, or an artifact no suite claims.

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

        report = BenchmarkReport(
            benchmark_id=benchmark_id,
            manifest=manifest,
            counts=summary.counts,
            metrics=summary.metrics,
            by_scenario=summary.by_scenario,
            by_failure_profile=summary.by_failure_profile,
            trials=summary.trials,
        )
        return PreparedFinalization(
            benchmark_id=benchmark_id,
            report=report,
            correlation_mode=mode,
            source_kind=str(suite["source_kind"]),
            target_status=target,
            # FR-094's derived→source link. One report may draw on one imported
            # artifact today; the first is recorded here and every hash is in
            # the manifest, so nothing is lost if that ever becomes several.
            source_artifact_id=sources[0] if sources else None,
        )

    async def seal_finalize(
        self, benchmark_id: str, prepared: PreparedFinalization, written: Any, store: Any
    ) -> str:
        """Commit the derived artifact and the suite together (FR-094, §16.4).

        The transition is re-checked here rather than trusted from `prepared`.
        The caller holds the workspace lock across both transactions, so the
        status cannot move in between today — but this method is what actually
        writes `completed`, and a state check that lives only in the phase before
        the file write would be a check the committing transaction never made.
        """
        suite = await self.get(benchmark_id)
        target = require_transition(
            BenchmarkStatus(str(suite["status"])),
            BenchmarkStatus.COMPLETED,
            correlation_mode=CorrelationMode(str(suite["correlation_mode"])),
        )
        artifact_id = await store.record(
            self._work,
            self._workspace_id,
            None,
            written,
            metadata={
                "correlation_mode": prepared.correlation_mode.value,
                "source_kind": prepared.source_kind,
            },
            benchmark_suite_id=benchmark_id,
            source_artifact_id=prepared.source_artifact_id,
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

    async def list_suites(self) -> list[Mapping[str, Any]]:
        """Every suite this workspace owns, newest first.

        A summary row rather than the whole suite: a listing exists so somebody
        can *choose* one, and the matrix, metrics and trials that `get` returns
        are the answer to a question they have not asked yet. `created_at` with
        the id as tie-break, because timestamps here have coarse granularity and
        two suites made inside one tick would otherwise order by whichever way
        SQLite happened to scan them.
        """
        rows = await self._work.fetch_all(
            "SELECT id, status, source_kind, correlation_mode, result_artifact_id, created_at "
            "FROM benchmark_suites WHERE workspace_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (self._workspace_id,),
        )
        return [dict(row) for row in rows]

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
        # Ordered by the trial's own identifier alone, never by `created_at`.
        # Timestamps have coarse granularity: nine rows written inside one tick
        # sort by id, and nine that straddle a tick sort by insertion — so a
        # `created_at` term makes the order depend on how fast the machine was.
        #
        # That would reach further than a listing. FR-094 hashes the finalized
        # report over its trials, so a timing-dependent order makes the
        # artifact's content hash timing-dependent too, and two identical
        # benchmarks would disagree about their own identity. The constitution
        # requires collections used in hashes to be normalized deterministically;
        # this is that normalization.
        rows = await self._work.fetch_all(
            "SELECT * FROM benchmark_trials WHERE benchmark_suite_id = ? "
            "ORDER BY external_trial_id",
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


def _outcome_of_run(status: str, overall_result: Any) -> OutcomeTrialResult:
    """One completed run's authoritative verdict, in the trial's vocabulary.

    §17.1 splits a run's ending across two columns and never merges them:
    `overall_result` is the business verdict `VerificationService` writes at the
    seal, and `status` is the lifecycle state it ended in. Only the first is read
    as a verdict here. The second is consulted for exactly one thing — telling
    "the outcome layer ran and broke" from "the outcome layer reached no verdict
    at all" — because FR-092 excludes both but §9.9's coverage has to say which,
    and `error_trials` is a disclosed subset rather than a silent one.

    `overall_result` is NULL precisely when a run reached no business verdict:
    the target could not be observed, or FR-008's event ceiling tripped. Neither
    is a pass, and §5's rail is explicit that an observation failure "produces an
    explicit non-pass result; it never degrades to success" — so the absence of a
    verdict is read as an absence, never filled in from the run's status.
    """
    if overall_result is not None:
        try:
            return outcome_from_layer_result(LayerResult(str(overall_result)))
        except ValueError:  # pragma: no cover - only the verification seal writes this
            # A stored verdict outside §23.1's vocabulary is a record this module
            # cannot read. Ambiguity is a non-pass, so it is disclosed as an
            # outcome error rather than guessed at or treated as absent.
            return OutcomeTrialResult.ERROR
    return (
        OutcomeTrialResult.ERROR
        if status == RunState.ERROR.value
        else OutcomeTrialResult.NOT_REACHED
    )


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)


def _has_trajectory(metadata_json: Any) -> bool:
    """Whether this stored trial carries calls a repetition could re-execute.

    Read here rather than through `benchmark_replay.stored_trajectory`, which
    would make these two modules import each other: the repetition runner is the
    one that reaches back into this service, and the dependency has to point one
    way. The question asked is deliberately narrower than that function's — this
    only needs to know whether *anything* replayable is recorded, and the runner
    still refuses a malformed trajectory on its own terms.
    """
    try:
        stored = json.loads(str(metadata_json or "{}"))
    except json.JSONDecodeError:  # pragma: no cover - written by this module
        return False
    trajectory = stored.get("trajectory") if isinstance(stored, dict) else None
    return isinstance(trajectory, list) and bool(trajectory)


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
