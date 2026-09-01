"""006-T1 — a protected action pauses for a human (§14, FR-060–FR-062).

The property everything else rests on: **nothing is dispatched.** A confirmation
flow that asked for consent after acting would be a receipt, not a gate, and the
difference is invisible from the response alone — both say "waiting". So the
tests here check the target, not the reply: no order exists, and the cart the
approval is bound to is the one that was observed.

The second property is that the harness learns *which* actions are protected
from the contract, not from a list of tool names it keeps. A harness that
decided that itself would be deciding what is consequential on the operator's
behalf, and `test_a_tool_without_the_policy_is_not_gated` is what stops the
implementation drifting back to a hardcoded check.
"""

from __future__ import annotations

import json
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


async def _checkout_contract(visitor: httpx.AsyncClient) -> dict:
    """Select a built-in template whose policies protect `proceed_to_checkout`."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    for template in templates:
        document = (await visitor.get(f"{CONTRACTS}/{template['contract_id']}")).json()
        policies = document.get("document", document).get("policies") or []
        if any(
            policy.get("type") == "requires_confirmation"
            and policy.get("tool") == "proceed_to_checkout"
            for policy in policies
        ):
            assert (
                await visitor.post(f"{CONTRACTS}/{template['contract_id']}/select")
            ).status_code == 200
            return document
    raise AssertionError("no built-in template protects proceed_to_checkout")


async def _scenario(visitor: httpx.AsyncClient, mode: str = "post_fix") -> None:
    assert (
        await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    ).status_code == 200
    assert (
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})
    ).status_code == 200


async def _armed_with_cart(visitor: httpx.AsyncClient) -> str:
    """A run with a mug in the cart, ready to check out."""
    await _checkout_contract(visitor)
    await _scenario(visitor)
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    added = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
    )
    assert added.status_code == 200, added.text
    return run_id


async def _checkout(visitor: httpx.AsyncClient, run_id: str) -> httpx.Response:
    return await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": {"request_id": "req_checkoutone"}},
    )


async def _order(app: FastAPI, workspace_id: str) -> dict:
    """The target's own account of whether an order exists."""
    response = await app.state.store_client.get(
        f"{STORE}/store/cart", headers={"X-Workspace-Id": workspace_id}
    )
    assert response.status_code == 200
    return response.json()["order"]


# --- the gate ----------------------------------------------------------------


async def test_a_protected_action_creates_no_order_before_consent(stack: FastAPI) -> None:
    """The property the whole flow exists for, checked at the target.

    The response saying "awaiting" proves nothing on its own — a flow that
    ordered first and asked afterwards would say exactly the same. So this
    looks at the store.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed_with_cart(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

        # Act
        response = await _checkout(visitor, run_id)

    # Assert
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_confirmation"
    assert (await _order(stack, workspace_id))["created"] is False


async def test_the_pause_is_not_reported_as_a_failure(stack: FastAPI) -> None:
    """§14.3 keeps the promise pending. An error would teach an agent to retry,
    which is the behaviour a consent gate exists to prevent."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed_with_cart(visitor)

        # Act
        body = (await _checkout(visitor, run_id)).json()

    # Assert
    assert body["reported"]["status"] is None
    assert "terminal_event" not in body, "an unfinished invocation has no terminal event"
    assert body["observed"]["state_changed"] is False


async def test_the_run_waits_and_the_human_is_named_as_the_actor(stack: FastAPI) -> None:
    """FR-120: exactly one active actor, and it is no longer the agent."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _armed_with_cart(visitor)

        # Act
        body = (await _checkout(visitor, run_id)).json()
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
    assert row is not None and row["status"] == "awaiting_confirmation"
    assert workspace["guidance"]["active_actor"] == "human_approver"
    assert body["next_action"]["action_code"] == "decide_confirmation"


async def test_the_approval_is_bound_to_what_was_observed(stack: FastAPI) -> None:
    """§14: the binding is what stops an approval being replayed against a
    different cart. It is the hash of the *independently observed* state, never
    of anything the tool reported."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _armed_with_cart(visitor)
        body = (await _checkout(visitor, run_id)).json()

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT * FROM confirmation_requests WHERE id = ?",
            (body["confirmation"]["confirmation_id"],),
        )
        event = await work.fetch_one(
            "SELECT state_hash_before FROM events "
            "WHERE run_id = ? AND event_type = 'confirmation_requested'",
            (run_id,),
        )
    assert row is not None and event is not None
    assert row["state_binding_hash"].startswith("sha256:")
    assert row["status"] == "pending"
    assert row["run_id"] == run_id
    # The same observation the timeline recorded, not a second look.
    assert row["state_binding_hash"] == event["state_hash_before"]


async def test_the_consequence_shows_what_the_action_affects(stack: FastAPI) -> None:
    """§14.1 wants a human to see the material state — for this target, the cart
    and its total. The harness derives that from the adapter's declared effect
    paths (§13.4) rather than from knowing what a cart is.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed_with_cart(visitor)

        # Act
        consequence = (await _checkout(visitor, run_id)).json()["confirmation"]["consequence"]

    # Assert
    assert consequence["action"] == "proceed_to_checkout"
    assert consequence["state_version"] is not None
    # The declared effects of this tool reach the cart, so the human sees it.
    affects = json.dumps(consequence["affects"])
    assert "cart" in affects or "order" in affects, consequence["affects"]


async def test_the_expiry_comes_from_the_contracts_own_policy(stack: FastAPI) -> None:
    """FR-062: the timeout is contract-configured, not a constant the service
    picked. A harness-chosen expiry would silently override an operator who
    wrote a different one."""
    # Arrange
    async with client(stack) as visitor:
        document = await _checkout_contract(visitor)
        await _scenario(visitor)
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
        )

        # Act
        body = (await _checkout(visitor, run_id)).json()

    # Assert
    policies = document.get("document", document)["policies"]
    configured = next(
        policy["timeout_seconds"]
        for policy in policies
        if policy.get("type") == "requires_confirmation"
    )
    assert body["confirmation"]["consequence"] is not None
    async with stack.state.database.reading() as work:
        row = await work.fetch_one(
            "SELECT expires_at, created_at FROM confirmation_requests WHERE id = ?",
            (body["confirmation"]["confirmation_id"],),
        )
    assert row is not None
    from datetime import datetime

    span = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
    assert abs(span.total_seconds() - configured) < 2


async def test_a_tool_without_the_policy_is_not_gated(stack: FastAPI) -> None:
    """The counterpart. `update_cart` mutates too, and it is *not* protected —
    because this contract's policies do not name it.

    Without this, a harness that gated every mutation would pass every test
    above while making the contract's `requires_confirmation` policy
    decorative.
    """
    # Arrange
    async with client(stack) as visitor:
        await _checkout_contract(visitor)
        await _scenario(visitor)
        run_id = str((await visitor.post(RUNS)).json()["run_id"])

        # Act
        response = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_nogate"}},
        )

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["observed"]["state_changed"] is True


async def test_a_paused_run_blocks_verification(stack: FastAPI) -> None:
    """An unresolved confirmation is in-flight work, and 005's gate already
    refuses to verify over it. Asserted here because the *reason* it is now
    reachable is this task."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed_with_cart(visitor)
        await _checkout(visitor, run_id)

        # Act
        verification = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert verification.status_code == 409
    assert verification.json()["error"]["code"] == "RUN_IN_PROGRESS"
