"""006-T4 — the reads a client and an agent load state from (§15.3, §11.4, AC-22).

`GET /runs/{run_id}` is what a page rebuilds itself from after a refresh, which
is why `test_a_refreshing_client_can_rebuild_a_pending_dialog` matters: §14.14
keeps the confirmation in the page that requested it, so a reload that lost the
dialog would strand a run on a decision nobody can reach.

`GET /runs/{run_id}/findings` is §11.4's bounded projection. Its budget is
generous by design — a finding an agent cannot read is equivalent to one that was
never produced — and the rule that keeps it honest is that it always reports the
untruncated total. `test_an_elided_list_says_how_much_it_left_out` is that rule;
without it a bounded list would let an agent conclude it had seen everything.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
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
            harness.state.store_client = target_client
            yield harness


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _select(visitor: httpx.AsyncClient, template: str = CANONICAL) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == template)
    assert (await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")).status_code == 200


async def _checkout_contract(visitor: httpx.AsyncClient) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    for template in templates:
        document = (await visitor.get(f"{CONTRACTS}/{template['contract_id']}")).json()
        policies = document.get("document", document).get("policies") or []
        if any(p.get("tool") == "proceed_to_checkout" for p in policies):
            await visitor.post(f"{CONTRACTS}/{template['contract_id']}/select")
            return
    raise AssertionError("no template protects proceed_to_checkout")


async def _scenario(visitor: httpx.AsyncClient, mode: str) -> None:
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})


async def _failed_run(visitor: httpx.AsyncClient) -> str:
    """A run that fails `false_success_or_state_mismatch`, so it has findings."""
    await _select(visitor)
    await _scenario(visitor, "pre_fix")
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    for tool, arguments in (
        ("search_catalog", {"query": "mug"}),
        ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
        ("apply_discount", {"code": "SAVE20"}),
    ):
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
    assert (await visitor.post(f"{RUNS}/{run_id}/verify")).status_code == 200
    return run_id


# --- the run read ------------------------------------------------------------


async def test_the_run_read_summarises_without_duplicating_the_report(
    stack: FastAPI,
) -> None:
    """§15.3: "status and summary".

    Deliberately not the report: two differently shaped views of one verdict
    would give a reader two places to read it from and no way to tell which was
    authoritative when they disagreed.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)

        # Act
        body = (await visitor.get(f"{RUNS}/{run_id}")).json()

    # Assert
    assert body["run_id"] == run_id
    assert body["overall_result"] == "failed"
    assert body["scenario_mode"] == "pre_fix"
    assert body["completed_at"] is not None
    assert body["next_action"]["action_code"]
    assert "layers" not in body, "the report has its own endpoint and its own hash"
    assert "findings" not in body


async def test_a_refreshing_client_can_rebuild_a_pending_dialog(stack: FastAPI) -> None:
    """§14.14 keeps the confirmation in the page that requested it, so a reload
    has to be able to reconstruct it.

    Without this the dialog is lost on refresh and the run waits on a decision
    nobody can reach any more.
    """
    # Arrange
    async with client(stack) as visitor:
        await _checkout_contract(visitor)
        await _scenario(visitor, "post_fix")
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
        )
        paused = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )

        # Act — a fresh read, as a reloaded page would make.
        body = (await visitor.get(f"{RUNS}/{run_id}")).json()

    # Assert
    pending = body["pending_confirmation"]
    assert pending is not None
    assert pending["confirmation_id"] == paused.json()["confirmation"]["confirmation_id"]
    assert pending["tool_name"] == "proceed_to_checkout"
    # Enough to render the modal again: what it affects, and when it lapses.
    assert pending["consequence"]["action"] == "proceed_to_checkout"
    assert pending["expires_at"]


async def test_a_run_with_nothing_pending_says_so(stack: FastAPI) -> None:
    """The counterpart: a client must be able to tell "no dialog" from "a dialog
    I failed to load"."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)

        # Act
        body = (await visitor.get(f"{RUNS}/{run_id}")).json()

    # Assert
    assert body["pending_confirmation"] is None


async def test_a_lapsed_request_is_expired_by_the_read(stack: FastAPI) -> None:
    """§14.14: "otherwise the server expires it".

    Without this the run waits forever on a decision that can never arrive, and
    the only escape is a reset — which also discards the evidence.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _checkout_contract(visitor)
        await _scenario(visitor, "post_fix")
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
        )
        paused = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        confirmation_id = paused.json()["confirmation"]["confirmation_id"]

        async with database.transaction() as work:
            await work.execute(
                "UPDATE confirmation_requests SET expires_at = ? WHERE id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), confirmation_id),
            )

        # Act — the poll a waiting page would make.
        body = (await visitor.get(f"{RUNS}/{run_id}")).json()
        verification = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert body["pending_confirmation"] is None
    assert body["status"] != "awaiting_confirmation"
    # And the run is genuinely free again, which is the point of expiring it.
    assert verification.status_code == 200, verification.text


async def test_another_workspaces_run_is_not_readable(stack: FastAPI) -> None:
    """AC-11, with two clients and the real identifier."""
    # Arrange
    async with client(stack) as owner:
        run_id = await _failed_run(owner)

    # Act
    async with client(stack) as stranger:
        await stranger.get(WORKSPACE)
        response = await stranger.get(f"{RUNS}/{run_id}")

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# --- bounded findings --------------------------------------------------------


async def test_findings_are_structured_not_narrated(stack: FastAPI) -> None:
    """AC-22: "check ID, classification, path, and redacted expected and actual
    values", and "no finding is returned as narrated prose".

    A sentence would be shorter and would make the finding unusable by the agent
    it was written for.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)

        # Act
        body = (await visitor.get(f"{RUNS}/{run_id}/findings")).json()

    # Assert
    assert body["findings"], "a failed run reported no findings"
    first = body["findings"][0]
    assert set(first) >= {
        "check_id",
        "check_type",
        "status",
        "severity",
        "classification",
        "path",
        "expected",
        "actual",
    }
    # The failing checks come first, so a bounded list spends its budget on the
    # findings a reader acts on.
    assert first["status"] == "failed"


async def test_an_elided_list_says_how_much_it_left_out(stack: FastAPI) -> None:
    """§11.4: "it always reports the untruncated total count and the report
    endpoint".

    This is the rule that keeps a bounded projection honest. Without it an agent
    reading three findings would conclude there were three.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)

        # Act — the default limit of 3, against a run with more than that.
        body = (await visitor.get(f"{RUNS}/{run_id}/findings")).json()
        everything = (await visitor.get(f"{RUNS}/{run_id}/findings?limit=10")).json()

    # Assert
    assert body["returned"] <= 3
    assert body["total"] == everything["total"]
    assert body["total"] >= body["returned"]
    assert body["elided"] == body["total"] - body["returned"]
    assert body["report"] == f"/api/v1/runs/{run_id}/report"


async def test_the_default_limit_is_three(stack: FastAPI) -> None:
    """§11.4 fixes it: "its default `limit` is 3 rather than 10"."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)

        # Act
        body = (await visitor.get(f"{RUNS}/{run_id}/findings")).json()

    # Assert
    assert body["returned"] == min(3, body["total"])


async def test_long_values_are_truncated_with_a_visible_marker(stack: FastAPI) -> None:
    """§11.4 truncates each `expected` and `actual` to 120 characters.

    With a marker, so a reader can see a value was shortened rather than
    treating the fragment as the whole thing — a silently clipped `actual` is a
    finding that says something untrue.
    """
    # Arrange — a finding whose stored value is far longer than the budget.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)
        async with database.transaction() as work:
            await work.execute(
                "UPDATE findings SET actual_json = ? WHERE run_id = ?",
                (json.dumps("x" * 500), run_id),
            )

        # Act
        body = (await visitor.get(f"{RUNS}/{run_id}/findings")).json()

    # Assert
    actual = body["findings"][0]["actual"]
    assert isinstance(actual, str)
    assert len(actual) < 500
    assert actual != "x" * 500
    assert not actual.endswith("x"), "truncation left no visible marker"


async def test_a_limit_beyond_the_budget_is_refused(stack: FastAPI) -> None:
    """The cap exists so the 4,000-character budget stays meetable."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _failed_run(visitor)

        # Act
        response = await visitor.get(f"{RUNS}/{run_id}/findings?limit=50")

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_findings_from_another_workspace_are_not_readable(stack: FastAPI) -> None:
    """AC-11 again, on the endpoint an agent uses to read a verdict."""
    # Arrange
    async with client(stack) as owner:
        run_id = await _failed_run(owner)

    # Act
    async with client(stack) as stranger:
        await stranger.get(WORKSPACE)
        response = await stranger.get(f"{RUNS}/{run_id}/findings")

    # Assert
    assert response.status_code == 404
