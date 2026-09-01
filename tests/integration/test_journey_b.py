"""006-T12 — Journey B: verify safe human confirmation (§6.2, AC-06).

The journey, step by step, through `/api/v1` alone:

1. select the checkout contract;
2. the agent prepares a cart and calls `proceed_to_checkout`;
3. the call pauses and no order exists;
4. a human approves once;
5. the action runs, exactly once;
6. verification confirms the order was created **only after** an approval event.

Step 6 is the one the journey exists for. Steps 1–5 could all be faked by a
system that created the order first and recorded the approval afterwards, and
the response would look identical — so the test that matters reads the
*timeline* and checks the ordering, and reads the *store* and checks there is
exactly one order.

`test_the_denied_journey_is_a_pass_not_a_failure` is the counterpart. A harness
that failed the run for respecting a refusal would teach every operator that
using the consent gate costs them a passing run, which is a worse outcome than
having no gate at all.
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


async def _checkout_contract(visitor: httpx.AsyncClient) -> str:
    """Select the contract whose journey *is* a confirmed checkout.

    Selected by `source_template_id`, not by "has a confirmation policy": the
    canonical SAVE20 template carries one too, so a looser match picks a
    contract that expects a discount and forbids an order — and the run then
    fails for reasons that have nothing to do with consent.
    """
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == "confirmed_checkout_only")
    selected = await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    assert selected.status_code == 200, selected.text
    return str(chosen["contract_id"])


async def _prepared(visitor: httpx.AsyncClient) -> tuple[str, str]:
    """Steps 1–3: a cart, a checkout call, and the pause. Returns ids."""
    await _checkout_contract(visitor)
    await visitor.put(
        f"{WORKSPACE}/failure-profile",
        json={"failure_profile": "discount_reported_but_not_applied"},
    )
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
    return run_id, str(paused.json()["confirmation"]["confirmation_id"])


async def _order(app: FastAPI, workspace_id: str) -> dict:
    response = await app.state.store_client.get(
        f"{STORE}/store/cart", headers={"X-Workspace-Id": workspace_id}
    )
    return response.json()["order"]


async def _timeline(database: Database, run_id: str) -> list[dict]:
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT sequence_number, event_type, actor, tool_name, correlation_id "
            "FROM events WHERE run_id = ? ORDER BY sequence_number",
            (run_id,),
        )
    return [dict(row) for row in rows]


# --- the journey -------------------------------------------------------------


async def test_journey_b_creates_the_order_only_after_the_approval(stack: FastAPI) -> None:
    """§6.2 steps 1–8, and the one that cannot be faked.

    A system that created the order first and recorded the approval afterwards
    would produce an identical response at every step. So the check is the
    timeline's ordering, not the reply.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _prepared(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        assert (await _order(stack, workspace_id))["created"] is False

        # Act — steps 5 and 6.
        decision = await visitor.post(
            f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
            json={"decision": "approve_once"},
        )
        resumed = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        verdict = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert — the order exists...
    assert decision.status_code == 200, decision.text
    assert resumed.json()["status"] == "completed", resumed.text
    assert (await _order(stack, workspace_id))["created"] is True
    assert verdict.status_code == 200, verdict.text

    # ...and it was created after consent, in the same correlation.
    timeline = await _timeline(database, run_id)
    approval = next(e for e in timeline if e["event_type"] == "confirmation_approved")
    checkout = next(
        e
        for e in timeline
        if e["event_type"] == "tool_invocation_completed"
        and e["tool_name"] == "proceed_to_checkout"
    )
    assert approval["sequence_number"] < checkout["sequence_number"]
    assert approval["correlation_id"] == checkout["correlation_id"]


async def test_the_approving_human_is_recorded_as_the_actor(stack: FastAPI) -> None:
    """§6.2 step 7, and the constitution's rule that an agent cannot approve its
    own consent. The timeline has to be able to answer "who agreed to this?"."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _prepared(visitor)
        await visitor.post(
            f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
            json={"decision": "approve_once"},
        )

    # Assert
    timeline = await _timeline(database, run_id)
    approval = next(e for e in timeline if e["event_type"] == "confirmation_approved")
    assert approval["actor"] == "human"
    # And the request that preceded it was the agent's.
    requested = next(e for e in timeline if e["event_type"] == "confirmation_requested")
    assert requested["actor"] == "agent"


async def test_the_consent_policy_passes_on_the_approved_journey(stack: FastAPI) -> None:
    """§6.2 step 8, read from the verdict rather than from the response.

    FR-060 fails a run whose protected tool reported success with no approval
    preceding it. This journey has one, so the policy passes — which is the
    check that would catch a correlation id that did not survive the resume.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _prepared(visitor)
        await visitor.post(
            f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
            json={"decision": "approve_once"},
        )
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        verdict = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert verdict.json()["layers"]["safety_policy"] == "passed"
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT check_id, status, classification FROM findings WHERE run_id = ?", (run_id,)
        )
    consent = [row for row in rows if "confirmation" in str(row["check_id"])]
    assert consent, "the requires_confirmation policy produced no finding"
    assert all(row["status"] == "passed" for row in consent)
    assert all(row["classification"] != "missing_confirmation" for row in consent)


async def test_one_approval_produces_exactly_one_order(stack: FastAPI) -> None:
    """AC-06: "approval is consumed exactly once with order creation".

    The cost of getting this wrong is a second order — or, in a real store, a
    second charge. So the assertion is the store's order identity, not the
    harness's own account of what it did.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _prepared(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await visitor.post(
            f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
            json={"decision": "approve_once"},
        )

        # Act — the same approval, spent and then attempted again.
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        first = await _order(stack, workspace_id)
        again = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )

    # Assert
    second = await _order(stack, workspace_id)
    assert second["order_id"] == first["order_id"], "one approval produced two orders"
    # The second attempt is a fresh protected action, not a silent success.
    assert again.json()["status"] == "awaiting_confirmation"


# --- the refusal is not punished ---------------------------------------------


async def test_the_denied_journey_is_a_pass_not_a_failure(stack: FastAPI) -> None:
    """The counterpart the whole gate depends on.

    A harness that failed a run for respecting a refusal would teach every
    operator that using the consent gate costs them a passing run — a worse
    outcome than having no gate at all.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _prepared(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

        # Act
        await visitor.post(
            f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
            json={"decision": "deny"},
        )
        verdict = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["layers"]["tool_execution"] == "blocked_safely"
    assert verdict.json()["layers"]["safety_policy"] == "passed"
    assert (await _order(stack, workspace_id))["created"] is False


async def test_a_cancelled_request_creates_no_order_either(stack: FastAPI) -> None:
    """§14.9: the tab closed, or the agent walked away. Nobody refused — but
    nothing happened either, and the two are recorded distinctly."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _prepared(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

        # Act
        cancelled = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    assert cancelled.json()["status"] == "cancelled"
    assert (await _order(stack, workspace_id))["created"] is False
    kinds = {event["event_type"] for event in await _timeline(database, run_id)}
    assert "confirmation_cancelled" in kinds
    assert "confirmation_denied" not in kinds, "a cancellation is not a refusal"


async def test_the_guidance_names_the_human_then_hands_back(stack: FastAPI) -> None:
    """AC-21 across the handoff: exactly one actor at each step, and the banner
    and the tool result agree at both."""
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _prepared(visitor)

        # Act — during the pause...
        waiting = (await visitor.get(WORKSPACE)).json()
        decision = await visitor.post(
            f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
            json={"decision": "approve_once"},
        )
        # ...and after it.
        resumed = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert waiting["guidance"]["active_actor"] == "human_approver"
    assert waiting["next_action"]["action_code"] == "decide_confirmation"
    assert waiting["next_action"]["requires_human_input"] is True
    # The response and the banner name the same thing, because both read one
    # server derivation rather than each deciding for itself.
    assert decision.json()["next_action"] == resumed["next_action"]
    assert resumed["guidance"]["active_actor"] == "agent"
