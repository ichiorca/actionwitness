"""005-T7 — the layered outcome report (§23.1, FR-070, FR-073).

The milestone's exit gate names one report shape exactly:

> The report shows observed trajectory pass, execution pass, business outcome
> fail, and model selection `not_evaluated` for the source run.

That sentence is the point of having five layers instead of one verdict. Under
the discount fault every tool call *works* — the trajectory is what the contract
expected, the execution layer is clean — and the business outcome still fails.
A single pass/fail would collapse "the agent did the wrong thing" and "the
target lied about doing the right thing" into the same answer, and those need
different responses from a reader.

`model_tool_selection` stays `not_evaluated` because nothing in Tier 1 judges
tool selection, and §23.1 forbids a Tier 2 import from updating it in the source
report. It is not a parameter of `compose_outcome_report` at all, which is how
the core makes that unbreakable rather than merely documented.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.security.canonical import content_hash
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"


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


async def _scenario(app: FastAPI, workspace_id: str, mode: str, profile: str) -> None:
    adapter = app.state.adapters.adapter("buggy_store")
    response = await adapter._client.post(
        "/demo/api/v1/store/scenario",
        headers={"X-Workspace-Id": workspace_id},
        json={"scenario_mode": mode, "fault_profile": profile},
    )
    assert response.status_code < 400, response.text


async def _run_the_journey(visitor: httpx.AsyncClient) -> dict:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    run_id = (await visitor.post(RUNS)).json()["run_id"]

    for path, arguments in (
        ("search_catalog", {"query": "mug"}),
        ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
        ("apply_discount", {"code": "SAVE20"}),
    ):
        response = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{path}:invoke", json={"arguments": arguments}
        )
        assert response.status_code == 200, response.text

    verified = await visitor.post(f"{RUNS}/{run_id}/verify")
    assert verified.status_code == 200, verified.text
    return {"run_id": run_id, **verified.json()}


# --- the exit gate's report shape -------------------------------------------


async def test_the_pre_fix_report_separates_execution_from_outcome(
    stack: FastAPI,
) -> None:
    """The exit gate's sentence, layer by layer.

    Every call worked and the trajectory was the one the contract expected; the
    business outcome still fails, because the target did not do what it said.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")

        # Act
        result = await _run_the_journey(visitor)

    # Assert
    layers = result["layers"]
    assert layers["observed_trajectory"] == "passed"
    assert layers["tool_execution"] == "passed"
    assert layers["business_outcome"] == "failed"
    assert layers["model_tool_selection"] == "not_evaluated"
    assert result["overall_result"] == "failed"


async def test_the_post_fix_report_passes_every_layer(stack: FastAPI) -> None:
    """The counterpart: the same five layers against an honest target."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "post_fix", "discount_reported_but_not_applied")

        # Act
        result = await _run_the_journey(visitor)

    # Assert
    layers = result["layers"]
    assert layers["business_outcome"] in {"passed", "passed_with_warnings"}
    assert layers["observed_trajectory"] == "passed"
    assert layers["tool_execution"] == "passed"
    # Still `not_evaluated`: nothing in Tier 1 judges tool selection, and a
    # passing run must not imply that it did.
    assert layers["model_tool_selection"] == "not_evaluated"


async def test_model_tool_selection_is_never_anything_else(stack: FastAPI) -> None:
    """§23.1 finalizes it as `not_evaluated` in a source report, and the core
    offers no parameter for it — so this holds by construction, not by care."""
    # Arrange / Act
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        result = await _run_the_journey(visitor)

    # Assert
    stored = await _stored_report(stack, result["run_id"])
    assert stored["layers"]["model_tool_selection"] == "not_evaluated"


async def test_the_safety_policy_layer_is_reported_separately(stack: FastAPI) -> None:
    """A failing policy must not drag the business outcome down with it, and a
    failing assertion must not implicate the policies. Five layers exist to keep
    those answers apart."""
    # Arrange / Act
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        result = await _run_the_journey(visitor)

    # Assert — the contract's assertions failed; its policies did not.
    assert result["layers"]["business_outcome"] == "failed"
    assert result["layers"]["safety_policy"] in {"passed", "passed_with_warnings"}


# --- counts -----------------------------------------------------------------


async def test_the_counts_describe_the_run(stack: FastAPI) -> None:
    """§23.1's counts, each with a stated denominator."""
    # Arrange / Act
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        result = await _run_the_journey(visitor)

    # Assert
    counts = result["counts"]
    # Three agent invocations were made; §23.1 counts actor-`agent` starts only.
    assert counts["tool_calls"] == 3
    assert counts["critical_failures"] >= 1
    assert counts["human_confirmations"] == 0
    # Arming and each transition record one guidance handoff.
    assert counts["guidance_handoffs"] >= 1


# --- the report as an immutable artifact ------------------------------------


async def _stored_report(app: FastAPI, run_id: str) -> dict:
    database: Database = app.state.database
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT relative_path FROM artifacts WHERE run_id = ? AND artifact_type = ?",
            (run_id, "outcome_report"),
        )
    assert row is not None, "no outcome report artifact was recorded"
    return json.loads(app.state.artifacts.read_text(row["relative_path"]))


async def test_the_report_is_stored_as_an_artifact_with_its_size_accounted(
    stack: FastAPI,
) -> None:
    """FR-008 caps artifact count and bytes, so an artifact that skipped its row
    would be invisible to the ceiling that is supposed to bound it."""
    # Arrange / Act
    async with client(stack) as visitor:
        result = await _run_the_journey(visitor)

    # Assert
    database: Database = stack.state.database
    async with database.reading() as work:
        row = await work.fetch_one("SELECT * FROM artifacts WHERE run_id = ?", (result["run_id"],))
    assert row["artifact_type"] == "outcome_report"
    assert row["byte_size"] > 0
    assert row["content_hash"].startswith("sha256:")
    assert row["workspace_id"]


async def test_the_stored_bytes_hash_to_the_recorded_hash(stack: FastAPI) -> None:
    """§17.2: the stored document excludes its own `content_hash` member from the
    hash input, so a reader can recompute the hash from the file alone.

    Writing pretty-printed JSON beside a hash taken over canonical text would
    produce an artifact nobody could verify.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        result = await _run_the_journey(visitor)

    # Assert
    stored = await _stored_report(stack, result["run_id"])
    recorded = stored.pop("content_hash")
    assert recorded == content_hash(stored)
    assert recorded == result["report_content_hash"]


async def test_the_stored_report_carries_the_run_and_contract_identity(
    stack: FastAPI,
) -> None:
    """A report that could not name what it judged would be unauditable."""
    # Arrange / Act
    async with client(stack) as visitor:
        result = await _run_the_journey(visitor)

    # Assert
    stored = await _stored_report(stack, result["run_id"])
    assert stored["run_id"] == result["run_id"]
    assert stored["contract"]["content_hash"].startswith("sha256:")
    assert stored["target"]["id"] == "buggy-store"
    assert stored["target"]["adapter_id"] == "buggy_store"
    assert stored["mode"] == "verification"


async def test_the_report_records_the_guidance_at_finalization(
    stack: FastAPI,
) -> None:
    """§23.8: the report says who was asked to do what when the run ended."""
    # Arrange / Act
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        result = await _run_the_journey(visitor)

    # Assert
    stored = await _stored_report(stack, result["run_id"])
    guidance = stored["guidance_at_finalization"]
    assert guidance["actor"] == "operator"
    assert guidance["action"] == "review_findings"
    assert guidance["reason"]


async def test_the_report_names_the_primary_failure_classification(
    stack: FastAPI,
) -> None:
    """One failure is the headline; §22 orders them so the same run always
    reports the same one."""
    # Arrange / Act
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        result = await _run_the_journey(visitor)

    # Assert
    stored = await _stored_report(stack, result["run_id"])
    assert stored["primary_failure"] is not None


async def test_a_passing_run_names_no_primary_failure(stack: FastAPI) -> None:
    # Arrange / Act
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "post_fix", "discount_reported_but_not_applied")
        result = await _run_the_journey(visitor)

    # Assert
    stored = await _stored_report(stack, result["run_id"])
    assert stored["primary_failure"] is None


# --- determinism and isolation ----------------------------------------------


async def test_the_same_journey_produces_the_same_report_hash(
    stack: FastAPI,
) -> None:
    """§24's replay compares classifications against a source report hash, so a
    hash that drifted between identical runs would make a regression case
    worthless.

    The run id differs between the two, so the hashes are compared with it
    removed — everything else must be identical.
    """
    # Arrange / Act
    documents = []
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        for _ in range(2):
            await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
            result = await _run_the_journey(visitor)
            stored = await _stored_report(stack, result["run_id"])
            stored.pop("content_hash")
            stored.pop("run_id")
            documents.append(stored)
            await visitor.post(f"{WORKSPACE}/reset")

    # Assert
    assert documents[0] == documents[1]


async def test_one_workspaces_report_is_not_another_s(stack: FastAPI) -> None:
    """AC-11 reaches the artifact: a report hangs off a run, and a run hangs off
    a workspace."""
    # Arrange
    async with client(stack) as alice, client(stack) as bob:
        alice_result = await _run_the_journey(alice)
        await bob.get(WORKSPACE)
        bob_workspace = (await bob.get(WORKSPACE)).json()["workspace_id"]

    # Assert
    database: Database = stack.state.database
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT workspace_id FROM artifacts WHERE run_id = ?", (alice_result["run_id"],)
        )
    assert row["workspace_id"] != bob_workspace
