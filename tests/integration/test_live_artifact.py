"""010-T9/T10 — the live artifact, and the fallback when there is no live run.

FR-101: "persist the live evaluator report and its model/configuration metadata
as immutable benchmark sources. A redacted, checked-in report fixture ... shall
keep the matrix UI and deterministic verification reproducible when model
credentials, quota, or network access are unavailable; fallback results shall be
visibly labeled `recorded_fixture`, never `live`."

**"Precomputed before the demo is recorded" is testable as survival.** The
reason M9 asks for the artifact to be finalized in advance is that a demo
happens on a day when the credential may have expired, the quota may be spent,
or the network may be missing. So the test that matters is: finalize while a
live backend is configured, then bring up a deployment with **no live backend at
all** against the same storage, and read the artifact. It must still be there,
still readable, and still say `live_model_run` — because it is a record of a run
that happened, not a claim about the machine reading it.

**And the fallback must be honest at that same moment.** On the deployment where
the live run just became impossible, importing the checked-in fixture must
produce `recorded_fixture` and must not be presentable as live. That is the
moment §25.3's rule protects: a demo under pressure is exactly when somebody
would want the more impressive label.
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

LIVE = {
    "LIVE_EVALUATOR_ENABLED": "true",
    "LIVE_EVALUATOR_PROVIDER": "google",
    "LIVE_EVALUATOR_MODEL": "example-model-1",
    "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
    CREDENTIAL: "not-a-real-key",
}

#: A backend that is switched on but has lost its credential — quota exhausted,
#: key rotated, secret not mounted. The state a demo actually fails in.
LIVE_BROKEN = {
    "LIVE_EVALUATOR_ENABLED": "true",
    "LIVE_EVALUATOR_PROVIDER": "google",
    "LIVE_EVALUATOR_MODEL": "example-model-1",
    "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
}


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _deployment(root: Path, extra: dict[str, str]) -> AsyncIterator[FastAPI]:
    """One harness over `root`'s storage. Called twice with the same root to
    represent the same deployment restarted with different configuration."""
    root.mkdir(parents=True, exist_ok=True)
    store = create_store(database_path=root / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ={
                "HARNESS_ENV": "local",
                "BUGGY_STORE_ENABLED": "true",
                "HARNESS_ARTIFACT_ROOT": str(root / "artifacts"),
                **extra,
            },
            database_path=root / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            yield harness


async def _pipeline(visitor: httpx.AsyncClient, source_kind: str) -> dict:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    created = await visitor.post(
        BENCHMARKS, json={"source_kind": source_kind, "scenarios": SCENARIOS}
    )
    assert created.status_code == 201, created.text
    benchmark_id = created.json()["benchmark_id"]
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=FIXTURE.read_bytes(),
        headers={"content-type": "application/json"},
    )
    await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/replay")
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/finalize")
    return (await visitor.get(f"{BENCHMARKS}/{benchmark_id}")).json()


# --- T9: the artifact outlives the backend -----------------------------------


async def test_a_finalized_live_artifact_survives_the_backend_disappearing(
    tmp_path: Path,
) -> None:
    """FR-101 and M9's "finalize ... before recording the demo".

    Finalized while a live backend was configured; read back on a deployment
    that has none. The artifact is a record of a run that happened, not a claim
    about the machine reading it — so it must still be there and still say
    `live_model_run`.
    """
    # Arrange — finalize with the backend configured.
    root = tmp_path / "deployment"
    cookies: httpx.Cookies | None = None
    async for app in _deployment(root, LIVE):
        async with client(app) as visitor:
            body = await _pipeline(visitor, "live_model_run")
            benchmark_id = body["benchmark_id"]
            cookies = visitor.cookies
    assert body["source_kind"] == "live_model_run"

    # Act — the same storage, now with no live backend at all.
    async for app in _deployment(root, {}):
        async with client(app) as visitor:
            visitor.cookies = cookies  # the same workspace, restarted
            read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
            report = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/report")

    # Assert
    assert read.status_code == 200, read.text
    assert read.json()["source_kind"] == "live_model_run"
    assert report.status_code == 200
    assert json.loads(report.text)["manifest"]["source_kind"] == "live_model_run"


async def test_the_stored_report_still_verifies_without_the_backend(
    tmp_path: Path,
) -> None:
    """FR-089's interchange promise has to hold on the machine with no
    credential — that is where a reader checks a benchmark they were handed."""
    # Arrange
    from actionwitness_core.security.canonical import content_hash

    root = tmp_path / "deployment"
    cookies: httpx.Cookies | None = None
    async for app in _deployment(root, LIVE):
        async with client(app) as visitor:
            benchmark_id = (await _pipeline(visitor, "live_model_run"))["benchmark_id"]
            cookies = visitor.cookies

    # Act
    async for app in _deployment(root, {}):
        async with client(app) as visitor:
            visitor.cookies = cookies
            report = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/report")

    # Assert
    document = json.loads(report.text)
    recomputed = content_hash({k: v for k, v in document.items() if k != "content_hash"})
    assert document["content_hash"] == recomputed


async def test_the_imported_source_artifact_records_what_kind_of_run_it_was(
    tmp_path: Path,
) -> None:
    """FR-101: the live report is persisted "as [an] immutable benchmark
    source".

    The source kind travels on the artifact so it can be identified without
    consulting the suite — a suite goes when its workspace does, and an
    artifact that could not say what produced it would be unusable evidence.
    """
    # Arrange / Act
    async for app in _deployment(tmp_path / "d", LIVE):
        async with client(app) as visitor:
            body = await _pipeline(visitor, "live_model_run")
            async with app.state.database.transaction() as work:
                rows = await work.fetch_all(
                    "SELECT metadata_json FROM artifacts WHERE artifact_type = ?",
                    ("evaluator_report",),
                )

    # Assert
    assert rows
    metadata = json.loads(str(rows[0]["metadata_json"]))
    assert metadata["source_kind"] == "live_model_run"
    assert metadata["reporter_schema"] == "webmcp-evals/0.0.4"
    assert body["source_kind"] == "live_model_run"


# --- T10: the fallback, at the moment it is needed ---------------------------


async def test_a_broken_live_backend_still_runs_the_fixture_path(
    tmp_path: Path,
) -> None:
    """FR-101: the fixture keeps the matrix and deterministic verification
    reproducible "when model credentials, quota, or network access are
    unavailable".

    The backend here is switched on and has lost its credential — the state a
    demo actually fails in, rather than a tidy unconfigured one.
    """
    # Arrange / Act
    async for app in _deployment(tmp_path / "d", LIVE_BROKEN):
        async with client(app) as visitor:
            body = await _pipeline(visitor, "recorded_fixture")

    # Assert — the whole dual-layer result is still there.
    assert body["status"] == "completed"
    assert body["counts"]["total_trials"] == 9
    assert body["counts"]["call_level_pass_outcome_fail"] == 3


async def test_a_broken_live_backend_cannot_be_presented_as_live(
    tmp_path: Path,
) -> None:
    """§25.3: the fallback is "never presented as a live execution".

    This is the moment the rule protects. The credential has failed, the demo
    is about to start, and the more impressive label is one request away.
    """
    # Arrange / Act
    async for app in _deployment(tmp_path / "d", LIVE_BROKEN):
        async with client(app) as visitor:
            refused = await visitor.post(BENCHMARKS, json={"source_kind": "live_model_run"})

    # Assert
    assert refused.status_code == 409
    assert "no configured live model backend" in refused.text


async def test_the_fallback_says_recorded_fixture_everywhere_a_viewer_looks(
    tmp_path: Path,
) -> None:
    """FR-101: "fallback results shall be visibly labeled `recorded_fixture`,
    never `live`".

    The screen, the manifest, and the downloadable artifact — a label that held
    in only two of the three would still mislead somebody.
    """
    # Arrange / Act
    async for app in _deployment(tmp_path / "d", LIVE_BROKEN):
        async with client(app) as visitor:
            body = await _pipeline(visitor, "recorded_fixture")
            report = await visitor.get(f"{BENCHMARKS}/{body['benchmark_id']}/report")

    # Assert
    document = json.loads(report.text)
    assert body["source_kind"] == "recorded_fixture"
    assert body["manifest"]["source_kind"] == "recorded_fixture"
    assert document["manifest"]["source_kind"] == "recorded_fixture"
    assert "live_model_run" not in report.text


async def test_the_fixture_path_needs_no_credential_at_all(tmp_path: Path) -> None:
    """AC-17's second paragraph: "the same import/correlation path must still
    pass in CI from the checked-in redacted fixture"."""
    # Arrange / Act
    async for app in _deployment(tmp_path / "d", {}):
        async with client(app) as visitor:
            body = await _pipeline(visitor, "recorded_fixture")

    # Assert
    assert body["status"] == "completed"
    assert body["source_kind"] == "recorded_fixture"
    assert body["metrics"]["incremental_outcome_failure_trials"] == 3
