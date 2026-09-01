"""008-T11 — the checked-in evaluator fixture (AC-16, §25.3, §26.5).

AC-16 asks for "a checked-in, redacted report ... containing at least three
scenarios with at least three completed trials each, including one call-level
pass whose deterministic outcome fails", imported "without Node, an LLM API key,
Shopify, or a live model call".

The trial that matters is the `silent_outcome_defect`: the evaluator says the
model made every required call, and the independently observed business state
disagrees. That cell is the product's entire thesis, and a fixture that could
not produce it would make AC-16 a green light over nothing.

**The fixture provokes it honestly.** The discount scenario is declared to run
against `pre_fix` with the discount fault active, so the replay reaches a target
that reports success and does not apply the discount. Nothing here forces the
outcome — the contract engine decides it, exactly as it does on a live run.

Nothing in this module imports Node, a model client, Shopify, or a credential.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = REPO_ROOT / "integrations" / "google_evals" / "fixtures" / "tier2_three_scenarios.json"

BENCHMARKS = f"{API_PREFIX}/benchmarks"
CONTRACTS = f"{API_PREFIX}/contracts"
CANONICAL = "one_mug_save20_no_checkout"
FAULT = "discount_reported_but_not_applied"

FAULTY = "SAVE20 on one mug against the faulty build"
CORRECTED = "SAVE20 on one mug against the corrected build"
OMITTED = "SAVE20 on one mug, discount step omitted"

#: §24.7 step 1. The discount scenario runs against the faulty implementation,
#: which is what makes a silent outcome defect possible at all; the other two
#: run against the corrected one.
SCENARIOS = [
    {"scenario_id": FAULTY, "scenario_mode": "pre_fix", "failure_profile": FAULT},
    {"scenario_id": CORRECTED, "scenario_mode": "post_fix"},
    {"scenario_id": OMITTED, "scenario_mode": "post_fix"},
]


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


async def _run_the_fixture(visitor: httpx.AsyncClient) -> dict:
    """Import → seal → replay → finalize, and read the result back."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")

    created = await visitor.post(
        BENCHMARKS,
        json={"source_kind": "recorded_fixture", "scenarios": SCENARIOS},
    )
    benchmark_id = created.json()["benchmark_id"]

    imported = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=FIXTURE.read_bytes(),
        headers={"content-type": "application/json"},
    )
    assert imported.status_code == 201, imported.text

    await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})
    replayed = await visitor.post(f"{BENCHMARKS}/{benchmark_id}/replay")
    assert replayed.status_code == 200, replayed.text
    finalized = await visitor.post(f"{BENCHMARKS}/{benchmark_id}/finalize")
    assert finalized.status_code == 200, finalized.text

    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
    return read.json()


# --- the fixture itself ------------------------------------------------------


def test_the_fixture_carries_three_scenarios_of_three_trials() -> None:
    """AC-16's shape requirement, checked against the file rather than assumed."""
    # Arrange
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # Act
    trials = document["results"]["results"]
    by_scenario: dict[str, int] = {}
    for trial in trials:
        by_scenario[trial["test"]["name"]] = by_scenario.get(trial["test"]["name"], 0) + 1

    # Assert
    assert len(by_scenario) >= 3
    assert all(count >= 3 for count in by_scenario.values())
    assert len(trials) == 9


def test_the_fixture_carries_no_credential_or_personal_data() -> None:
    """§20.3 and the constitution: a checked-in artifact carries no secret.

    Asserted rather than trusted, because a fixture is the one file somebody
    regenerates from a real run and commits without rereading.
    """
    # Arrange
    text = FIXTURE.read_text(encoding="utf-8").lower()

    # Act / Assert
    for marker in ("apikey", "api_key", "authorization", "bearer ", "@", "sk-"):
        assert marker not in text, f"the fixture contains {marker!r}"


def test_the_fixture_declares_the_pinned_reporter() -> None:
    """ADR-0005: the importer reads one schema, and the fixture announces it."""
    # Arrange
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))

    # Act / Assert
    assert document["config"]["reporterSchema"] == "webmcp-evals/0.0.4"
    assert document["config"]["evaluatorVersion"] == "0.0.4"


# --- AC-16 end to end --------------------------------------------------------


async def test_the_fixture_produces_a_silent_outcome_defect(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-16's headline: "one call-level pass whose deterministic outcome fails".

    The evaluator reported that every required call was made. The independently
    observed cart says the discount was never applied. Neither layer is derived
    from the other, which is the only reason the disagreement means anything.
    """
    # Arrange / Act
    body = await _run_the_fixture(visitor)

    # Assert
    assert body["counts"]["call_level_pass_outcome_fail"] >= 1
    assert body["metrics"]["incremental_outcome_failure_trials"] >= 1

    # …and it is the discount scenario that produced it.
    faulty = next(g for g in body["by_scenario"] if g["label"] == FAULTY)
    assert faulty["counts"]["call_level_pass_outcome_fail"] == 3


async def test_the_corrected_scenario_passes_both_layers(
    visitor: httpx.AsyncClient,
) -> None:
    """The counterpart that makes the defect mean something.

    A benchmark where every trial failed the outcome layer would satisfy the
    test above and prove nothing about the target.
    """
    # Arrange / Act
    body = await _run_the_fixture(visitor)

    # Assert
    corrected = next(g for g in body["by_scenario"] if g["label"] == CORRECTED)
    assert corrected["counts"]["call_level_pass_outcome_pass"] == 3


async def test_the_counting_identities_hold_on_the_fixture(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-092's three identities, on the real nine-trial population."""
    # Arrange / Act
    counts = (await _run_the_fixture(visitor))["counts"]

    # Assert
    cells = (
        counts["call_level_pass_outcome_pass"]
        + counts["call_level_pass_outcome_fail"]
        + counts["call_level_fail_outcome_pass"]
        + counts["call_level_fail_outcome_fail"]
    )
    assert cells == counts["eligible_trials"]
    assert counts["eligible_trials"] + counts["excluded_trials"] == counts["total_trials"]
    assert counts["error_trials"] <= counts["excluded_trials"]
    assert counts["total_trials"] == 9


async def test_the_errored_trial_is_excluded_not_counted_as_a_failure(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-092: "evaluator or adapter `error` is excluded".

    The fixture carries one errored trial on purpose. Counting it as a
    call-level failure would make a crashed evaluator look like a bad model.
    """
    # Arrange / Act
    body = await _run_the_fixture(visitor)

    # Assert
    assert body["counts"]["error_trials"] >= 1
    errored = [t for t in body["trials"] if t["call_level_result"] == "error"]
    assert errored
    assert all(t["eligibility"] == "excluded" for t in errored)
    assert all(t["exclusion_reason"] == "evaluator_error" for t in errored)


async def test_the_benchmark_is_labelled_a_recorded_fixture(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-16: the application "never represent[s] either as a live execution".

    §25.3 is blunter still: a checked-in artifact "must never be presented as a
    live execution".
    """
    # Arrange / Act
    body = await _run_the_fixture(visitor)

    # Assert
    assert body["source_kind"] == "recorded_fixture"
    assert body["manifest"]["source_kind"] == "recorded_fixture"
    assert body["manifest"]["model_name"] != "live"


async def test_scenario_and_failure_profile_populations_stay_separate(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-16: "does not pool correlation modes, source kinds, scenario modes, or
    failure-profile populations"."""
    # Arrange / Act
    body = await _run_the_fixture(visitor)

    # Assert
    labels = {group["label"] for group in body["by_scenario"]}
    assert {FAULTY, CORRECTED, OMITTED} <= labels

    # Only the faulty-build scenario declared a failure profile, so it is the
    # only profile population — the others are absent rather than gathered
    # under an invented "none".
    profiles = {group["label"] for group in body["by_failure_profile"]}
    assert profiles == {FAULT}


async def test_the_finalized_report_is_downloadable_and_self_describing(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-094: the derived artifact carries the matrix, metrics, and manifest,
    and references the source it was computed from."""
    # Arrange
    body = await _run_the_fixture(visitor)
    benchmark_id = body["benchmark_id"]

    # Act
    downloaded = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/report")

    # Assert
    report = json.loads(downloaded.text)
    assert report["manifest"]["reporter_schema"] == "webmcp-evals/0.0.4"
    assert report["manifest"]["source_artifact_hashes"]
    assert report["counts"]["total_trials"] == 9
    assert report["content_hash"].startswith("sha256:")
