"""An `executed_browser` suite, from imported report to finalized matrix.

FR-091 binds one imported trial "one-to-one to the exact completed outcome
`run_id`"; FR-092 then needs that run's verdict as the trial's outcome half so
the two-by-two can be counted. This module drives the whole path — normalize,
import, bind, seal, finalize — against *real* completed runs, and asserts the
numbers that come out match the verdicts those runs actually reached.

**Why through the real thing rather than hand-built rows.** Every other
benchmark suite in this directory constructs `NormalizedTrial` values with
`outcome_result` and `eligibility` already filled in, which is a perfectly good
way to test the arithmetic and a perfectly bad way to notice that nothing fills
them in. A suite whose trials were bound to completed runs finalized with an
all-zero matrix and null rates, and every existing test agreed it was fine,
because every existing test supplied by hand the one thing the product never
derived. So nothing here sets those columns: the trials arrive from the
normalizer, the outcome layer arrives from the run, and the assertions are on
what the API reports afterwards.

**The two layers must be able to disagree.** §12.10 and §5's rail keep an
imported evaluator result a self-report — it is the channel under test, and it
is already counted on the call-level axis. `test_the_imported_report_never
_supplies_the_outcome_verdict` is the counterfactual: two trials the report
calls opposite things land on outcome verdicts that disagree with it in *both*
directions, which is impossible if one source fed both axes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI
from integrations.google_evals.pins import REPORTER_SCHEMA, REPORTER_VERSION

pytestmark = pytest.mark.integration

BENCHMARKS = f"{API_PREFIX}/benchmarks"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
WORKSPACE = f"{API_PREFIX}/workspace"

CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
FAULT = "discount_reported_but_not_applied"

#: The scenario each trial names, and the target configuration it ran under.
#: Declared by the benchmark rather than read from the report (§24.7 step 1).
SILENT = "reports the discount it did not apply"
CLEAN = "adds a mug and applies the discount"
WRONG = "reaches for the wrong tool"
LOOSE = "nobody bound this one"

SCENARIOS = [
    {"scenario_id": name, "scenario_mode": "pre_fix", "failure_profile": FAULT}
    for name in (SILENT, WRONG, LOOSE)
] + [{"scenario_id": CLEAN, "scenario_mode": "post_fix", "failure_profile": None}]


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    store = create_store(database_path=tmp_path / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ={
                "HARNESS_ENV": "local",
                "BUGGY_STORE_ENABLED": "true",
                "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            },
            database_path=tmp_path / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            yield harness


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


# --- real outcome runs -------------------------------------------------------


async def _select_contract(visitor: httpx.AsyncClient) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    selected = await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    assert selected.status_code == 200, selected.text


async def _completed_run(visitor: httpx.AsyncClient, *, mode: str, request_id: str) -> str:
    """One real journey, verified, returning the run a trial can bind to.

    The same three calls every time; only the scenario mode differs, so the two
    verdicts differ for the reason the demo exists to show rather than because
    the journeys were different. The workspace is reset first, which reseeds the
    target — without it the second run would start with the first run's cart and
    its verdict would be about state nobody arranged.

    The verdict is asserted here rather than in the test bodies: a run that
    reached the wrong terminal state would otherwise make the matrix assertions
    downstream fail for a reason that has nothing to do with the matrix.
    """
    await visitor.post(f"{WORKSPACE}/reset")
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})
    armed = await visitor.post(RUNS)
    assert armed.status_code == 201, armed.text
    run_id = str(armed.json()["run_id"])

    for tool, arguments in (
        ("search_catalog", {"query": "mug"}),
        ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": request_id}),
        ("apply_discount", {"code": "SAVE20"}),
    ):
        invoked = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
        assert invoked.status_code == 200, invoked.text

    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
    assert verdict.status_code == 200, verdict.text
    expected = "failed" if mode == "pre_fix" else "passed"
    assert verdict.json()["overall_result"] == expected, verdict.text
    return run_id


# --- the imported evaluator report -------------------------------------------


def _report(*trials: dict) -> bytes:
    document = {
        "config": {"reporterSchema": REPORTER_SCHEMA, "evaluatorVersion": REPORTER_VERSION},
        "results": {
            "results": list(trials),
            "testCount": len(trials),
            "passCount": 0,
            "failCount": 0,
            "errorCount": 0,
        },
    }
    return json.dumps(document).encode("utf-8")


def _trial(name: str, outcome: str, run_index: int = 0) -> dict:
    return {"test": {"name": name}, "outcome": outcome, "runIndex": run_index}


def _address(name: str, run_index: int = 0) -> str:
    """The id ADR-0005 gives a trial the report can name."""
    return f"{name}#{run_index}"


def _trial_url(benchmark_id: str, trial_id: str) -> str:
    """The trial-evidence path, with the id escaped.

    ADR-0005's addresses contain spaces and a `#`, and a `#` in a URL is the
    fragment delimiter — an unescaped id would leave the trial segment truncated
    at the run index and the request would ask for a trial nobody named.
    """
    return f"{BENCHMARKS}/{benchmark_id}/trials/{quote(trial_id, safe='')}"


async def _suite(visitor: httpx.AsyncClient, report: bytes) -> str:
    """A draft `executed_browser` suite holding one imported report's trials."""
    created = await visitor.post(
        BENCHMARKS,
        json={
            "source_kind": "external_import",
            "correlation_mode": "executed_browser",
            "scenarios": SCENARIOS,
        },
    )
    assert created.status_code == 201, created.text
    benchmark_id = str(created.json()["benchmark_id"])

    imported = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=report,
        headers={"content-type": "application/json"},
    )
    assert imported.status_code == 201, imported.text
    return benchmark_id


async def _bind_and_seal(
    visitor: httpx.AsyncClient, benchmark_id: str, bindings: dict[str, str]
) -> None:
    sealed = await visitor.put(
        f"{BENCHMARKS}/{benchmark_id}/bindings",
        json={
            "bindings": [
                {"external_trial_id": trial_id, "outcome_run_id": run_id}
                for trial_id, run_id in bindings.items()
            ],
            "seal": True,
        },
    )
    assert sealed.status_code == 200, sealed.text
    assert sealed.json()["status"] == "ready"


async def _finalize(visitor: httpx.AsyncClient, benchmark_id: str) -> dict:
    finalized = await visitor.post(f"{BENCHMARKS}/{benchmark_id}/finalize")
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["status"] == "completed"
    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
    assert read.status_code == 200, read.text
    return dict(read.json())


async def _standard_suite(visitor: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    """Three bound trials over three real runs, plus one nobody bound.

    The report and the runs disagree on purpose: `SILENT` is a call-level pass
    whose run failed, which is the cell this whole feature exists to surface.
    """
    await _select_contract(visitor)
    silent_run = await _completed_run(visitor, mode="pre_fix", request_id="req_mugone")
    clean_run = await _completed_run(visitor, mode="post_fix", request_id="req_mugtwo")
    wrong_run = await _completed_run(visitor, mode="pre_fix", request_id="req_mugthree")

    benchmark_id = await _suite(
        visitor,
        _report(
            _trial(SILENT, "pass"),
            _trial(CLEAN, "pass"),
            _trial(WRONG, "fail"),
            _trial(LOOSE, "pass"),
        ),
    )
    runs = {
        _address(SILENT): silent_run,
        _address(CLEAN): clean_run,
        _address(WRONG): wrong_run,
    }
    await _bind_and_seal(visitor, benchmark_id, runs)
    return benchmark_id, runs


# --- the end-to-end gap ------------------------------------------------------


async def test_a_bound_suite_finalizes_with_the_matrix_its_runs_earned(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-091 → FR-092: the binding feeds the arithmetic, or it feeds nothing.

    This is the test whose absence let an `executed_browser` suite finalize with
    an all-zero two-by-two and null rates while reporting success. Nothing here
    sets `outcome_result` or `eligibility`: they come from the three runs, which
    is the only place FR-091 permits them to come from.
    """
    # Arrange
    benchmark_id, _ = await _standard_suite(visitor)

    # Act
    body = await _finalize(visitor, benchmark_id)

    # Assert — the four cells, and the coverage around them.
    counts = body["counts"]
    assert counts["call_level_pass_outcome_pass"] == 1
    assert counts["call_level_pass_outcome_fail"] == 1
    assert counts["call_level_fail_outcome_pass"] == 0
    assert counts["call_level_fail_outcome_fail"] == 1
    assert counts["eligible_trials"] == 3
    assert counts["excluded_trials"] == 1
    assert counts["error_trials"] == 0
    assert counts["total_trials"] == 4

    # …and the rates FR-092 computes from them, over the population that exists.
    metrics = body["metrics"]
    assert metrics["call_level_pass_rate"]["value"] == "0.6667"
    assert metrics["outcome_pass_rate"]["value"] == "0.3333"
    assert metrics["end_to_end_success_rate"]["value"] == "0.3333"
    assert metrics["silent_outcome_failure_rate"]["value"] == "0.5000"
    assert metrics["incremental_outcome_failure_trials"] == 1


async def test_the_finalized_report_carries_the_same_matrix_as_the_suite(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-094: the derived artifact is what a reader downloads, so it must say
    the same thing the suite does.

    A derivation that ran only on the read path would leave these two
    disagreeing — the view would be right and the immutable record wrong, which
    is the worse half to get wrong.
    """
    # Arrange
    benchmark_id, _ = await _standard_suite(visitor)
    body = await _finalize(visitor, benchmark_id)

    # Act
    downloaded = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/report")

    # Assert
    assert downloaded.status_code == 200, downloaded.text
    document = json.loads(downloaded.content)
    assert document["counts"] == body["counts"]
    assert document["metrics"]["silent_outcome_failure_rate"]["value"] == "0.5000"


# --- the quadrants -----------------------------------------------------------


async def test_each_trial_lands_in_the_quadrant_its_own_run_earned(
    visitor: httpx.AsyncClient,
) -> None:
    """The cells, checked one trial at a time.

    `call_pass_outcome_fail` is the signal the product exists to surface: the
    evaluator was satisfied with every call, and the independently observed
    business state disagreed. A matrix that put it anywhere else would report the
    opposite of what happened.
    """
    # Arrange
    benchmark_id, runs = await _standard_suite(visitor)
    await _finalize(visitor, benchmark_id)

    # Act
    body = await _finalize_free_read(visitor, benchmark_id)

    # Assert
    assert body[_address(SILENT)] == ("passed", "failed", "eligible", None)
    assert body[_address(CLEAN)] == ("passed", "passed", "eligible", None)
    assert body[_address(WRONG)] == ("failed", "failed", "eligible", None)
    # Each verdict came from that trial's own run, never a neighbour's.
    trial = (await visitor.get(_trial_url(benchmark_id, _address(SILENT)))).json()
    assert trial["outcome_run_id"] == runs[_address(SILENT)]


async def _finalize_free_read(
    visitor: httpx.AsyncClient, benchmark_id: str
) -> dict[str, tuple[str, str, str, str | None]]:
    """Each trial's two layers, its eligibility, and its exclusion reason."""
    body = (await visitor.get(f"{BENCHMARKS}/{benchmark_id}")).json()
    return {
        str(trial["external_trial_id"]): (
            str(trial["call_level_result"]),
            str(trial["outcome_result"]),
            str(trial["eligibility"]),
            trial["exclusion_reason"],
        )
        for trial in body["trials"]
    }


# --- coverage, honestly ------------------------------------------------------


async def test_an_unbound_trial_stays_excluded_and_never_counts_as_a_pass(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-091: a trial without sufficient evidence is `excluded`, with a reason.

    The evaluator called this trial a pass. Nothing bound it to a run, so no
    observation exists — and §5's rail is that a missing observation is an
    explicit non-pass, never a degradation to success.
    """
    # Arrange
    benchmark_id, _ = await _standard_suite(visitor)

    # Act
    trials = await _finalize_free_read(visitor, benchmark_id)

    # Assert
    call_level, outcome, eligibility, reason = trials[_address(LOOSE)]
    assert call_level == "passed"
    assert outcome == "not_reached"
    assert eligibility == "excluded"
    assert reason == "unbound"
    # And it is absent from every cell rather than sitting quietly in one.
    body = await _finalize(visitor, benchmark_id)
    assert body["counts"]["eligible_trials"] == 3
    assert body["counts"]["excluded_trials"] == 1


# --- the two sources stay two sources ----------------------------------------


async def test_the_imported_report_never_supplies_the_outcome_verdict(
    visitor: httpx.AsyncClient,
) -> None:
    """§12.10: an imported result is a self-report, never promoted to an
    observation.

    The counterfactual is built into the arrangement. `SILENT` is a report
    `pass` bound to a run that failed; `WRONG` is a report `fail` bound to a run
    that also failed. If the report had fed the outcome axis, `SILENT` would read
    `passed` there. It reads `failed`, because the run did — and the call-level
    axis still reads `passed`, because that is what the report said and the two
    are not merged.
    """
    # Arrange
    benchmark_id, runs = await _standard_suite(visitor)

    # Act
    silent = (await visitor.get(_trial_url(benchmark_id, _address(SILENT)))).json()

    # Assert — the same trial carries two results from two sources, and they
    # disagree.
    assert silent["call_level_result"] == "passed"
    assert silent["outcome_result"] == "failed"
    # Source classification survives: the call-level half references the
    # immutable evaluator artifact, the outcome half references the run.
    assert silent["source_artifact_id"]
    assert silent["outcome_run_id"] == runs[_address(SILENT)]
    assert silent["source_artifact_id"] != silent["outcome_run_id"]
    # And the report's own word for this trial is still what it always was — the
    # derivation read the run, not the document.
    imported = (await visitor.get(_trial_url(benchmark_id, _address(WRONG)))).json()
    assert imported["call_level_result"] == "failed"
    assert imported["outcome_result"] == "failed"


# --- the other mode is untouched ---------------------------------------------


async def test_an_imported_trajectory_replay_suite_is_unaffected(
    visitor: httpx.AsyncClient,
) -> None:
    """The regression guard for the mode that already worked.

    A replay trial's outcome layer comes from the replay, which is the only
    place it exists — FR-091 forbids an outcome run in this mode, so the
    binding-derivation has nothing to match and must stay out of the way.
    """
    # Arrange
    await _select_contract(visitor)
    created = await visitor.post(
        BENCHMARKS,
        json={
            "source_kind": "external_import",
            "correlation_mode": "imported_trajectory_replay",
            "scenarios": [
                {"scenario_id": CLEAN, "scenario_mode": "post_fix", "failure_profile": None}
            ],
        },
    )
    assert created.status_code == 201, created.text
    benchmark_id = str(created.json()["benchmark_id"])
    document = _report(_trial(CLEAN, "pass"))
    # A replayable trajectory, which `executed_browser` trials do not carry.
    parsed = json.loads(document)
    parsed["results"]["results"][0]["trajectory"] = [
        {
            "name": "update_cart",
            "arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_replayone"},
        },
        {"name": "apply_discount", "arguments": {"code": "SAVE20"}},
    ]
    imported = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=json.dumps(parsed).encode("utf-8"),
        headers={"content-type": "application/json"},
    )
    assert imported.status_code == 201, imported.text
    await _bind_and_seal(visitor, benchmark_id, {})

    # Act
    replayed = await visitor.post(f"{BENCHMARKS}/{benchmark_id}/replay")
    assert replayed.status_code == 200, replayed.text
    body = await _finalize(visitor, benchmark_id)

    # Assert — the replay supplied the outcome layer, exactly as before.
    assert replayed.json()["replayed"][0]["eligibility"] == "eligible"
    assert body["counts"]["eligible_trials"] == 1
    assert body["counts"]["excluded_trials"] == 0
    trials = await _finalize_free_read(visitor, benchmark_id)
    assert trials[_address(CLEAN)][2] == "eligible"
