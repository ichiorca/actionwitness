"""005-T5 — the verification race gate and the mutation lease (FR-038, FR-039).

FR-038's word is **atomically**, so the tests that matter are the concurrent
ones. A check-then-update written as two statements passes every single-client
test here and admits two verifications under load — and two verifications mean
two final snapshots, which is the "partial final snapshot" the requirement's
last sentence forbids.

The other clause worth testing directly is the one about network flight: "a tool
invocation that was still in network flight and had not recorded its start loses
the race and receives the same rejection." That is a statement about what
happens when an invocation and a verify request overlap, so it is tested by
overlapping them rather than by reasoning about the code.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.verification_gate import (
    LEASED_RUN_STATES,
    require_no_lease,
)
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"}
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
            environ=ENV,
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


async def _arm(visitor: httpx.AsyncClient) -> str:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    armed = await visitor.post(RUNS)
    assert armed.status_code == 201, armed.text
    return str(armed.json()["run_id"])


async def _act(visitor: httpx.AsyncClient, run_id: str, *, request_id: str) -> httpx.Response:
    """One completed target action, so the run has something to verify."""
    return await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": request_id}},
    )


async def _status(app: FastAPI, run_id: str) -> str:
    database: Database = app.state.database
    async with database.reading() as work:
        row = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
    return str(row["status"])


# --- the transition ---------------------------------------------------------


async def test_verification_takes_a_running_run_to_a_terminal_state(
    stack: FastAPI,
) -> None:
    """The winner does not stop at `verifying`: it observes, judges, and seals
    in the same request, because FastAPI is the sole transition authority and
    splitting it would leave the run parked in an intermediate state."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 200
    assert await _status(stack, run_id) in {"passed", "passed_with_warnings", "failed"}


async def test_an_armed_run_with_no_action_cannot_be_verified(stack: FastAPI) -> None:
    """Verifying here would judge a contract against a target nobody touched.

    Refused by the core's transition table — `armed` leads to `running`,
    `cancelled`, or `error`, never straight to `verifying` — so §16's
    invalid-transition mapping answers, and the gate needs no second opinion.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_IN_PROGRESS"
    assert await _status(stack, run_id) == "armed"


async def test_a_second_verify_loses_with_the_race_code(stack: FastAPI) -> None:
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")
        first = await visitor.post(f"{RUNS}/{run_id}/verify")

        # Act
        second = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert — the second request is refused and cannot be retried into
    # success. It arrives after the winner already sealed the run, so the
    # answer is the invalid-transition refusal for a terminal run rather than
    # FR-038's in-flight code; the concurrent tests below cover the window
    # where `RUN_ALREADY_VERIFYING` is the right answer.
    assert first.status_code == 200
    assert second.status_code == 409
    body = second.json()["error"]
    assert body["code"] == "RUN_IN_PROGRESS"
    assert body["retryable"] is False


async def test_concurrent_verifications_produce_exactly_one_winner(
    stack: FastAPI,
) -> None:
    """FR-038's "atomically", tested the only way it can be.

    A check-then-update written as two statements passes every other test in
    this file and admits both of these.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")

        # Act
        first, second = await asyncio.gather(
            visitor.post(f"{RUNS}/{run_id}/verify"),
            visitor.post(f"{RUNS}/{run_id}/verify"),
        )

    # Assert
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"]["code"] == "RUN_ALREADY_VERIFYING"
    assert await _status(stack, run_id) in {"passed", "passed_with_warnings", "failed"}


async def test_many_concurrent_verifications_still_produce_one_winner(
    stack: FastAPI,
) -> None:
    """Two requests can win by luck; eight cannot."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")

        # Act
        responses = await asyncio.gather(
            *(visitor.post(f"{RUNS}/{run_id}/verify") for _ in range(8))
        )

    # Assert
    accepted = [r for r in responses if r.status_code == 200]
    assert len(accepted) == 1
    # Every loser is refused. Which refusal depends on whether it arrived while
    # the winner held `verifying` or after the run was sealed — both are 409 and
    # neither can be retried into a second verdict, which is the property that
    # matters.
    assert all(r.status_code == 409 for r in responses if r is not accepted[0])
    assert {r.json()["error"]["code"] for r in responses if r.status_code == 409} <= {
        "RUN_ALREADY_VERIFYING",
        "RUN_IN_PROGRESS",
    }


# --- nothing in flight ------------------------------------------------------


async def test_an_invocation_still_in_flight_blocks_verification(
    stack: FastAPI,
) -> None:
    """In flight is read off the timeline: a start event with no terminal event.

    Defining it that way rather than with a flag is what makes it survive a
    restart, so the test writes the timeline rather than setting a flag.
    """
    # Arrange — a completed action, then an orphaned start event.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")

        from actionwitness_service.persistence.repositories import EventRepository

        async with database.transaction() as work:
            await EventRepository(work).append(
                run_id,
                {
                    "event_type": "tool_invocation_started",
                    "actor": "agent",
                    "tool_name": "update_cart",
                    "correlation_id": "corr_never_finished",
                },
            )

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert — the accurate reason: something has not finished, rather than
    # "nothing completed", which in `running` can only be caused by this.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_IN_PROGRESS"
    assert await _status(stack, run_id) == "running"


async def test_a_finished_invocation_does_not_block_verification(
    stack: FastAPI,
) -> None:
    """The counterpart: without it, "in flight" could just mean "any start
    event ever recorded" and verification would never be reachable."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")
        await _act(visitor, run_id, request_id="req_action002")
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 200


async def test_an_unresolved_confirmation_blocks_verification(stack: FastAPI) -> None:
    """FR-038 names confirmations alongside invocations."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO confirmation_requests (
                    id, workspace_id, run_id, correlation_id, tool_name,
                    state_binding_hash, consequence_summary_json, status,
                    expires_at, created_at
                ) VALUES ('cnf_pending', ?, ?, 'corr_1', 'proceed_to_checkout',
                          'sha256:x', '{}', 'pending', ?, ?)
                """,
                (workspace_id, run_id, work.now(), work.now()),
            )

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_IN_PROGRESS"
    assert await _status(stack, run_id) == "running"


async def test_a_decided_confirmation_does_not_block_verification(
    stack: FastAPI,
) -> None:
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO confirmation_requests (
                    id, workspace_id, run_id, correlation_id, tool_name,
                    state_binding_hash, consequence_summary_json, status,
                    expires_at, created_at
                ) VALUES ('cnf_done', ?, ?, 'corr_1', 'proceed_to_checkout',
                          'sha256:x', '{}', 'approved', ?, ?)
                """,
                (workspace_id, run_id, work.now(), work.now()),
            )

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 200


# --- losing the race --------------------------------------------------------


async def test_an_action_after_verification_completes_is_rejected(stack: FastAPI) -> None:
    """409 and no event written — "that rejection creates no finding and no
    `tool_execution_error`".

    The code is `RUN_TIMELINE_SEALED` rather than FR-038's
    `RUN_ALREADY_VERIFYING` because verification is synchronous: by the time a
    later action arrives the run is terminal, and its timeline genuinely is
    sealed. The window where `RUN_ALREADY_VERIFYING` is the right answer is
    covered by `test_a_verifying_run_refuses_a_new_action_with_the_race_code`
    in the invocation suite, and by the overlap test below.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")
        await visitor.post(f"{RUNS}/{run_id}/verify")
        async with database.reading() as work:
            before = await work.fetch_all("SELECT id FROM events WHERE run_id = ?", (run_id,))

        # Act
        response = await _act(visitor, run_id, request_id="req_too_late01")

        async with database.reading() as work:
            after = await work.fetch_all("SELECT id FROM events WHERE run_id = ?", (run_id,))

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_TIMELINE_SEALED"
    assert len(after) == len(before)


async def test_an_invocation_overlapping_verification_loses_cleanly(
    stack: FastAPI,
) -> None:
    """ "A tool invocation that was still in network flight and had not recorded
    its start loses the race and receives the same rejection."

    Tested by overlapping the two requests rather than by reasoning about the
    code. Either ordering is legitimate; what must never happen is both
    succeeding, or the invocation half-writing a start event with no terminal.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")

        # Act
        invocation, verification = await asyncio.gather(
            _act(visitor, run_id, request_id="req_overlapped"),
            visitor.post(f"{RUNS}/{run_id}/verify"),
        )

    # Assert — exactly one of the two outcomes, never a mixture.
    assert verification.status_code in {200, 409}
    if verification.status_code == 200 and invocation.status_code == 409:
        # Which refusal the loser gets depends on how far the winner had run,
        # not on whether the gate worked. Starved past the seal — which is what
        # a loaded machine does — the run is already terminal, and the honest
        # answer is that its timeline is closed rather than that verification is
        # still going. Both are the same fail-closed refusal from the same
        # guard, so pinning only the first made this assert the scheduler.
        assert invocation.json()["error"]["code"] in {
            "RUN_ALREADY_VERIFYING",
            "RUN_TIMELINE_SEALED",
        }
        assert invocation.json()["error"]["retryable"] is False
    else:
        assert invocation.status_code == 200

    # And the timeline has no start event without its terminal event.
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT event_type, correlation_id FROM events WHERE run_id = ? "
            "AND event_type LIKE 'tool_invocation_%'",
            (run_id,),
        )
    started = {r["correlation_id"] for r in rows if r["event_type"].endswith("_started")}
    finished = {r["correlation_id"] for r in rows if not r["event_type"].endswith("_started")}
    assert started == finished


async def test_a_losing_verification_leaves_the_run_untouched(stack: FastAPI) -> None:
    """ "No invalid or racing request may capture a partial final snapshot."

    The gate runs before any observation is taken, so a losing request has
    nothing to capture — asserted as the absence of a second snapshot.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")
        await visitor.post(f"{RUNS}/{run_id}/verify")

        # Act
        loser = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert loser.status_code == 409
    async with database.reading() as work:
        snapshots = await work.fetch_all(
            "SELECT phase FROM snapshots WHERE run_id = ? ORDER BY phase", (run_id,)
        )
    # Exactly one of each phase: arming took `before`, the winner took `after`,
    # and the loser took nothing. A second `after` would be the partial final
    # snapshot FR-038 forbids, so the count is asserted rather than assumed.
    assert [row["phase"] for row in snapshots] == ["after", "before"]


async def test_a_second_client_cannot_verify_the_first_ones_run(stack: FastAPI) -> None:
    """FastAPI is the sole transition authority, and it authorizes by cookie."""
    # Arrange
    async with client(stack) as alice, client(stack) as bob:
        run_id = await _arm(alice)
        await _act(alice, run_id, request_id="req_action001")
        await bob.get(WORKSPACE)

        # Act
        response = await bob.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 404
    assert await _status(stack, run_id) == "running"


async def test_a_terminal_run_cannot_be_verified_again(stack: FastAPI) -> None:
    """The core's transition table is the authority, so `passed -> verifying`
    is refused there rather than by a second opinion here."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _act(visitor, run_id, request_id="req_action001")
        async with database.transaction() as work:
            await work.execute("UPDATE runs SET status = 'passed' WHERE id = ?", (run_id,))

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_IN_PROGRESS"


# --- FR-039's lease ---------------------------------------------------------


async def test_the_lease_covers_exactly_the_four_states_fr_039_names() -> None:
    """Named rather than "any active run": FR-039 keeps reads, reset, and
    confirmation decisions available, and a lease over every state would break
    the recovery paths it exists alongside."""
    # Arrange / Act / Assert
    assert {
        "armed",
        "running",
        "awaiting_confirmation",
        "verifying",
    } == LEASED_RUN_STATES


@pytest.mark.parametrize("status", sorted(LEASED_RUN_STATES))
async def test_the_lease_refuses_a_direct_human_mutation(stack: FastAPI, status: str) -> None:
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        async with database.transaction() as work:
            await work.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    # Act / Assert
    async with database.reading() as work:
        with pytest.raises(ApiError) as caught:
            await require_no_lease(work, workspace_id)
    assert caught.value.code is ApiErrorCode.RUN_MUTATION_LOCKED
    assert caught.value.http_status == 409


@pytest.mark.parametrize("status", ["passed", "failed", "cancelled", "error"])
async def test_a_terminal_run_holds_no_lease(stack: FastAPI, status: str) -> None:
    """A finished run must not lock a workspace out of its target forever."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        async with database.transaction() as work:
            await work.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))

    # Act / Assert — no exception.
    async with database.reading() as work:
        await require_no_lease(work, workspace_id)


async def test_one_workspaces_lease_does_not_bind_another(stack: FastAPI) -> None:
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as alice, client(stack) as bob:
        await _arm(alice)
        bob_workspace = (await bob.get(WORKSPACE)).json()["workspace_id"]

    # Act / Assert
    async with database.reading() as work:
        await require_no_lease(work, bob_workspace)


# --- the selection gap this task closed -------------------------------------


async def test_a_contract_cannot_be_reselected_while_a_run_is_in_flight(
    stack: FastAPI,
) -> None:
    """FR-012: "Changing any value requires reset and creates a new run."

    Found while implementing the lease: selection was unguarded, so a workspace
    could end up pointing at one contract while its run was judged by another.
    """
    # Arrange
    async with client(stack) as visitor:
        await _arm(visitor)
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        other = next(t for t in templates if t["source_template_id"] != CANONICAL)

        # Act
        response = await visitor.post(f"{CONTRACTS}/{other['contract_id']}/select")

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_IN_PROGRESS"


async def test_reset_still_frees_the_workspace_to_reselect(stack: FastAPI) -> None:
    """FR-013 keeps reset available, and FR-039 keeps it out of the lease."""
    # Arrange
    async with client(stack) as visitor:
        await _arm(visitor)
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        other = next(t for t in templates if t["source_template_id"] != CANONICAL)

        # Act
        assert (await visitor.post(f"{WORKSPACE}/reset")).status_code == 200
        response = await visitor.post(f"{CONTRACTS}/{other['contract_id']}/select")

    # Assert
    assert response.status_code == 200
