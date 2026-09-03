"""008-T9 — the §15.6 benchmark routes.

Eight endpoints, driven over real HTTP through the composed application, so what
is tested is the surface a client actually reaches rather than the services
underneath it.

Two behaviours here are not visible from the service layer and get their own
tests:

- **the import body is bytes.** FR-090's 1 MiB cap must precede parsing, and a
  FastAPI model parameter would have parsed the document before any handler
  ran. `test_an_oversized_report_is_refused` sends a payload that is both
  oversized *and* unparseable: if the route had parsed first, the error would
  be a different one.
- **`/report` returns the stored bytes.** A benchmark is identified by its
  content hash, so a downloaded report must rehash to the value the row
  records. A re-serialisation would be equal but not identical.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.security.canonical import content_hash
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI
from integrations.google_evals.pins import REPORTER_SCHEMA, REPORTER_VERSION

pytestmark = pytest.mark.integration

BENCHMARKS = f"{API_PREFIX}/benchmarks"
CONTRACTS = f"{API_PREFIX}/contracts"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"

JOURNEY = [
    {
        "name": "update_cart",
        "arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"},
    },
    {"name": "apply_discount", "arguments": {"code": "SAVE20"}},
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


def _trial(name: str, outcome: str, run_index: int = 0, **extra: object) -> dict:
    return {
        "test": {"name": name},
        "outcome": outcome,
        "runIndex": run_index,
        "response": "the assistant's reply",
        **extra,
    }


async def _select_contract(visitor: httpx.AsyncClient) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")


async def _suite(visitor: httpx.AsyncClient, mode: str = "imported_trajectory_replay") -> str:
    created = await visitor.post(BENCHMARKS, json={"correlation_mode": mode})
    assert created.status_code == 201, created.text
    return created.json()["benchmark_id"]


# --- create ------------------------------------------------------------------


async def test_a_suite_is_created_in_draft(visitor: httpx.AsyncClient) -> None:
    """§15.6: "create a benchmark suite from a validated manifest"."""
    # Arrange / Act
    created = await visitor.post(BENCHMARKS, json={"source_kind": "recorded_fixture"})

    # Assert
    assert created.status_code == 201, created.text
    assert created.json()["status"] == "draft"
    assert created.json()["source_kind"] == "recorded_fixture"


async def test_the_suites_can_be_listed_so_a_person_can_choose_one(
    visitor: httpx.AsyncClient,
) -> None:
    """The matrix had no door: every other benchmark route needs an id.

    Without a listing, a suite could only be reached by somebody who already
    held its identifier — workable for an API client and impossible from a
    screen, which is why the dual-layer view shipped unreachable.
    """
    # Arrange
    first = await _suite(visitor)
    second = await _suite(visitor)

    # Act
    listed = await visitor.get(BENCHMARKS)

    # Assert
    assert listed.status_code == 200, listed.text
    ids = [row["benchmark_id"] for row in listed.json()["benchmarks"]]
    assert set(ids) == {first, second}
    # Newest first, so the suite just created is the one offered by default.
    assert ids[0] == second
    assert listed.json()["benchmarks"][0]["status"] == "draft"


async def test_an_empty_workspace_lists_no_suites(visitor: httpx.AsyncClient) -> None:
    """Nothing yet is an ordinary state, not a 404 and not an error."""
    # Arrange / Act
    listed = await visitor.get(BENCHMARKS)

    # Assert
    assert listed.status_code == 200
    assert listed.json()["benchmarks"] == []


async def test_the_listing_shows_only_this_workspace_s_suites(stack: FastAPI) -> None:
    """004's isolation rule reaches the listing too.

    A listing is the one route that returns identifiers nobody supplied, so a
    leak here hands another workspace's ids to a caller who could then read
    them by name.
    """
    # Arrange
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as owner:
        await _suite(owner)

    # Act — a second client gets its own workspace cookie.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as intruder:
        listed = await intruder.get(BENCHMARKS)

    # Assert
    assert listed.status_code == 200
    assert listed.json()["benchmarks"] == []


async def test_an_unknown_source_kind_is_refused(visitor: httpx.AsyncClient) -> None:
    """The enum is closed; a typo must not become a new population."""
    # Arrange / Act
    created = await visitor.post(BENCHMARKS, json={"source_kind": "live_ish"})

    # Assert
    assert created.status_code == 422


# --- import ------------------------------------------------------------------


async def test_a_report_imports_and_preserves_its_source_artifact(
    visitor: httpx.AsyncClient,
) -> None:
    """§15.6: "import, validate, redact, preserve, and normalize"."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act
    imported = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(_trial("adds a mug", "pass", trajectory=JOURNEY)),
        headers={"content-type": "application/json"},
    )

    # Assert
    assert imported.status_code == 201, imported.text
    body = imported.json()
    assert body["trial_count"] == 1
    assert body["reporter_schema"] == REPORTER_SCHEMA
    assert body["content_hash"].startswith("sha256:")
    assert body["source_artifact_id"]


async def test_an_oversized_report_is_refused(visitor: httpx.AsyncClient) -> None:
    """FR-090's cap, and evidence that it precedes parsing.

    The payload is unparseable as well as large. A route that parsed before
    measuring would report malformed JSON instead, so the error we get tells us
    which happened first.
    """
    # Arrange
    benchmark_id = await _suite(visitor)
    payload = b"{" + b"x" * 1_100_000

    # Act
    refused = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=payload,
        headers={"content-type": "application/json"},
    )

    # Assert
    assert refused.status_code == 422
    assert "limit" in refused.text


async def test_an_unpinned_reporter_schema_is_refused(visitor: httpx.AsyncClient) -> None:
    """ADR-0005's pin is a refusal, not a preference."""
    # Arrange
    benchmark_id = await _suite(visitor)
    document = json.loads(_report(_trial("adds a mug", "pass")))
    document["config"]["reporterSchema"] = "webmcp-evals/9.9.9"

    # Act
    refused = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=json.dumps(document).encode("utf-8"),
        headers={"content-type": "application/json"},
    )

    # Assert
    assert refused.status_code == 422


async def test_the_import_names_the_trials_that_need_a_human_choice(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-091: a trial with no stable address binds only by explicit choice, and
    the caller is told which ones those are rather than discovering it on
    refusal."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act — two trials share an address, so neither is addressable.
    imported = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(
            _trial("adds a mug", "pass"),
            _trial("adds a mug", "fail"),
        ),
        headers={"content-type": "application/json"},
    )

    # Assert
    assert imported.json()["unaddressable_trial_ids"] == ["#0", "#1"]


# --- bindings ----------------------------------------------------------------


async def test_bindings_are_saved_and_the_suite_can_be_sealed(
    visitor: httpx.AsyncClient,
) -> None:
    """§15.6: "validate and save explicit one-to-one trial bindings before the
    suite becomes ready"."""
    # Arrange
    benchmark_id = await _suite(visitor)
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(_trial("adds a mug", "pass", trajectory=JOURNEY)),
        headers={"content-type": "application/json"},
    )

    # Act
    saved = await visitor.put(
        f"{BENCHMARKS}/{benchmark_id}/bindings", json={"bindings": [], "seal": True}
    )

    # Assert
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "ready"


async def test_an_ambiguous_binding_is_refused_by_name(visitor: httpx.AsyncClient) -> None:
    """§26.5's ambiguous rejection, reaching the client as its own code."""
    # Arrange
    benchmark_id = await _suite(visitor)
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(_trial("adds a mug", "pass"), _trial("adds a mug", "fail")),
        headers={"content-type": "application/json"},
    )

    # Act — `#0` is positional, and the request does not acknowledge that.
    refused = await visitor.put(
        f"{BENCHMARKS}/{benchmark_id}/bindings",
        json={"bindings": [{"external_trial_id": "#0", "evaluation_run_id": "evr-1"}]},
    )

    # Assert
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "TRIAL_BINDING_AMBIGUOUS"


async def test_bindings_are_refused_once_the_suite_is_ready(
    visitor: httpx.AsyncClient,
) -> None:
    """§16.4: bindings become immutable at `ready`."""
    # Arrange
    benchmark_id = await _suite(visitor)
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(_trial("adds a mug", "pass", trajectory=JOURNEY)),
        headers={"content-type": "application/json"},
    )
    await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})

    # Act
    refused = await visitor.put(
        f"{BENCHMARKS}/{benchmark_id}/bindings",
        json={"bindings": [{"external_trial_id": "adds a mug#0", "evaluation_run_id": "evr-1"}]},
    )

    # Assert
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "BENCHMARK_BINDINGS_SEALED"


# --- replay, finalize, read --------------------------------------------------


async def test_the_whole_pipeline_runs_end_to_end(visitor: httpx.AsyncClient) -> None:
    """§24.7's six steps through the API: import → bind → replay → finalize.

    The suite finishes `completed` with a matrix over trials that actually
    executed, which is what every later surface reads.
    """
    # Arrange
    await _select_contract(visitor)
    benchmark_id = await _suite(visitor)
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(
            _trial("adds a mug", "pass", 0, trajectory=JOURNEY),
            _trial("adds a mug", "pass", 1, trajectory=JOURNEY),
        ),
        headers={"content-type": "application/json"},
    )
    await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})

    # Act
    replayed = await visitor.post(f"{BENCHMARKS}/{benchmark_id}/replay")
    finalized = await visitor.post(f"{BENCHMARKS}/{benchmark_id}/finalize")

    # Assert
    assert replayed.status_code == 200, replayed.text
    assert len(replayed.json()["replayed"]) == 2
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["status"] == "completed"
    assert finalized.json()["result_artifact_id"]


async def test_reading_a_benchmark_reports_status_matrix_and_metrics(
    visitor: httpx.AsyncClient,
) -> None:
    """§15.6: "status, metadata, matrix, metrics, and trial summaries"."""
    # Arrange
    await _select_contract(visitor)
    benchmark_id = await _suite(visitor)
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(_trial("adds a mug", "pass", trajectory=JOURNEY)),
        headers={"content-type": "application/json"},
    )

    # Act
    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")

    # Assert
    body = read.json()
    assert body["status"] == "draft"
    assert body["source_kind"] == "recorded_fixture"
    assert body["correlation_mode"] == "imported_trajectory_replay"
    assert "counts" in body
    assert "metrics" in body
    assert body["trials"][0]["external_trial_id"] == "adds a mug#0"


async def test_one_trial_reports_bounded_redacted_evidence(
    visitor: httpx.AsyncClient,
) -> None:
    """§15.6: "bounded redacted call-level and outcome evidence for one trial".

    The evaluator's prose stays in the immutable source artifact; what this
    endpoint returns is the verdicts, the trajectory, and the keys that were not
    understood.
    """
    # Arrange
    benchmark_id = await _suite(visitor)
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(
            _trial("adds a mug", "pass", trajectory=JOURNEY, someUpstreamField={"x": 1})
        ),
        headers={"content-type": "application/json"},
    )

    # Act
    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/trials/adds a mug%230")

    # Assert
    assert read.status_code == 200, read.text
    body = read.json()
    assert body["call_level_result"] == "passed"
    assert body["unsupported_metadata"] == {"someUpstreamField": None}
    assert body["trajectory"][0]["name"] == "update_cart"
    assert "the assistant's reply" not in read.text


async def test_the_report_download_is_the_stored_bytes(visitor: httpx.AsyncClient) -> None:
    """§15.6's download, and FR-089's interchange promise.

    The bytes must rehash to the recorded content hash — which only holds if
    what the client receives is what was written.
    """
    # Arrange
    await _select_contract(visitor)
    benchmark_id = await _suite(visitor)
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report(_trial("adds a mug", "pass", trajectory=JOURNEY)),
        headers={"content-type": "application/json"},
    )
    await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/replay")
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/finalize")

    # Act
    downloaded = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/report")

    # Assert
    assert downloaded.status_code == 200, downloaded.text
    document = json.loads(downloaded.text)
    recomputed = content_hash({k: v for k, v in document.items() if k != "content_hash"})
    assert document["content_hash"] == recomputed


async def test_an_unfinalized_benchmark_has_no_report(visitor: httpx.AsyncClient) -> None:
    """A report that does not exist is refused rather than invented."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act
    refused = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/report")

    # Assert
    assert refused.status_code == 409


# --- isolation ---------------------------------------------------------------


async def test_another_workspace_cannot_read_the_benchmark(stack: FastAPI) -> None:
    """§12.4 and 004's rule: a known id from another workspace grants nothing,
    and is indistinguishable from one that never existed."""
    # Arrange
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as owner:
        benchmark_id = await _suite(owner)

    # Act — a second client gets its own workspace cookie.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as intruder:
        read = await intruder.get(f"{BENCHMARKS}/{benchmark_id}")
        absent = await intruder.get(f"{BENCHMARKS}/bench_does_not_exist")

    # Assert
    assert read.status_code == absent.status_code == 404
