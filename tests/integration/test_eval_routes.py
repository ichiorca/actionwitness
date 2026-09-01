"""007-T11 — §15.4's eval routes.

The endpoint worth the most care is `case.json`. A case is identified by its
content hash, and the person most likely to check that hash is someone who was
handed the file rather than the person who generated it — so the download has to
be the **stored bytes**, not an equal-but-different re-serialisation.

Generation answers 200 whether or not it created the case. FR-080 makes an
identical repeat return the existing one, and answering a repeat with a conflict
would teach clients to treat idempotence as a failure.
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

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
EVALS = f"{API_PREFIX}/evals"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
FAULT = "discount_reported_but_not_applied"


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


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _failed_run(visitor: httpx.AsyncClient) -> str:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    for tool, arguments in (
        ("search_catalog", {"query": "mug"}),
        ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
        ("apply_discount", {"code": "SAVE20"}),
    ):
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
    await visitor.post(f"{RUNS}/{run_id}/verify")
    return run_id


async def test_a_failed_run_becomes_a_case_through_the_api(stack: FastAPI) -> None:
    """AC-08, at the boundary an agent and the panel both use."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/evals")

    # Assert
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["created"] is True
    assert body["content_hash"].startswith("sha256:")
    assert body["expected"]["reproduce_source"]["required_classifications"] == [
        "false_success_or_state_mismatch"
    ]


async def test_an_identical_repeat_is_a_200_that_says_it_created_nothing(
    stack: FastAPI,
) -> None:
    """FR-080. A 409 would teach clients to treat idempotence as a failure."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)
        first = await visitor.post(f"{RUNS}/{run_id}/evals")

        # Act
        second = await visitor.post(f"{RUNS}/{run_id}/evals")

    # Assert
    assert second.status_code == 200
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["eval_case_id"] == first.json()["eval_case_id"]


async def test_the_download_returns_the_stored_bytes(stack: FastAPI) -> None:
    """The hash has to be checkable by whoever was handed the file.

    A re-serialisation would be equal but not identical, and the recomputed hash
    would then disagree with the one the case carries — leaving a reader unable
    to tell tampering from formatting.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)
        case_id = (await visitor.post(f"{RUNS}/{run_id}/evals")).json()["eval_case_id"]

        # Act
        response = await visitor.get(f"{EVALS}/{case_id}/case.json")

    # Assert
    assert response.status_code == 200
    document = json.loads(response.text)
    declared = document.pop("content_hash")
    assert declared == content_hash(document), "the downloaded bytes do not match their own hash"


async def test_a_case_can_be_listed_and_read(stack: FastAPI) -> None:
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)
        case_id = (await visitor.post(f"{RUNS}/{run_id}/evals")).json()["eval_case_id"]

        # Act
        listed = await visitor.get(EVALS)
        read = await visitor.get(f"{EVALS}/{case_id}")

    # Assert
    assert [c["eval_case_id"] for c in listed.json()["cases"]] == [case_id]
    assert read.json()["source_run_id"] == run_id
    # No run yet, and the field says so rather than implying a verdict.
    assert read.json()["latest_run"] is None


async def test_running_a_case_reports_status_and_outcome_separately(
    stack: FastAPI,
) -> None:
    """§24.3's distinction, at the API boundary.

    A response carrying only one of the two fields would be read as the other,
    and a faithfully reproduced failure would look like a broken build.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)
        case_id = (await visitor.post(f"{RUNS}/{run_id}/evals")).json()["eval_case_id"]

        # Act
        response = await visitor.post(
            f"{EVALS}/{case_id}/runs", json={"environment": "reproduce_source"}
        )

    # Assert
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "passed"
    assert body["overall_result"] == "failed"
    assert body["classification_match"] is True


async def test_the_default_environment_is_current(stack: FastAPI) -> None:
    """§24.4: "`current` is always the default." A body that omitted it and got
    `reproduce_source` would report a reproduced failure as routine CI green."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)
        case_id = (await visitor.post(f"{RUNS}/{run_id}/evals")).json()["eval_case_id"]

        # Act
        response = await visitor.post(f"{EVALS}/{case_id}/runs", json={})

    # Assert
    assert response.json()["environment"] == "current"
    assert response.json()["status"] == "passed"
    assert response.json()["overall_result"] == "passed"


async def test_an_eval_run_can_be_read_back_with_its_report(stack: FastAPI) -> None:
    """FR-088's report, retrievable after the fact — a CI job that only saw an
    exit code has nothing to show a reader who asks why."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)
        case_id = (await visitor.post(f"{RUNS}/{run_id}/evals")).json()["eval_case_id"]
        eval_run_id = (await visitor.post(f"{EVALS}/{case_id}/runs", json={})).json()["eval_run_id"]

        # Act
        response = await visitor.get(f"{EVALS}/{case_id}/runs/{eval_run_id}")

    # Assert
    assert response.status_code == 200
    assert response.json()["report"]["environment"] == "current"
    assert response.json()["report"]["content_hash"].startswith("sha256:")


async def test_another_workspace_sees_none_of_it(stack: FastAPI) -> None:
    """AC-11, on every eval surface. Two clients, and the stranger holds real
    identifiers — an unguessable id is not a boundary."""
    # Arrange
    async with client(stack) as owner:
        run_id = await _failed_run(owner)
        case_id = (await owner.post(f"{RUNS}/{run_id}/evals")).json()["eval_case_id"]

    # Act
    async with client(stack) as stranger:
        await stranger.get(WORKSPACE)
        listed = await stranger.get(EVALS)
        read = await stranger.get(f"{EVALS}/{case_id}")
        download = await stranger.get(f"{EVALS}/{case_id}/case.json")
        generated = await stranger.post(f"{RUNS}/{run_id}/evals")

    # Assert
    assert listed.json()["cases"] == []
    assert read.status_code == 404
    assert download.status_code == 404
    assert generated.status_code == 404
