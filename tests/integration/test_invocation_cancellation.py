"""012-T4 — invocation cancellation is safe (AC-14, FR-037).

AC-14: "Given a checkout tool waiting for confirmation, when its WebMCP
execution signal is aborted, then the confirmation is cancelled,
`tool_invocation_cancelled` is recorded, and no order is created."

**Most of this shipped in 006.** `proceed_to_checkout` is registered through the
native path precisely because the pinned hook's `execute` takes only its
arguments and cannot tell a handler that its caller walked away; the browser
half — abort, cancel the confirmation, surface an error — is covered by
`tools.test.ts`. What these tests add is the *server* boundary AC-14 describes,
and the one clause of FR-037 that nothing exercised:

> "If a commit already completed before cancellation won the race, the immutable
> completion event and canonical state remain authoritative; the system shall
> never relabel that call as cancelled."

That is the dangerous direction. A cancellation arriving late is not merely
ineffective — if it were honoured, the run would carry a `cancelled` terminal
for a call that really did create an order, and the report would say no
consequential action occurred while the target held one. The evidence would be
worse than absent; it would be wrong in the safe-looking direction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CHECKOUT_TEMPLATE = "confirmed_checkout_only"
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
            # Kept so a test can read the target's own state directly: "no order
            # was created" is only worth asserting against the store rather
            # than against the report that describes it.
            harness.state.store_client = target_client
            yield harness


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


async def _pending_checkout(visitor: httpx.AsyncClient) -> tuple[str, str]:
    """Arm the checkout contract and reach a confirmation awaiting a decision."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CHECKOUT_TEMPLATE)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})

    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_cancel_mug"}},
    )
    pending = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": {"request_id": "req_cancel_checkout"}},
    )
    body = pending.json()
    assert body["status"] == "awaiting_confirmation", body
    return run_id, str(body["confirmation"]["confirmation_id"])


async def _events(visitor: httpx.AsyncClient, run_id: str) -> list[dict]:
    page = await visitor.get(f"{RUNS}/{run_id}/events?limit=50")
    return page.json()["events"]


async def _order(stack: FastAPI, visitor: httpx.AsyncClient) -> dict:
    workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
    body = await stack.state.store_client.get(
        "/demo/api/v1/store/cart", headers={"X-Workspace-Id": workspace_id}
    )
    return body.json()["order"]


# --- AC-14 -------------------------------------------------------------------


async def test_cancelling_records_a_cancelled_invocation(visitor: httpx.AsyncClient) -> None:
    """AC-14: "`tool_invocation_cancelled` is recorded".

    The event terminates the *agent's* invocation, so the timeline shows the
    call ending rather than simply stopping — a run whose last word on a
    checkout was its start would read as still in flight forever.
    """
    # Arrange
    run_id, confirmation_id = await _pending_checkout(visitor)

    # Act
    cancelled = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    assert cancelled.status_code == 200, cancelled.text
    kinds = [event["event_type"] for event in await _events(visitor, run_id)]
    assert "confirmation_cancelled" in kinds
    assert "tool_invocation_cancelled" in kinds


async def test_cancelling_creates_no_order(stack: FastAPI, visitor: httpx.AsyncClient) -> None:
    """AC-14's consequence clause, read from the target rather than the report.

    The whole point of the confirmation gate is that the consequential action
    does not happen. Asserting it against independently observed state is the
    only version of this test worth having.
    """
    # Arrange
    run_id, confirmation_id = await _pending_checkout(visitor)

    # Act
    await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    assert (await _order(stack, visitor))["created"] is False


async def test_a_cancellation_is_distinguishable_from_a_denial(
    visitor: httpx.AsyncClient,
) -> None:
    """§14.9: "a person said no" and "the tab closed" are different facts.

    Both create no order, and a timeline that recorded them identically would
    lose the difference between a human decision and an abandoned request.
    """
    # Arrange
    run_id, confirmation_id = await _pending_checkout(visitor)

    # Act
    await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    kinds = [event["event_type"] for event in await _events(visitor, run_id)]
    assert "confirmation_cancelled" in kinds
    assert "confirmation_denied" not in kinds


# --- FR-037's race clause ----------------------------------------------------


async def test_a_late_cancellation_never_relabels_a_completed_commit(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-037: "the system shall never relabel that call as cancelled".

    The dangerous direction. If a late cancellation were honoured, the run would
    carry a `cancelled` terminal for a call that really did create an order —
    the report would say no consequential action occurred while the target held
    one, which is wrong in the safe-looking direction.
    """
    # Arrange — approve, and let the checkout commit.
    run_id, confirmation_id = await _pending_checkout(visitor)
    approved = await visitor.post(
        f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
        json={"decision": "approve_once"},
    )
    assert approved.status_code == 200, approved.text
    completed = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": {"request_id": "req_cancel_checkout"}},
    )
    assert completed.json()["reported"]["status"] == "success", completed.text

    # Act — cancellation arrives after the commit.
    late = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert — refused, and the completed call keeps its terminal.
    assert late.status_code >= 400
    kinds = [event["event_type"] for event in await _events(visitor, run_id)]
    assert "tool_invocation_completed" in kinds
    assert "tool_invocation_cancelled" not in kinds


async def test_the_order_survives_a_late_cancellation(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """Canonical state "remains authoritative".

    The order exists. A cancellation cannot un-create it, and the harness must
    not pretend otherwise — the whole product rests on state being what the
    report says it is.
    """
    # Arrange
    run_id, confirmation_id = await _pending_checkout(visitor)
    await visitor.post(
        f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
        json={"decision": "approve_once"},
    )
    await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": {"request_id": "req_cancel_checkout"}},
    )

    # Act
    await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    assert (await _order(stack, visitor))["created"] is True


async def test_a_second_cancellation_is_refused(visitor: httpx.AsyncClient) -> None:
    """A decided request cannot be decided again, whichever way it went.

    Otherwise a repeated abort — a page reloading, a client retrying — would
    append a second terminal event for one invocation.
    """
    # Arrange
    run_id, confirmation_id = await _pending_checkout(visitor)
    first = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")
    assert first.status_code == 200

    # Act
    second = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    assert second.status_code >= 400
    terminals = [
        event["event_type"]
        for event in await _events(visitor, run_id)
        if event["event_type"] == "tool_invocation_cancelled"
    ]
    assert len(terminals) == 1


async def test_a_cancelled_run_still_verifies(visitor: httpx.AsyncClient) -> None:
    """A cancellation is a safe terminal outcome, not a broken run (FR-036).

    The journey ended without the consequential action, which is the outcome
    the contract wanted. A run that could not be verified after a cancellation
    would make abandoning a dialog look like a harness fault.
    """
    # Arrange
    run_id, confirmation_id = await _pending_checkout(visitor)
    await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Act
    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["overall_result"] in {"passed", "passed_with_warnings", "failed"}
