"""What one human approval does and does not authorize (FR-066, constitution §5).

`test_confirmation_decision.py` covers the three decisions a person can make.
This file covers the two ways an approval that was correctly given can still be
spent on the wrong thing, both of which were reproduced against the live service
before the fix:

**Spent twice.** The resume path read a row with `status = 'approved'`, observed
state, dispatched, and only consumed the approval afterwards. Two resumes
overlapping anywhere inside that window both read the same live row and both
reached the adapter, so one person's single consent stood behind two mutations
and one correlation id carried two terminal events.

**Spent on other arguments.** The approval was matched by workspace, run, and
tool name, and revalidated only against the observed-state binding hash — which
`binding_hash` computes from the observation payload and deliberately nothing
else. A resume could therefore carry arguments nobody had ever been shown, and
the timeline would still display the approved ones.

Every test here asks the *store* what exists, for the same reason the decision
tests do: "no mutation occurred" is a claim the harness makes about itself, and
this product exists because such claims need checking.
"""

from __future__ import annotations

import asyncio
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

#: The arguments a human is shown and approves.
APPROVED_ARGUMENTS = {"request_id": "req_APPROVED_ARGS"}

#: Arguments no human ever saw. Schema-valid, so nothing but the binding can
#: refuse them — which is the whole point: a check that only ever fires on
#: malformed input would not be the check §5 asks for.
SWAPPED_ARGUMENTS = {"request_id": "req_NEVER_APPROVED"}

#: §16.1's three ways an invocation can end. Counted rather than named
#: individually, because "exactly one terminal event" is the property, and a
#: double-spend that failed at the target the second time would still have
#: written a second one.
TERMINAL_EVENTS = frozenset(
    {"tool_invocation_completed", "tool_invocation_failed", "tool_invocation_cancelled"}
)


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


async def _approved(visitor: httpx.AsyncClient) -> tuple[str, str]:
    """A run whose checkout is paused and then approved. Returns (run, confirmation)."""
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
        json={"arguments": APPROVED_ARGUMENTS},
    )
    assert paused.json()["status"] == "awaiting_confirmation", paused.text
    confirmation_id = str(paused.json()["confirmation"]["confirmation_id"])
    decision = await visitor.post(
        f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
        json={"decision": "approve_once"},
    )
    assert decision.json()["status"] == "approved", decision.text
    return run_id, confirmation_id


async def _resume(
    visitor: httpx.AsyncClient, run_id: str, arguments: dict[str, str]
) -> httpx.Response:
    """§14.14: the invoking page calls back once the human has decided."""
    return await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": arguments},
    )


def _outcome(response: httpx.Response) -> str:
    """One word for what a resume did, precise enough to count.

    The route says `"completed"` for every invocation that finished, including
    one that finished by refusing — a stale approval comes back `completed` with
    a `confirmation_cancelled` terminal. Counting the coarse word would let a
    resume that did nothing pass as the resume that acted, which is exactly the
    confusion these tests exist to rule out, so the terminal event is what gets
    counted.
    """
    if response.status_code != 200:
        return f"error:{response.status_code}"
    body = response.json()
    if body.get("status") != "completed":
        return str(body.get("status"))
    return str(body.get("terminal_event"))


async def _order(app: FastAPI, workspace_id: str) -> dict:
    response = await app.state.store_client.get(
        f"{STORE}/store/cart", headers={"X-Workspace-Id": workspace_id}
    )
    return response.json()["order"]


async def _checkout_events(app: FastAPI, run_id: str) -> list[dict]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT sequence_number, event_type, correlation_id, request_id FROM events "
            "WHERE run_id = ? AND tool_name = 'proceed_to_checkout' ORDER BY sequence_number",
            (run_id,),
        )
    return [dict(row) for row in rows]


async def _confirmation(app: FastAPI, confirmation_id: str) -> dict:
    database: Database = app.state.database
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT status, arguments_hash, consumed_at FROM confirmation_requests WHERE id = ?",
            (confirmation_id,),
        )
    assert row is not None
    return dict(row)


# --- one approval, one mutation ----------------------------------------------


async def test_two_overlapping_resumes_spend_one_approval_once(stack: FastAPI) -> None:
    """FR-066: the approval is claimed atomically, before its mutation.

    The sequential version of this is already covered; concurrency is the case
    the old ordering actually lost. Reading the approval, observing state,
    dispatching, and consuming afterwards left a window minutes wide in which a
    second resume could read the same live row — and both then reached the
    adapter with `human_consent_granted=True`.

    The assertions are about what exists afterwards rather than about which
    request won. Either may win; what may never happen is that both do.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _approved(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        assert (await _order(stack, workspace_id))["created"] is False

        # Act — two resumes of the one approval, in flight together.
        first, second = await asyncio.gather(
            _resume(visitor, run_id, APPROVED_ARGUMENTS),
            _resume(visitor, run_id, APPROVED_ARGUMENTS),
        )

    # Assert — exactly one of them performed the action.
    outcomes = sorted(_outcome(response) for response in (first, second))
    acted = [outcome for outcome in outcomes if outcome == "tool_invocation_completed"]
    assert len(acted) == 1, f"expected exactly one acting resume, got {outcomes}"

    # The loser did not mutate anything, and which of the two fail-closed shapes
    # it takes depends on where the scheduler cut between them — both are
    # asserted because both are correct, and neither dispatches:
    #
    # * `error:409` — it read the same live approval and lost the claim, or it
    #   observed the winner's mutation and found the approval already spent;
    # * `awaiting_confirmation` — it looked for a live approval *after* the
    #   winner had claimed it, found none, and asked a human afresh.
    #
    # What is asserted deterministically is the invariant underneath, on the
    # three lines below: one terminal event, one order, one spent approval.
    loser = next(outcome for outcome in outcomes if outcome != "tool_invocation_completed")
    assert loser in {"error:409", "awaiting_confirmation"}, loser

    # One terminal event under one correlation id. Two would make the timeline
    # ambiguous about how many times this action ran.
    events = await _checkout_events(stack, run_id)
    terminals = [event for event in events if event["event_type"] in TERMINAL_EVENTS]
    assert len(terminals) == 1, [event["event_type"] for event in events]

    # And exactly one order at the store, which is what a spent-twice approval
    # actually costs.
    assert (await _order(stack, workspace_id))["created"] is True

    # One approval was granted, and start events are deliberately *not* counted
    # here: a loser that asked a human afresh legitimately records a second
    # start for the second request it raised. Terminals and approvals are the
    # things that must stay at one.
    approvals = [event for event in events if event["event_type"] == "confirmation_approved"]
    assert len(approvals) == 1, [event["event_type"] for event in events]

    # The approval is spent, once, and says when.
    record = await _confirmation(stack, confirmation_id)
    assert record["status"] == "consumed"
    assert record["consumed_at"] is not None


async def test_a_spent_approval_never_lets_a_second_resume_through(
    stack: FastAPI,
) -> None:
    """The sequential half of the same rule, and the one that stays deterministic.

    Claiming before dispatch moved *when* the approval is spent, so this pins
    that the move did not change what a second resume is allowed to do: it may
    ask a human again, and it may not act on the answer the first one already
    used.
    """
    # Arrange — the approval is spent by an ordinary resume.
    async with client(stack) as visitor:
        run_id, confirmation_id = await _approved(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        first = await _resume(visitor, run_id, APPROVED_ARGUMENTS)
        assert _outcome(first) == "tool_invocation_completed", first.text
        order_after_first = await _order(stack, workspace_id)

        # Act — a second resume, now that nothing authorizes it.
        second = await _resume(visitor, run_id, APPROVED_ARGUMENTS)

    # Assert — it pauses for a fresh human decision rather than acting, and the
    # store still holds the one order the approval paid for.
    assert _outcome(second) == "awaiting_confirmation", second.text
    order_after_second = await _order(stack, workspace_id)
    assert order_after_second["order_id"] == order_after_first["order_id"]
    assert (await _confirmation(stack, confirmation_id))["status"] == "consumed"


# --- the approval is bound to the arguments it was shown for -----------------


async def test_an_approval_does_not_authorize_arguments_nobody_approved(
    stack: FastAPI,
) -> None:
    """Constitution §5: consent is bound to "the workspace, run, action,
    arguments, and expiry".

    The observed-state binding hash cannot catch this on its own. Both argument
    sets describe a checkout of the same cart, so the world the human was shown
    is unchanged; only a hash of the arguments themselves can tell the approved
    action from the substituted one.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, _ = await _approved(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

        # Act — resume with arguments the dialog never carried.
        swapped = await _resume(visitor, run_id, SWAPPED_ARGUMENTS)

    # Assert — refused, and nothing was done.
    assert swapped.status_code == 409, swapped.text
    assert swapped.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert swapped.json()["error"]["retryable"] is False
    assert (await _order(stack, workspace_id))["created"] is False

    events = await _checkout_events(stack, run_id)
    assert [event["event_type"] for event in events if event["event_type"] in TERMINAL_EVENTS] == []


async def test_a_refused_argument_swap_leaves_the_real_consent_usable(
    stack: FastAPI,
) -> None:
    """The substitution is refused; the human's actual consent is not burned.

    Deliberately unlike a stale state binding, which cancels the approval
    because the world it described has gone. Here the approval still describes
    exactly what a person read, so the right resume must still be able to spend
    it — otherwise an agent could destroy consent it was refused.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, confirmation_id = await _approved(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        swapped = await _resume(visitor, run_id, SWAPPED_ARGUMENTS)
        assert swapped.status_code == 409, swapped.text
        assert (await _confirmation(stack, confirmation_id))["status"] == "approved"

        # Act — the resume the human actually authorized.
        honest = await _resume(visitor, run_id, APPROVED_ARGUMENTS)

    # Assert
    assert honest.json()["status"] == "completed", honest.text
    assert (await _order(stack, workspace_id))["created"] is True


async def test_an_approval_recording_no_arguments_authorizes_nothing(
    stack: FastAPI,
) -> None:
    """A row from before the binding existed fails closed.

    Migration 6 is additive and nullable, so a database written earlier holds
    approvals whose arguments were never recorded. Nobody can say what those
    humans were shown, and §5 makes an ambiguity an explicit non-pass rather
    than a degradation to success — so the unknown binding is refused rather
    than waved through.
    """
    # Arrange — an approval indistinguishable from a pre-migration one.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id, confirmation_id = await _approved(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        async with database.transaction() as work:
            await work.execute(
                "UPDATE confirmation_requests SET arguments_hash = NULL WHERE id = ?",
                (confirmation_id,),
            )

        # Act — the very arguments that were approved, against an unbound row.
        resumed = await _resume(visitor, run_id, APPROVED_ARGUMENTS)

    # Assert
    assert resumed.status_code == 409, resumed.text
    assert resumed.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert (await _order(stack, workspace_id))["created"] is False


async def test_the_approval_records_the_arguments_it_was_raised_for(
    stack: FastAPI,
) -> None:
    """The binding is persisted at request time, not reconstructed at resume.

    Reconstructing it later would hash whatever the resume happened to send,
    which is the substitution rather than the check.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        _, confirmation_id = await _approved(visitor)

    # Assert
    record = await _confirmation(stack, confirmation_id)
    assert isinstance(record["arguments_hash"], str)
    assert record["arguments_hash"].startswith("sha256:")


# --- one invocation, one identity --------------------------------------------


async def test_a_paused_invocation_keeps_one_request_id_across_its_pause(
    stack: FastAPI,
) -> None:
    """The start and terminal events of one invocation name one request.

    The resumed half used to rebuild the identifier by string surgery on the
    correlation id, which produced a third value: not the key the start event
    recorded, and not the key the target deduplicated on. FR-063 judges
    "repeating one request ID" against canonical state, so a terminal event
    naming a request nobody made makes that judgement unanswerable — and 007's
    replayer, which prefers the recorded argument, would classify the replay
    differently from the run it was cut from.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id, _ = await _approved(visitor)

        # Act
        resumed = await _resume(visitor, run_id, APPROVED_ARGUMENTS)
        assert resumed.json()["status"] == "completed", resumed.text

    # Assert
    events = await _checkout_events(stack, run_id)
    started = [event for event in events if event["event_type"] == "tool_invocation_started"]
    terminals = [event for event in events if event["event_type"] in TERMINAL_EVENTS]
    assert len(started) == 1 and len(terminals) == 1

    assert started[0]["request_id"] == terminals[0]["request_id"]
    # And it is the caller's own idempotency key — the one the target
    # deduplicates on — rather than anything the harness minted.
    assert started[0]["request_id"] == APPROVED_ARGUMENTS["request_id"]
    # One correlation across the whole invocation, unchanged by any of this.
    assert len({event["correlation_id"] for event in events}) == 1
