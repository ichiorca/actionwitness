"""006-T3 — refusals are safe outcomes, not failures (§14.8, §14.9, FR-033).

A denial, an expiry, a cancellation, and a stale approval all end the same way
at the target: nothing happened. What separates them is what a reader can learn
afterwards, and this file is about that difference surviving all the way into
the verdict.

The test that matters most is `test_a_denied_run_verifies_without_being_punished`.
It is easy to build a consent gate that blocks the mutation and then reports the
run as broken — and that would be worse than having no gate, because it teaches
every operator that refusing an action costs them a passing run. §23.1 is
explicit that `blocked_safely` "does not by itself fail the overall run".

`test_a_stale_approval_is_refused_and_cannot_be_reused` covers the case §14.7
exists for: the world moved between the human being shown a cart and the action
running. The approval described what they saw, and what they saw is gone.
"""

from __future__ import annotations

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
STORE = "/demo/api/v1"
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


async def _checkout_contract(visitor: httpx.AsyncClient) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    for template in templates:
        document = (await visitor.get(f"{CONTRACTS}/{template['contract_id']}")).json()
        policies = document.get("document", document).get("policies") or []
        if any(p.get("tool") == "proceed_to_checkout" for p in policies):
            await visitor.post(f"{CONTRACTS}/{template['contract_id']}/select")
            return
    raise AssertionError("no template protects proceed_to_checkout")


async def _paused(visitor: httpx.AsyncClient) -> tuple[str, str]:
    await _checkout_contract(visitor)
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    added = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
    )
    assert added.status_code == 200, added.text
    paused = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": {"request_id": "req_checkoutone"}},
    )
    assert paused.json()["status"] == "awaiting_confirmation", paused.text
    return run_id, paused.json()["confirmation"]["confirmation_id"]


async def _decide(
    visitor: httpx.AsyncClient, run_id: str, confirmation_id: str, decision: str
) -> httpx.Response:
    return await visitor.post(
        f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
        json={"decision": decision},
    )


async def _resume(visitor: httpx.AsyncClient, run_id: str) -> httpx.Response:
    return await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": {"request_id": "req_checkoutone"}},
    )


async def _order(app: FastAPI, workspace_id: str) -> dict:
    response = await app.state.store_client.get(
        f"{STORE}/store/cart", headers={"X-Workspace-Id": workspace_id}
    )
    return response.json()["order"]


async def _expire(database: Database, confirmation_id: str) -> None:
    """Move the request's expiry into the past.

    Reaching into the row rather than sleeping: the constitution forbids
    wall-clock dependence in a required suite, and a test that waited 60 seconds
    for a real expiry would be both slow and flaky. The behaviour under test is
    what the service does with a lapsed request, not the clock's arithmetic.
    """
    async with database.transaction() as work:
        await work.execute(
            "UPDATE confirmation_requests SET expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), confirmation_id),
        )


# --- the verdict is not punished --------------------------------------------


async def test_a_denied_run_verifies_without_being_punished(stack: FastAPI) -> None:
    """§23.1: `blocked_safely` "does not by itself fail the overall run".

    A gate that blocked the mutation and then reported the run as broken would
    be worse than no gate at all — it would teach every operator that refusing
    an action costs them a passing run.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        await _decide(visitor, run_id, confirmation_id, "deny")

        # Act
        verdict = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert verdict.status_code == 200, verdict.text
    body = verdict.json()
    assert body["layers"]["tool_execution"] == "blocked_safely"
    # The refusal is visible in the report rather than silently absorbed.
    assert body["layers"]["tool_execution"] != "passed"


async def test_the_consent_policy_passes_when_nothing_was_mutated(
    stack: FastAPI,
) -> None:
    """FR-060 fails a run whose protected tool reported success without an
    approval. A denial produced no success at all, so there is nothing
    unconsented — the policy passes, and the run is not accused of skipping
    consent it never needed.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        await _decide(visitor, run_id, confirmation_id, "deny")
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT check_id, status, classification FROM findings WHERE run_id = ?",
            (run_id,),
        )
    consent = [row for row in rows if "confirmation" in str(row["check_id"])]
    assert consent, "the requires_confirmation policy produced no finding at all"
    assert all(row["status"] == "passed" for row in consent), [dict(r) for r in consent]
    assert all(row["classification"] is None for row in consent)


# --- expiry ------------------------------------------------------------------


async def test_a_lapsed_request_expires_rather_than_being_approved(
    stack: FastAPI,
) -> None:
    """§14.8: a late approval must not land.

    Letting one through would collapse expiry into approval — the human's
    window would mean nothing, and an approval given against a cart from five
    minutes ago would authorize whatever the cart is now.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _expire(database, confirmation_id)

        # Act — an approval, arriving too late.
        response = await _decide(visitor, run_id, confirmation_id, "approve_once")

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "expired"
    assert response.json()["mutated"] is False
    assert (await _order(stack, workspace_id))["created"] is False


async def test_an_expiry_is_recorded_as_its_own_outcome(stack: FastAPI) -> None:
    """Distinct from a denial: nobody refused this, the clock ran out. A reader
    tracing a run has to be able to tell those apart."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        await _expire(database, confirmation_id)
        await _decide(visitor, run_id, confirmation_id, "approve_once")

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT event_type FROM events WHERE run_id = ?", (run_id,))
        row = await work.fetch_one(
            "SELECT status FROM confirmation_requests WHERE id = ?", (confirmation_id,)
        )
    kinds = {r["event_type"] for r in rows}
    assert "confirmation_expired" in kinds
    assert "confirmation_denied" not in kinds
    assert "confirmation_approved" not in kinds
    assert row is not None and row["status"] == "expired"


async def test_an_expired_request_cannot_be_resumed(stack: FastAPI) -> None:
    """The counterpart at the other door: an agent that calls the tool again
    after an expiry must not find a usable approval waiting."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _expire(database, confirmation_id)
        await _decide(visitor, run_id, confirmation_id, "approve_once")

        # Act
        resumed = await _resume(visitor, run_id)

    # Assert — a fresh request, not a completed checkout.
    assert resumed.json()["status"] == "awaiting_confirmation"
    assert (await _order(stack, workspace_id))["created"] is False


# --- stale approval ----------------------------------------------------------


async def test_a_stale_approval_is_refused_and_cannot_be_reused(
    stack: FastAPI,
) -> None:
    """§14.7: the world moved between the human being shown a cart and the
    action running.

    The approval described what they saw, and what they saw is gone. Carrying
    it forward is exactly the replay the state binding exists to prevent, so
    the approval is cancelled rather than left live — a live one would let the
    *next* attempt succeed against state nobody agreed to.
    """
    # Arrange — approve, then change the cart before resuming.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _decide(visitor, run_id, confirmation_id, "approve_once")

        moved = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 3, "request_id": "req_threemugs"}},
        )
        assert moved.json()["observed"]["state_changed"] is True

        # Act
        resumed = await _resume(visitor, run_id)

    # Assert — nothing happened, and the approval is spent for good.
    assert resumed.json()["reported"]["status"] is None
    assert (await _order(stack, workspace_id))["created"] is False
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT status FROM confirmation_requests WHERE id = ?", (confirmation_id,)
        )
        rows = await work.fetch_all("SELECT event_type FROM events WHERE run_id = ?", (run_id,))
    assert row is not None and row["status"] == "cancelled"
    assert "confirmation_cancelled" in {r["event_type"] for r in rows}


async def test_a_matching_state_still_lets_the_approval_through(
    stack: FastAPI,
) -> None:
    """The counterpart, so "refuse everything" cannot pass as correct.

    Without this, a revalidation that always failed would satisfy every stale
    test above while making approval impossible.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _decide(visitor, run_id, confirmation_id, "approve_once")

        # Act — nothing moved in between.
        resumed = await _resume(visitor, run_id)

    # Assert
    assert resumed.json()["status"] == "completed"
    assert (await _order(stack, workspace_id))["created"] is True


# --- recovery guidance -------------------------------------------------------


@pytest.mark.parametrize("decision", ["deny", "cancel"])
async def test_every_refusal_says_what_happened_and_what_to_do(
    stack: FastAPI, decision: str
) -> None:
    """FR-121: a blocking transition names the next action and its consequence.

    A refusal that left a human staring at a stopped run with no instruction is
    the failure mode guidance exists for.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)

        # Act
        if decision == "deny":
            response = await _decide(visitor, run_id, confirmation_id, "deny")
        else:
            response = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    body = response.json()
    assert "Nothing was changed" in body["detail"]
    assert body["next_action"]["action_code"], "a refusal must name a next action"
    # And the banner agrees with the response, rather than each deciding for
    # itself what the human should do next.
    assert workspace["guidance"]["action_code"] == body["next_action"]["action_code"]
