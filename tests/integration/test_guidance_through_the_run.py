"""005-T9 — guidance derived from authoritative state at every surface.

FR-120: "FastAPI derives this object from authoritative workspace/run state; the
frontend shall not invent a conflicting next action." §26.1's locked decision
extends that to every consumer: "Guidance state and `next_action` shall be
derived from the same server lifecycle state for the UI, WebMCP responses, and
audit trail."

The discipline has to hold on the server's own side of the wire too. A handler
that picked a phase itself would be a second opinion with no more authority than
the frontend's, and it would be wrong in precisely the situations guidance exists
for — the ones where the run did not end up where the caller assumed. So the
sharp test here is
`test_a_tool_result_after_the_ceiling_trips_does_not_say_keep_going`: the
invocation handler used to hardcode `running`, and would have told a caller to
continue against a run the server had just moved to `error`.

FR-122's other half is that a *transition* is a change of actor or action.
Re-recording the same phase on every request would bury the handoffs a reader is
looking for under repetitions of the state they were already in, so the stream
is append-only without being append-always.
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


async def _select(visitor: httpx.AsyncClient) -> str:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    return str(chosen["contract_id"])


async def _invoke(visitor: httpx.AsyncClient, run_id: str, tool: str, arguments: dict):
    return await visitor.post(
        f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
    )


#: Every expected sequence below begins with `contract_ready`: selecting a
#: contract hands the journey from the operator to the agent, and AC-21 records
#: each handoff. Arming is the second transition, not the first.
async def _guidance_phases(app: FastAPI, workspace_id: str) -> list[str]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT phase FROM guidance_events WHERE workspace_id = ? ORDER BY workspace_version",
            (workspace_id,),
        )
    return [row["phase"] for row in rows]


# --- the handoffs a run actually makes --------------------------------------


async def test_guidance_follows_the_run_through_its_handoffs(stack: FastAPI) -> None:
    """FR-122: control moves operator → agent → operator, and each move is
    recorded once."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _select(visitor)
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"},
        )

        # Act
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert await _guidance_phases(stack, workspace_id) == [
        "contract_ready",
        "armed",
        "running",
        "failed",
    ]


async def test_repeating_an_action_records_no_second_transition(
    stack: FastAPI,
) -> None:
    """The stream is append-only, not append-always.

    Three more invocations do not move control anywhere; recording a `running`
    handoff for each would bury the real ones.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _select(visitor)
        run_id = (await visitor.post(RUNS)).json()["run_id"]

        # Act
        for index in range(4):
            await _invoke(
                visitor,
                run_id,
                "update_cart",
                {"product_id": MUG, "quantity": 1, "request_id": f"req_repeat{index:03d}"},
            )

    # Assert
    assert await _guidance_phases(stack, workspace_id) == ["contract_ready", "armed", "running"]


async def test_selecting_a_contract_before_arming_moves_guidance(
    stack: FastAPI,
) -> None:
    """Guidance exists before any run does — that is why the workspace stream is
    separate from the run timeline (§12.13)."""
    # Arrange / Act
    async with client(stack) as visitor:
        before = (await visitor.get(WORKSPACE)).json()
        await _select(visitor)
        after = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert before["guidance"]["phase"] == "no_contract"
    assert before["next_action"]["action_code"] == "select_contract"
    assert after["guidance"]["phase"] == "contract_ready"
    assert after["next_action"]["action_code"] == "arm_run"


# --- every surface reads the same state -------------------------------------


async def test_a_tool_result_and_the_banner_agree(stack: FastAPI) -> None:
    """§26.1: the tool result and the visible banner resolve from one server
    derivation, so a client cannot be told two different things."""
    # Arrange
    async with client(stack) as visitor:
        await _select(visitor)
        run_id = (await visitor.post(RUNS)).json()["run_id"]

        # Act
        invocation = await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"},
        )
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert invocation.json()["next_action"] == workspace["next_action"]


async def test_a_verification_result_and_the_banner_agree(stack: FastAPI) -> None:
    # Arrange
    async with client(stack) as visitor:
        await _select(visitor)
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"},
        )

        # Act
        verified = await visitor.post(f"{RUNS}/{run_id}/verify")
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert verified.json()["next_action"] == workspace["next_action"]
    assert workspace["next_action"]["action_code"] == "review_findings"


async def test_a_tool_result_after_the_ceiling_trips_does_not_say_keep_going(
    stack: FastAPI,
) -> None:
    """The failure a hardcoded phase would have produced.

    The invocation handler used to derive `running` unconditionally. When the
    event ceiling trips, the server moves the run to `error` in that same
    request — and a `next_action` telling the caller to invoke another tool
    would be the server inventing a next action no state supports.
    """
    # Arrange — fill the ordinary budget so the next start trips the ceiling.
    from actionwitness_service.application import limits as fr008
    from actionwitness_service.persistence.repositories import EventRepository

    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _select(visitor)
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"},
        )

        async with database.reading() as work:
            used = await EventRepository(work).count(run_id)
            reserved = await fr008.WorkspaceCeilings(work, workspace_id).event_budget_remaining(
                run_id
            )
        async with database.transaction() as work:
            events = EventRepository(work)
            for _ in range(reserved):
                await events.append(run_id, {"event_type": "annotation_added", "actor": "harness"})
        assert used > 0

        # Act
        refused = await _invoke(visitor, run_id, "get_cart", {})
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert — the invocation is refused, and the banner now describes a run in
    # `error`, which returns the workspace to `contract_ready` rather than
    # telling anyone to keep invoking.
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "EVENT_LIMIT_EXCEEDED"
    assert workspace["next_action"]["action_code"] != "invoke_target_tool"


async def test_guidance_is_derived_after_the_state_change_not_before(
    stack: FastAPI,
) -> None:
    """Arming's recorded handoff must describe the armed workspace.

    Deriving before the run row and the active-run pointer were written would
    record `contract_ready` — the state the request arrived in — and the audit
    trail would show a handoff that never happened.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _select(visitor)
        await visitor.post(RUNS)

    # Assert
    assert await _guidance_phases(stack, workspace_id) == ["contract_ready", "armed"]


# --- isolation ---------------------------------------------------------------


async def test_guidance_is_per_workspace(stack: FastAPI) -> None:
    # Arrange
    async with client(stack) as alice, client(stack) as bob:
        alice_workspace = (await alice.get(WORKSPACE)).json()["workspace_id"]
        bob_workspace = (await bob.get(WORKSPACE)).json()["workspace_id"]
        await _select(alice)
        await alice.post(RUNS)

    # Assert
    assert await _guidance_phases(stack, alice_workspace) == ["contract_ready", "armed"]
    assert await _guidance_phases(stack, bob_workspace) == []


async def test_a_workspace_with_no_contract_is_told_to_choose_one(
    stack: FastAPI,
) -> None:
    """The projection reads state rather than assuming a starting point."""
    # Arrange / Act
    async with client(stack) as visitor:
        body = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert body["guidance"]["phase"] == "no_contract"
    assert body["guidance"]["active_actor"] == "operator"
    assert body["next_action"]["requires_human_input"] is True
