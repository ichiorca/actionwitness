"""006-T2 — deciding a confirmation (§14.4–§14.9, FR-066).

Three outcomes, and what separates them is what exists at the target afterwards.
So every test here asks the store rather than the response: "no mutation
occurred" is a claim the harness makes about itself, and this product exists
because such claims need checking.

The sharpest test is `test_an_approval_cannot_be_spent_twice`. "Approve once" is
the promise the whole flow is named for, and an implementation that marked an
approval used without enforcing it would pass every other test in this file.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    """A run stopped at a pending confirmation. Returns (run_id, confirmation_id)."""
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
    """§14.14: the invoking page calls back once the human has decided."""
    return await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": {"request_id": "req_checkoutone"}},
    )


async def _order(app: FastAPI, workspace_id: str) -> dict:
    response = await app.state.store_client.get(
        f"{STORE}/store/cart", headers={"X-Workspace-Id": workspace_id}
    )
    return response.json()["order"]


# --- approval ----------------------------------------------------------------


async def test_an_approval_lets_the_action_run_once_and_creates_the_order(
    stack: FastAPI,
) -> None:
    """§14.7: the mutation happens after the decision is recorded, not before."""
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        assert (await _order(stack, workspace_id))["created"] is False

        # Act
        decision = await _decide(visitor, run_id, confirmation_id, "approve_once")
        resumed = await _resume(visitor, run_id)

    # Assert
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "approved"
    # The decision itself changes nothing at the target (§14.6).
    assert decision.json()["mutated"] is False
    assert resumed.json()["status"] == "completed"
    assert (await _order(stack, workspace_id))["created"] is True


async def test_an_approval_cannot_be_spent_twice(stack: FastAPI) -> None:
    """FR-066: "approve once" is the promise the flow is named for.

    An implementation that marked the approval used without enforcing it would
    pass every other test here, so this one drives the resume twice and checks
    the *store's* order identity — a second order is what a spent-twice
    approval actually costs.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _decide(visitor, run_id, confirmation_id, "approve_once")
        first = await _resume(visitor, run_id)
        order_after_first = await _order(stack, workspace_id)

        # Act — the same approval, used again.
        second = await _resume(visitor, run_id)

    # Assert
    assert first.json()["status"] == "completed"
    order_after_second = await _order(stack, workspace_id)
    assert order_after_second["order_id"] == order_after_first["order_id"], (
        "a second order was created from one approval"
    )
    # Nor is the second attempt silently successful-looking: with the approval
    # consumed it is a fresh protected action, so it pauses again.
    assert second.json()["status"] == "awaiting_confirmation"


async def test_the_approval_is_consumed_not_merely_marked(stack: FastAPI) -> None:
    """The stored record says spent, and says when."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        await _decide(visitor, run_id, confirmation_id, "approve_once")
        await _resume(visitor, run_id)

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT status, decided_at, consumed_at FROM confirmation_requests WHERE id = ?",
            (confirmation_id,),
        )
    assert row is not None
    assert row["status"] == "consumed"
    assert row["decided_at"] is not None
    assert row["consumed_at"] is not None


async def test_the_approval_precedes_the_mutation_in_the_timeline(stack: FastAPI) -> None:
    """§14.6 and FR-060: the policy matches an approval to the mutation it
    authorized by correlation id, and requires it to come first.

    The correlation is the part worth pinning — a resumed invocation that took a
    fresh id would leave the approval looking like consent for nothing in
    particular, and the policy would report a missing confirmation.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        await _decide(visitor, run_id, confirmation_id, "approve_once")
        await _resume(visitor, run_id)

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT sequence_number, event_type, correlation_id FROM events "
            "WHERE run_id = ? AND tool_name = 'proceed_to_checkout' ORDER BY sequence_number",
            (run_id,),
        )
    kinds = {row["event_type"]: row for row in rows}
    assert "confirmation_approved" in kinds
    assert "tool_invocation_completed" in kinds
    assert (
        kinds["confirmation_approved"]["sequence_number"]
        < kinds["tool_invocation_completed"]["sequence_number"]
    )
    # One correlation across request, approval, and mutation.
    assert len({row["correlation_id"] for row in rows}) == 1


# --- refusals ----------------------------------------------------------------


@pytest.mark.parametrize(("call", "expected"), [("deny", "denied"), ("cancel", "cancelled")])
async def test_a_refusal_creates_no_order(stack: FastAPI, call: str, expected: str) -> None:
    """§14.8, §14.9: denial and cancellation both leave the target untouched.

    Recorded distinctly, because "a person said no" and "the tab closed" are
    different facts about a run and a reader has to be able to tell them apart.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

        # Act
        if call == "deny":
            response = await _decide(visitor, run_id, confirmation_id, "deny")
        else:
            response = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["status"] == expected
    assert response.json()["mutated"] is False
    assert (await _order(stack, workspace_id))["created"] is False


async def test_a_refusal_releases_the_run_rather_than_stranding_it(stack: FastAPI) -> None:
    """A refused action must not leave the run stuck awaiting a decision that
    already happened — the human would have no way to finish or reset."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)

        # Act
        await _decide(visitor, run_id, confirmation_id, "deny")
        verification = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
    assert row is not None and row["status"] != "awaiting_confirmation"
    # And the run can now be verified, which is what "released" has to mean.
    assert verification.status_code == 200, verification.text


async def test_a_denied_action_is_not_recorded_as_a_tool_failure(stack: FastAPI) -> None:
    """FR-033: a safe block is an expected outcome, not a broken tool.

    If a denial were recorded as a failure, the harness would punish the run for
    doing exactly the right thing.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        await _decide(visitor, run_id, confirmation_id, "deny")

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT event_type FROM events WHERE run_id = ?", (run_id,))
    kinds = {row["event_type"] for row in rows}
    assert "confirmation_denied" in kinds
    assert "tool_invocation_failed" not in kinds


# --- who may decide ----------------------------------------------------------


async def test_a_second_client_cannot_decide_someone_elses_confirmation(
    stack: FastAPI,
) -> None:
    """§14.5: the workspace cookie authorizes the decision, never the identifier
    in the path.

    Two clients, because a single-client test proves the route works rather than
    that a stranger is locked out — and the stranger here holds the *real*
    identifiers, which is the case a scoping bug actually produces.
    """
    # Arrange
    async with client(stack) as owner:
        run_id, confirmation_id = await _paused(owner)
        workspace_id = (await owner.get(WORKSPACE)).json()["workspace_id"]

        # Act
        async with client(stack) as stranger:
            await stranger.get(WORKSPACE)
            approved = await _decide(stranger, run_id, confirmation_id, "approve_once")
            cancelled = await stranger.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    assert approved.status_code == 404
    assert cancelled.status_code == 404
    assert (await _order(stack, workspace_id))["created"] is False


async def test_a_decision_on_an_already_decided_request_is_refused(stack: FastAPI) -> None:
    """Two humans cannot both decide one request, and a denial cannot be
    upgraded to an approval by asking again."""
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)
        await _decide(visitor, run_id, confirmation_id, "deny")

        # Act
        again = await _decide(visitor, run_id, confirmation_id, "approve_once")

    # Assert
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "PRECONDITION_FAILED"


async def test_an_unknown_confirmation_is_not_found(stack: FastAPI) -> None:
    """An invented identifier reveals nothing."""
    # Arrange
    async with client(stack) as visitor:
        run_id, _ = await _paused(visitor)

        # Act
        response = await _decide(visitor, run_id, "cnf_invented", "approve_once")

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_an_unrecognised_decision_is_refused(stack: FastAPI) -> None:
    """The body is a closed vocabulary; `maybe` is not a decision."""
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _paused(visitor)

        # Act
        response = await visitor.post(
            f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
            json={"decision": "maybe"},
        )

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"
