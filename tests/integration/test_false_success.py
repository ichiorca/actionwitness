"""005-T8 — the false-success classifier (FR-055, §12.2, §22).

FR-055 is a narrow rule and its narrowness is the point:

> For a failed final assertion, the classifier shall find the last terminal
> agent-tool action whose declared intended effect overlaps the assertion path,
> **regardless of whether state actually changed**. It shall use
> `false_success_or_state_mismatch` **only when** that tool reported success and
> its immediate authoritative post-call effect observation **also** mismatches
> the assertion. If the relevant action failed, was cancelled, lacks an
> immediate observation, or has no declared effect relationship, the engine
> shall use `assertion_mismatch` rather than infer causality.

So the tests below are mostly about *not* accusing. A classifier that reached
`false_success_or_state_mismatch` whenever an assertion failed after a
successful-looking call would pass the headline test and be wrong every other
time — and being wrong here means telling somebody their target lied when it
did not.

The classification is also what §22 orders failures by, so it decides which
failure a report names as primary. Getting it wrong changes the headline of the
whole run.
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


async def _select_and_arm(visitor: httpx.AsyncClient) -> str:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    armed = await visitor.post(RUNS)
    assert armed.status_code == 201, armed.text
    return str(armed.json()["run_id"])


async def _journey(visitor: httpx.AsyncClient, run_id: str) -> None:
    for tool, arguments in (
        ("search_catalog", {"query": "mug"}),
        ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
        ("apply_discount", {"code": "SAVE20"}),
    ):
        response = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
        assert response.status_code == 200, response.text


async def _failed_findings(app: FastAPI, run_id: str) -> list[dict]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT * FROM findings WHERE run_id = ? AND status = 'failed' ORDER BY check_id",
            (run_id,),
        )
    return [dict(row) for row in rows]


# --- the accusation, when it is warranted -----------------------------------


async def test_a_reported_success_contradicted_by_observation_is_false_success(
    stack: FastAPI,
) -> None:
    """Both halves of FR-055's "only when": the tool reported success, *and* the
    immediate post-call observation also mismatched."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _select_and_arm(visitor)
        await _journey(visitor, run_id)

        # Act
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    failed = await _failed_findings(stack, run_id)
    total = next(f for f in failed if f["path"] == "target.cart.total")
    assert total["classification"] == "false_success_or_state_mismatch"


async def test_the_accusation_names_the_action_it_blames(stack: FastAPI) -> None:
    """§17.1 stores the attribution, and a classification a reader cannot audit
    is not evidence."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _select_and_arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    failed = await _failed_findings(stack, run_id)
    total = next(f for f in failed if f["path"] == "target.cart.total")
    cause = json.loads(total["attributed_cause_json"])
    assert cause["kind"] == "tool_action"
    assert cause["tool_name"] == "apply_discount"
    assert cause["terminal_event"] == "tool_invocation_completed"
    assert "reported success" in cause["reason"]
    # The sequence number points at the event a reader can go and look at.
    assert isinstance(cause["event_sequence"], int)


async def test_the_classification_decides_the_reports_primary_failure(
    stack: FastAPI,
) -> None:
    """§22 orders failures by classification, so this decides the run's headline."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _select_and_arm(visitor)
        await _journey(visitor, run_id)

        # Act
        body = (await visitor.post(f"{RUNS}/{run_id}/verify")).json()

    # Assert
    assert body["primary_failure"] == "discounted-total"

    database: Database = stack.state.database
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT relative_path FROM artifacts WHERE run_id = ?", (run_id,)
        )
    report = json.loads(stack.state.artifacts.read_text(row["relative_path"]))
    assert report["primary_failure"] == "false_success_or_state_mismatch"


# --- and when it is not ------------------------------------------------------


async def test_an_honest_target_produces_no_accusation(stack: FastAPI) -> None:
    """Nothing failed, so nothing is classified. The counterpart that stops
    "always accuses" from passing."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "post_fix", "discount_reported_but_not_applied")
        run_id = await _select_and_arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert await _failed_findings(stack, run_id) == []


async def test_an_assertion_no_tool_claimed_is_not_blamed_on_a_tool(
    stack: FastAPI,
) -> None:
    """FR-055: "no declared effect relationship" means `assertion_mismatch`.

    The contract asserts no order was created. Nothing in this journey declares
    an effect on `target.order`, so if that assertion failed there would be no
    action to blame — and §12.2 forbids inferring one. Asserted through the
    attribution rather than only the classification, because "kind: none" is the
    engine explicitly declining to accuse.
    """
    # Arrange — arm, then create an order out of band so the assertion fails
    # without any recorded tool having declared an effect on it.
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        run_id = await _select_and_arm(visitor)
        await _journey(visitor, run_id)

        adapter = stack.state.adapters.adapter("buggy_store")
        confirmation = await adapter._client.post(
            "/demo/api/v1/store/checkout/confirmations",
            headers={"X-Workspace-Id": workspace_id},
            json={},
        )
        assert confirmation.status_code < 400, confirmation.text
        confirmation_id = confirmation.json()["confirmation_id"]
        decision = await adapter._client.post(
            f"/demo/api/v1/store/checkout/confirmations/{confirmation_id}/decision",
            headers={"X-Workspace-Id": workspace_id},
            json={"approved": True},
        )
        assert decision.status_code < 400, decision.text
        checkout = await adapter._client.post(
            "/demo/api/v1/store/checkout",
            headers={"X-Workspace-Id": workspace_id},
            json={"confirmation_id": confirmation_id, "request_id": "req_outofband"},
        )
        assert checkout.status_code < 400, checkout.text

        # Act
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    failed = await _failed_findings(stack, run_id)
    order = [f for f in failed if f["path"] == "target.order.created"]
    assert order, "the out-of-band order should have failed the no-order assertion"
    finding = order[0]
    assert finding["classification"] == "assertion_mismatch"
    cause = json.loads(finding["attributed_cause_json"])
    assert cause["kind"] == "none"
    assert "not inferred" in cause["reason"] or "no executed tool" in cause["reason"]


async def test_an_adapter_without_effect_metadata_loses_only_attribution(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§12.2: "missing effect metadata disables only causal false-success
    attribution."

    The run must still fail on the same assertion — the verdict is unchanged —
    but nothing is accused, because nothing declared what it would touch.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _select_and_arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreAdapter

        monkeypatch.setattr(BuggyStoreAdapter, "effect_map", lambda self: {})

        # Act
        body = (await visitor.post(f"{RUNS}/{run_id}/verify")).json()

    # Assert — the verdict is the same.
    assert body["overall_result"] == "failed"

    # But the classification falls back rather than accusing.
    failed = await _failed_findings(stack, run_id)
    total = next(f for f in failed if f["path"] == "target.cart.total")
    assert total["classification"] == "assertion_mismatch"
    cause = json.loads(total["attributed_cause_json"])
    assert cause["kind"] == "none"


async def test_a_failed_action_is_not_a_false_success(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-055: "if the relevant action failed ... use `assertion_mismatch`".

    A tool that never claimed success cannot have lied about succeeding, and the
    execution layer records the error separately.
    """
    # Arrange — the discount call fails outright.
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _select_and_arm(visitor)

        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}},
        )

        from integrations.buggy_store import BuggyStoreAdapter

        original = BuggyStoreAdapter.execute

        async def failing(self, workspace, tool_name, arguments, context):  # type: ignore[no-untyped-def]
            if tool_name == "apply_discount":
                raise RuntimeError("the discount service is down")
            return await original(self, workspace, tool_name, arguments, context)

        monkeypatch.setattr(BuggyStoreAdapter, "execute", failing)
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/apply_discount:invoke",
            json={"arguments": {"code": "SAVE20"}},
        )

        # Act
        body = (await visitor.post(f"{RUNS}/{run_id}/verify")).json()

    # Assert
    failed = await _failed_findings(stack, run_id)
    total = next(f for f in failed if f["path"] == "target.cart.total")
    assert total["classification"] == "assertion_mismatch"
    cause = json.loads(total["attributed_cause_json"])
    assert cause["terminal_event"] == "tool_invocation_failed"
    assert "did not complete" in cause["reason"]

    # §23.1's execution layer still records the error, separately.
    assert body["layers"]["tool_execution"] == "failed"


async def test_an_action_without_an_immediate_observation_is_not_accused(
    stack: FastAPI,
) -> None:
    """FR-055: "lacks an immediate observation" is a reason to fall back.

    Without the post-call reading there is no second source, and one source is
    exactly what this product refuses to convict on.

    The absence is created by removing `post_call_effect_state` from the stored
    event rather than by blinding the provider mid-journey. The classifier reads
    the timeline, so editing the timeline is what actually exercises the branch —
    and it does so without a fixture whose ordering has to be reasoned about.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _select_and_arm(visitor)
        await _journey(visitor, run_id)

        async with database.transaction() as work:
            row = await work.fetch_one(
                "SELECT id, redacted_payload_json FROM events WHERE run_id = ? "
                "AND tool_name = 'apply_discount' AND event_type = 'tool_invocation_completed'",
                (run_id,),
            )
            payload = json.loads(row["redacted_payload_json"])
            payload["post_call_effect_state"] = None
            await work.execute(
                "UPDATE events SET redacted_payload_json = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), row["id"]),
            )

        # Act
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    failed = await _failed_findings(stack, run_id)
    total = next(f for f in failed if f["path"] == "target.cart.total")
    assert total["classification"] == "assertion_mismatch"
    cause = json.loads(total["attributed_cause_json"])
    assert "no immediate authoritative" in cause["reason"]
    # The action is still named — the engine declines to convict, it does not
    # decline to say which call was relevant.
    assert cause["tool_name"] == "apply_discount"


async def test_the_last_relevant_action_is_the_one_blamed(stack: FastAPI) -> None:
    """FR-055 says *last*, so a later successful call on the same path is the
    one accused even though an earlier one also touched it."""
    # Arrange — two discount calls; the second is the last relevant action.
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _select_and_arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/apply_discount:invoke",
            json={"arguments": {"code": "SAVE20"}},
        )
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    database: Database = stack.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT sequence_number FROM events WHERE run_id = ? AND tool_name = 'apply_discount' "
            "AND event_type = 'tool_invocation_completed' ORDER BY sequence_number",
            (run_id,),
        )
    last = rows[-1]["sequence_number"]

    failed = await _failed_findings(stack, run_id)
    total = next(f for f in failed if f["path"] == "target.cart.total")
    cause = json.loads(total["attributed_cause_json"])
    assert cause["event_sequence"] == last
