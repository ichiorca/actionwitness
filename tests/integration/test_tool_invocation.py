"""005-T3 — generic target-tool invocation (FR-031, FR-032, FR-033, FR-036, FR-008).

Run against a real Buggy Store over ADR-0001's injected client, because the
properties under test are about what was *independently observed* around a real
dispatch, and a fake adapter would make every one of them vacuous.

The test that matters most is
`test_a_reported_success_and_an_unchanged_observation_are_both_recorded`. Under
the `pre_fix` discount fault the store reports success and changes nothing, and
the invocation event has to carry both halves — the self-report saying `success`
and the observed state hash unchanged. That disagreement is the product. An
implementation that wrote the tool's claimed state version into the canonical
column would pass every other test here and delete the only evidence that
matters.
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


async def _put_the_store_in_pre_fix(app: FastAPI, workspace_id: str) -> None:
    """Arm the discount fault in the *store*, through the store's own API.

    The harness records a scenario selection (004-T11) but does not yet reseed
    the target through the adapter — that is 005-T10. Until it does, a test
    that needs a genuinely faulty target has to say so directly rather than
    setting a harness column and hoping. Doing it through the store's real
    surface also keeps this test honest about which component is lying.
    """
    adapter = app.state.adapters.adapter("buggy_store")
    store_client = adapter._client
    response = await store_client.post(
        "/demo/api/v1/store/scenario",
        headers={"X-Workspace-Id": workspace_id},
        json={
            "scenario_mode": "pre_fix",
            "fault_profile": "discount_reported_but_not_applied",
        },
    )
    assert response.status_code < 400, response.text


def _invoke_path(run_id: str, tool: str) -> str:
    return f"{RUNS}/{run_id}/target-tools/{tool}:invoke"


async def _events(app: FastAPI, run_id: str) -> list[dict]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence_number", (run_id,)
        )
    return [dict(row) for row in rows]


# --- the pipeline -----------------------------------------------------------


async def test_an_invocation_records_a_start_and_exactly_one_terminal_event(
    stack: FastAPI,
) -> None:
    """FR-031 and the "exactly one terminal event" rule together."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

        # Act
        response = await visitor.post(
            _invoke_path(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_first_call"}},
        )

    # Assert
    assert response.status_code == 200, response.text
    events = await _events(stack, run_id)
    invocation = [row for row in events if row["event_type"].startswith("tool_invocation_")]
    assert [row["event_type"] for row in invocation] == [
        "tool_invocation_started",
        "tool_invocation_completed",
    ]
    # Two would make the timeline ambiguous; zero would make a hung call look
    # like one that never started.
    terminal = [row for row in invocation if row["event_type"] != "tool_invocation_started"]
    assert len(terminal) == 1


async def test_the_first_action_moves_the_run_from_armed_to_running(
    stack: FastAPI,
) -> None:
    """§11.5: "Armed --> Running: first target action"."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        async with database.reading() as work:
            before = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))

        # Act
        await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

        async with database.reading() as work:
            after = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert before["status"] == "armed"
    assert after["status"] == "running"
    # Guidance follows the run, and the transition is recorded (FR-120).
    assert workspace["guidance"]["phase"] == "running"


async def test_a_read_only_tool_also_records_a_start_event(stack: FastAPI) -> None:
    """FR-031: "every selected-target tool invocation, **including reads**"."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

        # Act
        await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    events = await _events(stack, run_id)
    assert any(row["event_type"] == "tool_invocation_started" for row in events)


async def test_the_start_event_precedes_the_dispatch_in_sequence(stack: FastAPI) -> None:
    """ "before business logic executes" is an ordering claim, so it is checked
    against the sequence rather than against a timestamp."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await visitor.post(
            _invoke_path(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_ordering"}},
        )

    # Assert
    events = await _events(stack, run_id)
    started = next(r for r in events if r["event_type"] == "tool_invocation_started")
    completed = next(r for r in events if r["event_type"] == "tool_invocation_completed")
    assert started["sequence_number"] < completed["sequence_number"]
    assert started["correlation_id"] == completed["correlation_id"]


# --- the two channels, kept apart -------------------------------------------


async def test_a_reported_success_and_an_unchanged_observation_are_both_recorded(
    stack: FastAPI,
) -> None:
    """The product, in one assertion block.

    Under `pre_fix` the discount fault reports success and changes nothing. The
    event must carry the self-report saying `success` **and** an observed state
    hash that did not move. An implementation that wrote the tool's claimed
    state version into the canonical column would pass every other test in this
    file and delete exactly this evidence.
    """
    # Arrange — a genuinely faulty store, then a mug for the discount to
    # apply to.
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _put_the_store_in_pre_fix(stack, workspace_id)
        run_id = await _arm(visitor)
        await visitor.post(
            _invoke_path(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_seed_cart"}},
        )

        # Act
        response = await visitor.post(
            _invoke_path(run_id, "apply_discount"), json={"arguments": {"code": "SAVE20"}}
        )

    # Assert — the wire response keeps them apart too.
    body = response.json()
    assert body["reported"]["status"] == "success"
    assert body["observed"]["state_changed"] is False

    # Assert — and so does the stored event.
    events = await _events(stack, run_id)
    discount = [
        row
        for row in events
        if row["tool_name"] == "apply_discount" and row["event_type"].endswith("_completed")
    ]
    assert len(discount) == 1
    row = discount[0]
    assert row["reported_status"] == "success"
    # Canonical means observed: the hash either side of the call is identical,
    # so nothing the contract cares about moved.
    assert row["state_hash_before"] == row["state_hash_after"]


async def test_the_canonical_columns_hold_the_observation_not_the_tools_claim(
    stack: FastAPI,
) -> None:
    """FR-032 calls these values canonical, and canonical means observed.

    The tool's own claimed version is kept, but under `reported` in the payload
    where it cannot be mistaken for evidence.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _put_the_store_in_pre_fix(stack, workspace_id)
        run_id = await _arm(visitor)
        await visitor.post(
            _invoke_path(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_seed001"}},
        )
        await visitor.post(
            _invoke_path(run_id, "apply_discount"), json={"arguments": {"code": "SAVE20"}}
        )

    # Assert
    events = await _events(stack, run_id)
    row = next(r for r in events if r["tool_name"] == "apply_discount" and r["reported_status"])
    payload = json.loads(row["redacted_payload_json"])
    # The two channels are siblings under distinct keys.
    assert set(payload) >= {"reported", "observed"}
    assert payload["reported"]["status"] == "success"
    assert payload["observed"]["state_changed"] is False
    # The claim is retained rather than discarded — a disagreement is only
    # visible if both numbers survive.
    assert "state_version_after" in payload["reported"]


async def test_an_honest_mutation_moves_the_observed_state(stack: FastAPI) -> None:
    """The counterpart. Without this, "state_changed is False" could just mean
    the observation never works."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

        # Act
        response = await visitor.post(
            _invoke_path(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 2, "request_id": "req_real"}},
        )

    # Assert
    body = response.json()
    assert body["reported"]["status"] == "success"
    assert body["observed"]["state_changed"] is True

    events = await _events(stack, run_id)
    row = next(r for r in events if r["event_type"] == "tool_invocation_completed")
    assert row["state_hash_before"] != row["state_hash_after"]
    assert row["state_version_before"] != row["state_version_after"]


async def test_a_read_only_call_changes_nothing_it_observes(stack: FastAPI) -> None:
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        response = await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    assert response.json()["observed"]["state_changed"] is False


# --- schema validation ------------------------------------------------------


async def test_arguments_are_validated_against_the_published_schema(
    stack: FastAPI,
) -> None:
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

        # Act — a product the enum does not list and a quantity above maximum.
        response = await visitor.post(
            _invoke_path(run_id, "update_cart"),
            json={
                "arguments": {
                    "product_id": "not-a-product",
                    "quantity": 99,
                    "request_id": "req_long_enough",
                }
            },
        )

    # Assert — every problem at once, named by field (§10.2).
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "CONTRACT_VALIDATION_FAILED"
    assert {detail["path"] for detail in body["details"]} == {"product_id", "quantity"}


async def test_a_rejected_argument_records_no_event_at_all(stack: FastAPI) -> None:
    """Validation happens before anything is written: an argument the tool never
    accepts must not produce a start event claiming an invocation began."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        before = len(await _events(stack, run_id))

        # Act
        await visitor.post(_invoke_path(run_id, "update_cart"), json={"arguments": {"quantity": 1}})

    # Assert
    assert len(await _events(stack, run_id)) == before


async def test_an_unknown_argument_is_refused_rather_than_dropped(stack: FastAPI) -> None:
    """The published schemas set `additionalProperties: false`, and silently
    dropping a field would execute something the caller did not ask for."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        response = await visitor.post(
            _invoke_path(run_id, "update_cart"),
            json={
                "arguments": {
                    "product_id": MUG,
                    "quantity": 1,
                    "request_id": "req_extra_field",
                    "discount_everything": True,
                }
            },
        )

    # Assert
    assert response.status_code == 422
    assert any(
        detail["path"] == "discount_everything" for detail in response.json()["error"]["details"]
    )


async def test_schema_defaults_are_applied_before_dispatch(stack: FastAPI) -> None:
    """Defaulting here rather than in the adapter means the values that reached
    the target are the values the timeline recorded."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        response = await visitor.post(
            _invoke_path(run_id, "search_catalog"), json={"arguments": {"query": "mug"}}
        )

    # Assert
    assert response.status_code == 200
    assert response.json()["reported"]["status"] == "success"


# --- the allowlist and run state --------------------------------------------


async def test_a_tool_outside_the_allowlist_never_reaches_the_target(
    stack: FastAPI,
) -> None:
    """§20.2, FR-015. A 404: the tool does not exist for this caller, and a more
    precise answer would describe a surface they were not shown."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        response = await visitor.post(_invoke_path(run_id, "drop_database"), json={"arguments": {}})

    # Assert
    assert response.status_code == 404
    assert await _events(stack, run_id) != []  # the arming events are still there
    assert not any(row["tool_name"] == "drop_database" for row in await _events(stack, run_id))


async def test_a_verifying_run_refuses_a_new_action_with_the_race_code(
    stack: FastAPI,
) -> None:
    """FR-038: `RUN_ALREADY_VERIFYING`, and "that rejection creates no finding
    and no `tool_execution_error`" — so it writes no event either."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        async with database.transaction() as work:
            await work.execute("UPDATE runs SET status = 'verifying' WHERE id = ?", (run_id,))
        before = len(await _events(stack, run_id))

        # Act
        response = await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_ALREADY_VERIFYING"
    assert len(await _events(stack, run_id)) == before


async def test_a_terminal_run_accepts_no_further_actions(stack: FastAPI) -> None:
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        async with database.transaction() as work:
            await work.execute("UPDATE runs SET status = 'passed' WHERE id = ?", (run_id,))

        # Act
        response = await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    assert response.status_code == 409


async def test_a_second_client_cannot_invoke_against_the_first_ones_run(
    stack: FastAPI,
) -> None:
    """FR-036 and AC-11: events belong only to their own workspace's run."""
    # Arrange
    async with client(stack) as alice, client(stack) as bob:
        run_id = await _arm(alice)
        await bob.get(WORKSPACE)

        # Act — Bob is handed the run id.
        response = await bob.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    assert response.status_code == 404
    assert not any(
        row["event_type"] == "tool_invocation_started" for row in await _events(stack, run_id)
    )


# --- failure paths ----------------------------------------------------------


async def test_a_dispatch_failure_still_produces_exactly_one_terminal_event(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-033. Zero terminal events would leave the timeline claiming a call
    that never ended."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

        from integrations.buggy_store import BuggyStoreAdapter

        async def explode(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("the store is on fire at /var/lib/store.sqlite3")

        monkeypatch.setattr(BuggyStoreAdapter, "execute", explode)

        # Act
        response = await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["terminal_event"] == "tool_invocation_failed"
    # FR-033 / §20: the type, never the message.
    assert body["reported"]["error_code"] == "RuntimeError"
    assert "store.sqlite3" not in json.dumps(body)

    events = await _events(stack, run_id)
    terminal = [
        row
        for row in events
        if row["event_type"] in {"tool_invocation_completed", "tool_invocation_failed"}
    ]
    assert len(terminal) == 1
    # FR-032: the failed event type carries its outcome in its name and must not
    # also claim a self-reported status.
    assert terminal[0]["reported_status"] is None


async def test_a_failed_observation_still_terminates_the_invocation(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unobservable target makes the *verdict* unresolved, but the call still
    ended and the timeline has to say so."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

        from integrations.buggy_store import BuggyStoreObservationProvider

        original = BuggyStoreObservationProvider.capture
        calls = {"n": 0}

        async def flaky(self, workspace_id):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] > 1:  # the pre-call read succeeds, the post-call one fails
                raise RuntimeError("unreachable")
            return await original(self, workspace_id)

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", flaky)

        # Act
        response = await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    assert response.status_code == 200
    assert response.json()["observed"]["state_version"] is None

    events = await _events(stack, run_id)
    terminal = next(r for r in events if r["event_type"].startswith("tool_invocation_c"))
    # The absence is recorded rather than filled in with the tool's claim.
    assert terminal["state_hash_after"] is None
    payload = json.loads(terminal["redacted_payload_json"])
    assert payload["observed"]["available"] is False


async def test_an_unobservable_target_before_the_call_refuses_to_invoke(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constitution §5: an observation failure is an explicit non-pass. With no
    baseline there is nothing to compare against, so nothing is dispatched."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        before = len(await _events(stack, run_id))

        from integrations.buggy_store import BuggyStoreObservationProvider

        async def always_fails(self, workspace_id):  # type: ignore[no-untyped-def]
            raise RuntimeError("unreachable")

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", always_fails)

        # Act
        response = await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    assert response.status_code == 500
    assert len(await _events(stack, run_id)) == before


# --- the event ceiling ------------------------------------------------------


async def test_the_event_ceiling_trips_at_the_next_invocation_start(
    stack: FastAPI,
) -> None:
    """FR-008's boundary, at the moment the requirement names it."""
    # Arrange — fill the ordinary budget directly, then attempt one more call.
    from actionwitness_service.application import limits as fr008
    from actionwitness_service.persistence.repositories import EventRepository

    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        async with database.reading() as work:
            used = await EventRepository(work).count(run_id)
        async with database.transaction() as work:
            events = EventRepository(work)
            for _ in range(fr008.ORDINARY_EVENTS_PER_RUN - used):
                await events.append(run_id, {"event_type": "annotation_added", "actor": "harness"})

        # Act
        response = await visitor.post(_invoke_path(run_id, "get_cart"), json={"arguments": {}})

    # Assert — 409, the registry's status for this code: a lifecycle conflict
    # rather than a rate limit.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVENT_LIMIT_EXCEEDED"

    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
    assert run["status"] == "error"
    events_now = await _events(stack, run_id)
    # The reserved slot received the boundary event, and evidence is preserved.
    assert len(events_now) == fr008.EVENTS_PER_RUN
    assert events_now[-1]["event_type"] == "resource_limit_exceeded"
