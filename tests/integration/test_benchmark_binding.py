"""008-T5 — explicit one-to-one trial binding (FR-091, §16.4, §26.5).

§26.5 names the three rejections this stage exists for: "reject duplicate,
cross-workspace, or ambiguous trial bindings". Each gets its own test and its
own error code, because a caller has to tell them apart — an ambiguous binding
needs a human to choose, a duplicate needs a different trial, and a sealed suite
needs a whole new suite.

The property underneath all of them is a *negative* one: FR-091 says the
importer "shall never guess a binding from list position, similar text, or
timestamps alone". A negative is hard to test directly, so it is tested as the
absence of a convenience — there is no method here that takes a suite and
produces bindings, and `test_nothing_binds_a_trial_without_being_told_to` pins
that down.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from actionwitness_core.benchmarks.enums import (
    BenchmarkStatus,
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    SourceKind,
    TrialEligibility,
)
from actionwitness_core.benchmarks.models import NormalizedTrial, TrialBinding
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.benchmark_service import BenchmarkService
from actionwitness_service.persistence.database import Database

# Lane marker only: this module mixes one sync test with async ones, and a
# module-level `asyncio` mark would be applied to the sync one too.
# `asyncio_mode = "auto"` already runs the async tests.
pytestmark = pytest.mark.integration

BROWSER = CorrelationMode.EXECUTED_BROWSER
REPLAY = CorrelationMode.IMPORTED_TRAJECTORY_REPLAY


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    db = Database(tmp_path / "harness.sqlite3")
    await db.initialize()
    yield db


async def _workspace(database: Database, workspace_id: str) -> None:
    async with database.transaction() as work:
        await work.execute(
            "INSERT INTO workspaces (id, kind, created_at, last_seen_at) VALUES (?,?,?,?)",
            (workspace_id, "interactive", work.now(), work.now()),
        )


async def _run(database: Database, workspace_id: str, run_id: str, status: str) -> None:
    """A minimal terminal run row for an `executed_browser` binding to name."""
    async with database.transaction() as work:
        columns = await work.fetch_all("PRAGMA table_info(runs)")
        required = {
            str(row["name"]): row for row in columns if row["notnull"] and row["dflt_value"] is None
        }
        values: dict[str, object] = {
            "id": run_id,
            "workspace_id": workspace_id,
            "status": status,
        }
        for name in required:
            values.setdefault(name, work.now() if "_at" in name else "x")
        names = ", ".join(values)
        holes = ", ".join("?" for _ in values)
        await work.execute(
            f"INSERT INTO runs ({names}) VALUES ({holes})",
            tuple(values.values()),
        )


async def _artifact(database: Database, workspace_id: str, artifact_id: str) -> None:
    async with database.transaction() as work:
        await work.execute(
            "INSERT INTO artifacts (id, workspace_id, artifact_type, schema_version, "
            "content_hash, metadata_json, relative_path, created_at) "
            "VALUES (?, ?, 'evaluator_report', '1.0', 'sha256:x', '{}', 'p', ?)",
            (artifact_id, workspace_id, work.now()),
        )


def _trial(
    trial_id: str,
    *,
    mode: CorrelationMode = BROWSER,
    addressable: bool = True,
    call: CallLevelResult = CallLevelResult.PASSED,
) -> NormalizedTrial:
    return NormalizedTrial(
        external_trial_id=trial_id,
        scenario_id="adds a mug",
        correlation_mode=mode,
        call_level_result=call,
        eligibility=TrialEligibility.EXCLUDED,
        exclusion_reason=ExclusionReason.OUTCOME_NOT_REACHED,
        addressable=addressable,
    )


async def _suite(
    database: Database,
    workspace_id: str = "ws-1",
    *,
    mode: CorrelationMode = BROWSER,
    trials: tuple[NormalizedTrial, ...] = (),
) -> str:
    await _workspace(database, workspace_id)
    await _artifact(database, workspace_id, f"art-{workspace_id}")
    async with database.transaction() as work:
        service = BenchmarkService(work, workspace_id)
        benchmark_id = await service.create(
            source_kind=SourceKind.RECORDED_FIXTURE, correlation_mode=mode
        )
        if trials:
            await service.record_import(
                benchmark_id, source_artifact_id=f"art-{workspace_id}", trials=trials
            )
    return benchmark_id


# --- the negative property ---------------------------------------------------


def test_nothing_binds_a_trial_without_being_told_to() -> None:
    """FR-091's prohibition, as the absence of the convenience that would break it.

    There is no method that takes a suite and produces bindings — no
    `autobind`, no `suggest`, nothing that pairs by position or similarity. A
    helper like that is how a benchmark ends up attributing one execution's
    outcome to another execution's call evidence, which is the exact error this
    product exists to catch.
    """
    # Arrange / Act
    surface = {name for name in dir(BenchmarkService) if not name.startswith("_")}

    # Assert
    assert surface == {
        "create",
        "record_import",
        "bind",
        "seal",
        "get",
        "trials",
        # The listing that made the dual-layer view reachable. It binds
        # nothing: it returns identity and status for suites this workspace
        # already owns, so a person can choose one instead of needing to know
        # its id. Deliberately thinner than `get` — a caller cannot build a
        # matrix from it, only pick the suite to read.
        "list_suites",
        # 008-T7. Reads the stored trials and reports FR-092's numbers; it
        # decides no binding and creates none.
        "summarize",
        # 008-T8. `ready` → `running`, finalization into the immutable
        # artifact, and §16.4's error path. None of them binds anything: by the
        # time a suite reaches `ready` its bindings are already frozen.
        "start",
        # ADR-0003 split finalization in three so the report's file write
        # happens with no transaction open. Neither half binds anything:
        # `prepare_finalize` reads and composes, `seal_finalize` records the
        # artifact the other one described. The same applies to
        # `prepare_import`, which only reads what normalization needs and
        # refuses a suite past `draft` before a byte is written.
        "prepare_import",
        "prepare_finalize",
        "seal_finalize",
        "mark_error",
        # 010-T6. Seals FR-100's approved variants into the manifest while the
        # suite is still `draft`. It binds no trial: no trial exists yet.
        "freeze_variants",
        # §26.5's repeated trials. Neither method binds anything, and the
        # distinction matters here more than anywhere else on this list: a
        # repetition *copies* its source trial's evaluator verdict, and the
        # caller names that source explicitly. Nothing pairs a repetition with a
        # run, a report, or another trial by position, similarity or time —
        # `plan_repetitions` refuses a source the caller did not name, and
        # `record_repetition` writes only what the plan already decided.
        "plan_repetitions",
        "record_repetition",
    }


# --- the happy path ----------------------------------------------------------


async def test_an_explicit_browser_binding_is_saved(database: Database) -> None:
    """FR-091: bound one-to-one to "the exact completed outcome `run_id`"."""
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    await _run(database, "ws-1", "run-1", "failed")

    # Act
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").bind(
            benchmark_id,
            TrialBinding(
                external_trial_id="adds a mug#0",
                correlation_mode=BROWSER,
                outcome_run_id="run-1",
            ),
        )

    # Assert
    async with database.transaction() as work:
        trials = await BenchmarkService(work, "ws-1").trials(benchmark_id)
    assert trials[0]["outcome_run_id"] == "run-1"


# --- §26.5's three rejections ------------------------------------------------


async def test_a_duplicate_run_binding_is_refused(database: Database) -> None:
    """§17.1: "a source run cannot be counted twice in one benchmark".

    Counting it twice would inflate whichever cell it lands in, and the
    benchmark's whole claim is that its counts are checkable.
    """
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"), _trial("adds a mug#1")))
    await _run(database, "ws-1", "run-1", "failed")
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").bind(
            benchmark_id,
            TrialBinding(
                external_trial_id="adds a mug#0",
                correlation_mode=BROWSER,
                outcome_run_id="run-1",
            ),
        )

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="adds a mug#1",
                    correlation_mode=BROWSER,
                    outcome_run_id="run-1",
                ),
            )
    assert refused.value.code is ApiErrorCode.TRIAL_ALREADY_BOUND


async def test_rebinding_one_trial_is_refused(database: Database) -> None:
    """The other half of "duplicate": one trial, two runs.

    FR-091 forbids rebinding, and §16.4 sends a changed binding to a new suite.
    """
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    await _run(database, "ws-1", "run-1", "failed")
    await _run(database, "ws-1", "run-2", "passed")
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").bind(
            benchmark_id,
            TrialBinding(
                external_trial_id="adds a mug#0",
                correlation_mode=BROWSER,
                outcome_run_id="run-1",
            ),
        )

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="adds a mug#0",
                    correlation_mode=BROWSER,
                    outcome_run_id="run-2",
                ),
            )
    assert refused.value.code is ApiErrorCode.TRIAL_ALREADY_BOUND


async def test_a_cross_workspace_run_is_indistinguishable_from_a_missing_one(
    database: Database,
) -> None:
    """§26.5's cross-workspace rejection, under 004's rule.

    Someone else's run must not be *reported* as someone else's — that would
    confirm it exists. It reads as absent, which is the same answer a
    nonexistent id gets.
    """
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    await _workspace(database, "ws-2")
    await _run(database, "ws-2", "run-elsewhere", "failed")

    # Act
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="adds a mug#0",
                    correlation_mode=BROWSER,
                    outcome_run_id="run-elsewhere",
                ),
            )
        with pytest.raises(ApiError) as absent:
            await BenchmarkService(work, "ws-1").bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="adds a mug#0",
                    correlation_mode=BROWSER,
                    outcome_run_id="run-never-existed",
                ),
            )

    # Assert
    assert refused.value.code is absent.value.code is ApiErrorCode.RESOURCE_NOT_FOUND


async def test_binding_an_unaddressable_trial_needs_an_explicit_acknowledgement(
    database: Database,
) -> None:
    """§26.5's "ambiguous" rejection, and FR-091's "explicit one-to-one
    developer choice" made enforceable.

    The trial's id is *positional*. A caller passing `#0` without saying so
    would be binding by list position — the inference FR-091 forbids.
    """
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("#0", addressable=False),))
    await _run(database, "ws-1", "run-1", "failed")
    binding = TrialBinding(external_trial_id="#0", correlation_mode=BROWSER, outcome_run_id="run-1")

    # Act / Assert — refused without the acknowledgement…
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").bind(benchmark_id, binding)
    assert refused.value.code is ApiErrorCode.TRIAL_BINDING_AMBIGUOUS

    # …and accepted with it, because a human has now chosen.
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").bind(
            benchmark_id, binding, acknowledge_unaddressable=True
        )
    async with database.transaction() as work:
        trials = await BenchmarkService(work, "ws-1").trials(benchmark_id)
    assert trials[0]["outcome_run_id"] == "run-1"


async def test_an_addressable_trial_needs_no_acknowledgement(database: Database) -> None:
    """The counterpart — otherwise the acknowledgement would be a rubber stamp
    on every binding, which teaches a caller to pass it without reading."""
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    await _run(database, "ws-1", "run-1", "failed")

    # Act
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").bind(
            benchmark_id,
            TrialBinding(
                external_trial_id="adds a mug#0",
                correlation_mode=BROWSER,
                outcome_run_id="run-1",
            ),
        )

    # Assert
    async with database.transaction() as work:
        trials = await BenchmarkService(work, "ws-1").trials(benchmark_id)
    assert trials[0]["outcome_run_id"] == "run-1"


# --- run eligibility ---------------------------------------------------------


async def test_an_in_flight_run_cannot_be_bound(database: Database) -> None:
    """§17.1 binds to "the exact *completed* outcome run".

    An in-flight run has no verdict, so binding to it would reserve a result
    before anything observed one.
    """
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    await _run(database, "ws-1", "run-live", "running")

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="adds a mug#0",
                    correlation_mode=BROWSER,
                    outcome_run_id="run-live",
                ),
            )
    assert refused.value.code is ApiErrorCode.PRECONDITION_FAILED


async def test_an_unknown_trial_is_refused(database: Database) -> None:
    """A binding names both sides; an id that names nothing binds nothing."""
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    await _run(database, "ws-1", "run-1", "failed")

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="never imported",
                    correlation_mode=BROWSER,
                    outcome_run_id="run-1",
                ),
            )
    assert refused.value.code is ApiErrorCode.RESOURCE_NOT_FOUND


# --- modes stay separate -----------------------------------------------------


async def test_a_suite_refuses_a_trial_from_the_other_mode(database: Database) -> None:
    """§9.9: the two mode populations "shall never be aggregated into one rate".

    A suite holding both could not honestly report either.
    """
    # Arrange
    await _workspace(database, "ws-1")
    await _artifact(database, "ws-1", "art-ws-1")

    # Act / Assert
    async with database.transaction() as work:
        service = BenchmarkService(work, "ws-1")
        benchmark_id = await service.create(
            source_kind=SourceKind.RECORDED_FIXTURE, correlation_mode=BROWSER
        )
        with pytest.raises(ApiError) as refused:
            await service.record_import(
                benchmark_id,
                source_artifact_id="art-ws-1",
                trials=(_trial("t#0", mode=REPLAY),),
            )
    assert refused.value.code is ApiErrorCode.CONTRACT_VALIDATION_FAILED


async def test_a_binding_declaring_the_other_mode_is_refused(database: Database) -> None:
    """FR-091 gives each trial exactly one mode, and the suite's is the one."""
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="adds a mug#0",
                    correlation_mode=REPLAY,
                    evaluation_run_id="eval-1",
                ),
            )
    assert refused.value.code is ApiErrorCode.CONTRACT_VALIDATION_FAILED


# --- sealing -----------------------------------------------------------------


async def test_sealing_moves_the_suite_to_ready(database: Database) -> None:
    """§16.4's `draft` → `ready`."""
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))

    # Act
    async with database.transaction() as work:
        status = await BenchmarkService(work, "ws-1").seal(benchmark_id)

    # Assert
    assert status is BenchmarkStatus.READY
    async with database.transaction() as work:
        suite = await BenchmarkService(work, "ws-1").get(benchmark_id)
    assert suite["status"] == "ready"


async def test_bindings_are_immutable_once_the_suite_is_ready(database: Database) -> None:
    """§16.4: "bindings become immutable when the suite enters `ready`"."""
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    await _run(database, "ws-1", "run-1", "failed")
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").seal(benchmark_id)

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="adds a mug#0",
                    correlation_mode=BROWSER,
                    outcome_run_id="run-1",
                ),
            )
    assert refused.value.code is ApiErrorCode.BENCHMARK_BINDINGS_SEALED


async def test_sealing_records_unbound_trials_as_a_coverage_gap(database: Database) -> None:
    """FR-091: a trial without sufficient evidence is `excluded`, with a reason.

    Recorded at sealing rather than at import, because until the suite closes
    for binding, "unbound" means "not yet" — writing it earlier would make a
    fillable gap look permanent.
    """
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"), _trial("adds a mug#1")))
    await _run(database, "ws-1", "run-1", "failed")
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").bind(
            benchmark_id,
            TrialBinding(
                external_trial_id="adds a mug#0",
                correlation_mode=BROWSER,
                outcome_run_id="run-1",
            ),
        )

    # Act
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").seal(benchmark_id)

    # Assert
    async with database.transaction() as work:
        trials = {
            str(row["external_trial_id"]): row
            for row in await BenchmarkService(work, "ws-1").trials(benchmark_id)
        }
    assert trials["adds a mug#1"]["exclusion_reason"] == ExclusionReason.UNBOUND.value
    assert trials["adds a mug#0"]["exclusion_reason"] == (ExclusionReason.OUTCOME_NOT_REACHED.value)


async def test_a_sealed_suite_refuses_a_further_import(database: Database) -> None:
    """§16.4: "a changed manifest, adapter, binding, or source artifact requires
    a new suite"."""
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    async with database.transaction() as work:
        await BenchmarkService(work, "ws-1").seal(benchmark_id)

    # Act / Assert
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await BenchmarkService(work, "ws-1").record_import(
                benchmark_id, source_artifact_id="art-ws-1", trials=(_trial("late#0"),)
            )
    assert refused.value.code is ApiErrorCode.BENCHMARK_BINDINGS_SEALED


# --- workspace isolation -----------------------------------------------------


async def test_another_workspace_cannot_see_or_bind_the_suite(database: Database) -> None:
    """004's isolation rule, applied to benchmarks (§12.4)."""
    # Arrange
    benchmark_id = await _suite(database, trials=(_trial("adds a mug#0"),))
    await _workspace(database, "ws-2")

    # Act / Assert
    async with database.transaction() as work:
        intruder = BenchmarkService(work, "ws-2")
        with pytest.raises(ApiError) as read:
            await intruder.get(benchmark_id)
        with pytest.raises(ApiError) as write:
            await intruder.bind(
                benchmark_id,
                TrialBinding(
                    external_trial_id="adds a mug#0",
                    correlation_mode=BROWSER,
                    outcome_run_id="run-1",
                ),
            )
    assert read.value.code is write.value.code is ApiErrorCode.RESOURCE_NOT_FOUND
