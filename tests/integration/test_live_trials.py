"""010-T7 — live trials through the unchanged M7 path (AC-17, FR-101, §25.3).

AC-17: "the developer executes the pinned Google evaluator for at least three
scenarios with at least three completed live trials each and imports the
resulting report through the **AC-16 pipeline**".

**This milestone's real question is whether 008 was built fixture-shaped.** If a
live-sourced report needed a second import path, a second matrix, or a special
case in correlation, the Tier 2 claim was never as general as it looked. So the
central test here imports *the same bytes* twice — once on a deployment with a
live backend configured and once without — and asserts the trials, the counts,
and the metrics come out identical, differing only in the label.

**A client cannot claim a live run.** AC-17 makes labelling the application's
job and §25.3 forbids presenting a checked-in report as a live execution. Asking
for `live_model_run` where no backend is configured is refused rather than
downgraded: a caller who asked for live and silently got a fixture would go on
to present its numbers as a model result.

The live execution itself is an operator gate (T11). What is testable here is
the path a live report travels, which is the part that could be wrong.
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
CREDENTIAL = "EXAMPLE_MODEL_KEY"

FAULTY = "SAVE20 on one mug against the faulty build"
CORRECTED = "SAVE20 on one mug against the corrected build"
OMITTED = "SAVE20 on one mug, discount step omitted"

SCENARIOS = [
    {"scenario_id": FAULTY, "scenario_mode": "pre_fix", "failure_profile": FAULT},
    {"scenario_id": CORRECTED, "scenario_mode": "post_fix"},
    {"scenario_id": OMITTED, "scenario_mode": "post_fix"},
]

LIVE_ENVIRON = {
    "LIVE_EVALUATOR_ENABLED": "true",
    "LIVE_EVALUATOR_PROVIDER": "google",
    "LIVE_EVALUATOR_MODEL": "example-model-1",
    "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
    CREDENTIAL: "not-a-real-key",
}


def _build(tmp_path: Path, extra: dict[str, str]):
    # Each deployment gets its own directory: the point of this module is to run
    # two of them side by side, and a shared database would make the comparison
    # meaningless.
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = create_store(database_path=tmp_path / "store.sqlite3")
    return store, {
        "HARNESS_ENV": "local",
        "BUGGY_STORE_ENABLED": "true",
        "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        **extra,
    }


async def _stack(tmp_path: Path, extra: dict[str, str]) -> AsyncIterator[FastAPI]:
    store, environ = _build(tmp_path, extra)
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ=environ,
            database_path=tmp_path / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            yield harness


@pytest.fixture
async def fixture_only(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """A deployment with no live backend — the state CI runs in."""
    async for app in _stack(tmp_path / "offline", {}):
        yield app


@pytest.fixture
async def live_configured(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """A deployment with one configured backend, credential in the environment."""
    async for app in _stack(tmp_path / "live", LIVE_ENVIRON):
        yield app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _pipeline(visitor: httpx.AsyncClient, source_kind: str) -> dict:
    """The AC-16 pipeline, unchanged, under whichever source kind."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")

    created = await visitor.post(
        BENCHMARKS, json={"source_kind": source_kind, "scenarios": SCENARIOS}
    )
    assert created.status_code == 201, created.text
    benchmark_id = created.json()["benchmark_id"]

    imported = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=FIXTURE.read_bytes(),
        headers={"content-type": "application/json"},
    )
    assert imported.status_code == 201, imported.text
    await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/replay")
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/finalize")
    return (await visitor.get(f"{BENCHMARKS}/{benchmark_id}")).json()


# --- the milestone's real question -------------------------------------------


async def test_a_live_suite_uses_the_same_pipeline_as_a_fixture_one(
    fixture_only: FastAPI, live_configured: FastAPI
) -> None:
    """AC-17: imported "through the AC-16 pipeline".

    The same bytes, imported on a deployment with a live backend and on one
    without. Everything that describes the *trials* must be identical — a
    difference here would mean the Tier 2 path had a fixture-shaped special
    case in it, and the Tier 2 claim was narrower than it looked.
    """
    # Arrange / Act
    async with client(fixture_only) as offline:
        recorded = await _pipeline(offline, "recorded_fixture")
    async with client(live_configured) as online:
        live = await _pipeline(online, "live_model_run")

    # Assert — the numbers and the trials are the same.
    assert live["counts"] == recorded["counts"]
    assert live["metrics"] == recorded["metrics"]
    assert live["trials"] == recorded["trials"]
    assert live["by_scenario"] == recorded["by_scenario"]

    # …and the label is the one thing that differs.
    assert recorded["source_kind"] == "recorded_fixture"
    assert live["source_kind"] == "live_model_run"


async def test_the_live_suite_still_finds_the_silent_outcome_defect(
    live_configured: FastAPI,
) -> None:
    """The product's claim has to survive the change of source.

    A live suite that produced no dual-layer evidence would mean the matrix
    only ever worked for the checked-in case.
    """
    # Arrange / Act
    async with client(live_configured) as visitor:
        body = await _pipeline(visitor, "live_model_run")

    # Assert
    assert body["counts"]["call_level_pass_outcome_fail"] == 3
    assert body["metrics"]["incremental_outcome_failure_trials"] == 3
    assert body["status"] == "completed"


async def test_a_live_suite_meets_ac_17_scale(live_configured: FastAPI) -> None:
    """AC-17: "at least three scenarios with at least three completed live
    trials each"."""
    # Arrange / Act
    async with client(live_configured) as visitor:
        body = await _pipeline(visitor, "live_model_run")

    # Assert
    assert len(body["by_scenario"]) >= 3
    assert all(group["counts"]["total_trials"] >= 3 for group in body["by_scenario"])


# --- the label is the application's to give ---------------------------------


async def test_a_client_cannot_claim_a_live_run_without_a_backend(
    fixture_only: FastAPI,
) -> None:
    """§25.3: a checked-in report is "never presented as a live execution".

    Refused rather than downgraded: a caller who asked for live and silently
    received a fixture-labelled suite would present its numbers as a model
    result.
    """
    # Arrange / Act
    async with client(fixture_only) as visitor:
        refused = await visitor.post(BENCHMARKS, json={"source_kind": "live_model_run"})

    # Assert
    assert refused.status_code == 409
    assert "no configured live model backend" in refused.text


async def test_a_live_deployment_may_still_import_a_recorded_fixture(
    live_configured: FastAPI,
) -> None:
    """FR-101's fallback has to keep working on the machine that has a
    credential — that is the machine where a live run fails on quota."""
    # Arrange / Act
    async with client(live_configured) as visitor:
        body = await _pipeline(visitor, "recorded_fixture")

    # Assert
    assert body["source_kind"] == "recorded_fixture"
    assert body["counts"]["total_trials"] == 9


async def test_the_recorded_label_survives_finalization(
    live_configured: FastAPI,
) -> None:
    """The moment that matters is the demo where the credential failed.

    The artifact a viewer downloads has to say `recorded_fixture` too, not just
    the screen they were shown.
    """
    # Arrange
    async with client(live_configured) as visitor:
        body = await _pipeline(visitor, "recorded_fixture")

        # Act
        report = await visitor.get(f"{BENCHMARKS}/{body['benchmark_id']}/report")

    # Assert
    document = json.loads(report.text)
    assert document["manifest"]["source_kind"] == "recorded_fixture"


# --- the manifest of a live suite --------------------------------------------


async def test_a_live_suite_records_the_evaluator_metadata_it_imported(
    live_configured: FastAPI,
) -> None:
    """FR-093: actual exported parameters, recorded without invention.

    They come from the report, not from the deployment's configuration — the
    manifest describes the run that happened.
    """
    # Arrange / Act
    async with client(live_configured) as visitor:
        body = await _pipeline(visitor, "live_model_run")

    # Assert
    manifest = body["manifest"]
    assert manifest["reporter_schema"] == "webmcp-evals/0.0.4"
    assert manifest["normalized_adapter_version"] == "1"
    assert manifest["source_kind"] == "live_model_run"


async def test_a_live_suite_carries_no_credential(live_configured: FastAPI) -> None:
    """AC-17: the credential is retained "only in the evaluator process
    environment", and this deployment has one set."""
    # Arrange / Act
    async with client(live_configured) as visitor:
        body = await _pipeline(visitor, "live_model_run")
        report = await visitor.get(f"{BENCHMARKS}/{body['benchmark_id']}/report")

    # Assert
    assert "not-a-real-key" not in json.dumps(body)
    assert "not-a-real-key" not in report.text


async def test_two_identical_benchmarks_hash_identically(
    fixture_only: FastAPI, live_configured: FastAPI
) -> None:
    """FR-089 and FR-094: an artifact is identified by its content hash.

    Two runs of the same report must produce the same trial order, or the
    finalized report hashes differently each time and a benchmark stops being
    able to state its own identity. The order was `created_at`-dependent until
    010-T7 found this: nine rows inside one timestamp tick sorted by id, nine
    that straddled a tick sorted by insertion, so the hash depended on how fast
    the machine happened to be.
    """
    # Arrange / Act — the same bytes through two separate deployments.
    async with client(fixture_only) as first:
        one = await _pipeline(first, "recorded_fixture")
    async with client(live_configured) as second:
        two = await _pipeline(second, "recorded_fixture")

    # Assert — same order, same counts, and therefore a reproducible artifact.
    assert [trial["external_trial_id"] for trial in one["trials"]] == [
        trial["external_trial_id"] for trial in two["trials"]
    ]
    assert one["counts"] == two["counts"]
