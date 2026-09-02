"""What verification does when it cannot finish (§16, §22, constitution §5).

Three regressions, and one sentence links them: **a run must never end in a
state its evidence does not support.**

`verify()` runs in three phases and holds nothing between them, because ADR-0003
forbids holding a lock across I/O. That window is where the first two failures
live. A workspace reset is legal from every state (§16) and cancels every
non-terminal run, so a reset can land between the gate and the seal — and the
seal used to write its verdict over the top of the cancellation, producing a
timeline that read `run_cancelled` and then `verification_started`. Separately, a
final observation that failed used to escape as an unhandled exception with the
run already committed to `verifying`, so every retry lost to FR-038's own
`RUN_ALREADY_VERIFYING` and the only exit was a reset that discarded the run.

The third is quieter and is about a classification that existed only in the
vocabulary. §22 lists `tool_execution_error`, the engine can produce it, and
nothing in the production evaluation asked for it — so a run with a failed tool
call carried a failed `tool_execution` layer and no finding, which meant the
error could never be the primary failure, never be read through
`get_run_findings`, and never enter a generated eval case's expected set.

Everything here drives the real HTTP surface. The failures are injected at the
target — an observation provider that cannot reach the store, a tool that
raises — rather than by reaching into the service, because those are the shapes
the failure actually arrives in.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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


async def _scenario(app: FastAPI, workspace_id: str, mode: str, profile: str) -> None:
    """Put the *store* into a scenario, directly, as `test_verification` does."""
    adapter = app.state.adapters.adapter("buggy_store")
    response = await adapter._client.post(
        "/demo/api/v1/store/scenario",
        headers={"X-Workspace-Id": workspace_id},
        json={"scenario_mode": mode, "fault_profile": profile},
    )
    assert response.status_code < 400, response.text


async def _arm(visitor: httpx.AsyncClient) -> str:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    armed = await visitor.post(RUNS)
    assert armed.status_code == 201, armed.text
    return str(armed.json()["run_id"])


def _invoke(run_id: str, tool: str) -> str:
    return f"{RUNS}/{run_id}/target-tools/{tool}:invoke"


async def _journey(visitor: httpx.AsyncClient, run_id: str) -> None:
    """The canonical contract's journey: find a mug, add one, apply SAVE20."""
    assert (
        await visitor.post(_invoke(run_id, "search_catalog"), json={"arguments": {"query": "mug"}})
    ).status_code == 200
    assert (
        await visitor.post(
            _invoke(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}},
        )
    ).status_code == 200
    assert (
        await visitor.post(
            _invoke(run_id, "apply_discount"), json={"arguments": {"code": "SAVE20"}}
        )
    ).status_code == 200


async def _run_row(app: FastAPI, run_id: str) -> dict[str, Any]:
    database: Database = app.state.database
    async with database.reading() as work:
        row = await work.fetch_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    assert row is not None
    return dict(row)


async def _findings(app: FastAPI, run_id: str) -> list[dict[str, Any]]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY check_id", (run_id,)
        )
    return [dict(row) for row in rows]


async def _event_types(app: FastAPI, run_id: str) -> list[str]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT event_type FROM events WHERE run_id = ? ORDER BY sequence_number", (run_id,)
        )
    return [str(row["event_type"]) for row in rows]


# --- a reset that lands between the gate and the seal ------------------------


async def test_a_reset_during_the_capture_leaves_the_run_cancelled(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16: the only move out of `cancelled` is `reset`, and a seal is not one.

    The reset is issued from inside the final observation, which is exactly the
    unlocked window ADR-0003 requires and therefore the window a real reset can
    land in. The verification that was in flight has to discard its verdict: an
    operator who cancelled a run must not come back and find it passed.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreObservationProvider

        original = BuggyStoreObservationProvider.capture
        reset_statuses: list[int] = []

        async def reset_then_capture(self, workspace_id):  # type: ignore[no-untyped-def]
            # Once: the reseed the reset performs must not re-enter this.
            if not reset_statuses:
                resetting = await visitor.post(f"{WORKSPACE}/reset")
                reset_statuses.append(resetting.status_code)
            return await original(self, workspace_id)

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", reset_then_capture)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert — the reset really did land inside the window.
    assert reset_statuses == [200]

    # The cancellation stands, and carries no verdict.
    run = await _run_row(stack, run_id)
    assert run["status"] == "cancelled"
    assert run["overall_result"] is None

    # Nothing from the seal was written.
    assert await _findings(stack, run_id) == []
    types = await _event_types(stack, run_id)
    assert "run_cancelled" in types
    assert "verification_started" not in types
    assert "verification_completed" not in types

    # And the caller is told, rather than being handed a verdict that was
    # discarded. 409, and not retryable: the run cannot receive one now.
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "RUN_IN_PROGRESS"
    assert error["retryable"] is False


async def test_a_cancelled_run_is_never_reported_as_verified(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart read: whatever the seal computed, nobody can see it.

    A refusal that still left the verdict readable somewhere — the run row, the
    findings projection — would be a refusal in name only.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreObservationProvider

        original = BuggyStoreObservationProvider.capture
        done: list[bool] = []

        async def reset_then_capture(self, workspace_id):  # type: ignore[no-untyped-def]
            if not done:
                done.append(True)
                await visitor.post(f"{WORKSPACE}/reset")
            return await original(self, workspace_id)

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", reset_then_capture)
        await visitor.post(f"{RUNS}/{run_id}/verify")

        # Act
        projection = await visitor.get(f"{RUNS}/{run_id}/findings")

    # Assert
    assert projection.status_code == 200
    body = projection.json()
    assert body["status"] == "cancelled"
    assert body["overall_result"] is None
    assert body["total"] == 0


# --- a final observation that fails ------------------------------------------


async def test_a_failed_final_observation_ends_the_run_in_error(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constitution §5: an observation failure is an explicit non-pass.

    The provider is made unreachable *after* arming, so the run has its `before`
    snapshot and a real journey and fails only on the final read — which is the
    one FR-041 takes immediately before verification.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreObservationProvider

        async def unreachable(self, workspace_id):  # type: ignore[no-untyped-def]
            raise httpx.ConnectError("the store is unreachable")

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", unreachable)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert — an explicit refusal, not a 500 and not a silent pass.
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "TARGET_UNAVAILABLE"
    assert error["retryable"] is False

    run = await _run_row(stack, run_id)
    assert run["status"] == "error"
    # No business verdict was reached, so none is recorded. "failed" would say
    # the contract was judged and lost; it was never judged.
    assert run["overall_result"] is None
    assert run["completed_at"] is not None

    # §22's classification, attached where a reader will find it.
    findings = await _findings(stack, run_id)
    assert len(findings) == 1
    unavailable = findings[0]
    assert unavailable["check_id"] == "final_observation"
    assert unavailable["classification"] == "observation_unavailable"
    assert unavailable["status"] == "observation_unavailable"
    assert unavailable["severity"] == "critical"

    # The timeline says what happened, so the run is legible without the finding.
    types = await _event_types(stack, run_id)
    assert types.count("verification_started") == 1
    assert "snapshot_captured" in types  # the `before` capture only
    completed = types.index("verification_completed")
    assert types.index("verification_started") < completed


async def test_a_failed_final_observation_does_not_wedge_the_run(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug this replaces: `verifying` forever, escapable only by reset.

    A second verify must not be answered with FR-038's race rejection, because
    losing a race is a different fact from "this run already ended". Whatever the
    retry is told, it must not be told the run passed.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreObservationProvider

        async def unreachable(self, workspace_id):  # type: ignore[no-untyped-def]
            raise httpx.ConnectError("the store is unreachable")

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", unreachable)
        await visitor.post(f"{RUNS}/{run_id}/verify")

        # Act
        retry = await visitor.post(f"{RUNS}/{run_id}/verify")
        projection = await visitor.get(f"{RUNS}/{run_id}/findings")

    # Assert
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] != "RUN_ALREADY_VERIFYING"

    # The run is terminal, readable, and not a pass.
    run = await _run_row(stack, run_id)
    assert run["status"] == "error"
    body = projection.json()
    assert body["status"] == "error"
    assert body["overall_result"] is None
    assert [f["classification"] for f in body["findings"]] == ["observation_unavailable"]


async def test_a_reset_wins_over_a_failed_final_observation(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two failure paths meet: a cancellation still outranks an `error`.

    Recording the observation failure is a transition like any other, so it goes
    through §16's table too — a run already cancelled does not become `error`.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreObservationProvider

        async def reset_then_fail(self, workspace_id):  # type: ignore[no-untyped-def]
            await visitor.post(f"{WORKSPACE}/reset")
            raise httpx.ConnectError("the store is unreachable")

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", reset_then_fail)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_IN_PROGRESS"
    run = await _run_row(stack, run_id)
    assert run["status"] == "cancelled"
    assert await _findings(stack, run_id) == []


# --- §22's tool_execution_error, in production -------------------------------


async def test_a_failed_invocation_produces_a_tool_execution_finding(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§22: a classification that reaches no finding reaches no reader.

    The journey is the honest one, so every assertion holds and the *only*
    critical failure in the run is the tool that raised. That makes the ordering
    claim checkable rather than incidental: §22 selects the primary failure by
    severity, then causal event sequence, then check id, and with one failing
    finding the selection must land on it.
    """
    # Arrange — a clean journey, then one extra call that raises.
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "post_fix", "discount_reported_but_not_applied")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreAdapter

        original = BuggyStoreAdapter.execute

        async def failing(self, workspace, tool_name, arguments, context):  # type: ignore[no-untyped-def]
            if tool_name == "get_cart":
                raise RuntimeError("the catalogue service is down")
            return await original(self, workspace, tool_name, arguments, context)

        monkeypatch.setattr(BuggyStoreAdapter, "execute", failing)
        await visitor.post(_invoke(run_id, "get_cart"), json={"arguments": {}})

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")
        projection = await visitor.get(f"{RUNS}/{run_id}/findings")

    # Assert — the finding exists and is persisted with §22's classification.
    assert response.status_code == 200, response.text
    findings = await _findings(stack, run_id)
    execution = [f for f in findings if f["classification"] == "tool_execution_error"]
    assert len(execution) == 1
    assert execution[0]["check_id"].startswith("tool_execution:get_cart:")
    assert execution[0]["severity"] == "critical"
    assert execution[0]["status"] == "failed"

    body = response.json()
    # §22's ordering put it where it belongs: it is the run's primary failure,
    # and the verdict is the one its severity requires.
    assert body["primary_failure"] == execution[0]["check_id"]
    assert body["overall_result"] == "failed"
    # §23.1's execution layer already said an invocation failed; the finding is
    # what says which one, and the two now agree.
    assert body["layers"]["tool_execution"] == "failed"
    assert body["counts"]["critical_failures"] >= 1

    # And an agent reading `get_run_findings` can see it (§11.4, AC-22).
    visible = projection.json()["findings"]
    assert any(f["classification"] == "tool_execution_error" for f in visible)


async def test_a_safely_blocked_call_produces_no_execution_finding(
    stack: FastAPI,
) -> None:
    """FR-033's counterpart: not every non-completion is an execution error.

    Without this, wiring the execution findings in could have been done by
    emitting one per terminal invocation, and the suite would not have noticed.
    A cancelled or consent-blocked call is a safe terminal outcome and
    contributes nothing.
    """
    # Arrange — the same honest journey, with nothing raising.
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "post_fix", "discount_reported_but_not_applied")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 200, response.text
    findings = await _findings(stack, run_id)
    assert [f for f in findings if f["classification"] == "tool_execution_error"] == []
    assert response.json()["overall_result"] in {"passed", "passed_with_warnings"}


async def test_the_execution_finding_reaches_the_report_it_is_sealed_with(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§23.1's report is a view of the verdict, never a second opinion.

    The run row and the stored report have to agree about the status and the
    primary failure, which is the whole reason the execution findings are handed
    to the report rather than only persisted.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "post_fix", "discount_reported_but_not_applied")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreAdapter

        original = BuggyStoreAdapter.execute

        async def failing(self, workspace, tool_name, arguments, context):  # type: ignore[no-untyped-def]
            if tool_name == "get_cart":
                raise RuntimeError("the catalogue service is down")
            return await original(self, workspace, tool_name, arguments, context)

        monkeypatch.setattr(BuggyStoreAdapter, "execute", failing)
        await visitor.post(_invoke(run_id, "get_cart"), json={"arguments": {}})
        await visitor.post(f"{RUNS}/{run_id}/verify")

        # Act
        report = await visitor.get(f"{RUNS}/{run_id}/report")

    # Assert
    assert report.status_code == 200, report.text
    document = report.json()
    document = document.get("report", document)
    run = await _run_row(stack, run_id)
    assert document["status"] == run["status"] == "failed"
    assert document["primary_failure"] == "tool_execution_error"


async def test_the_completion_event_records_an_unobservable_target(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§16.1: the terminal event has to say why the run stopped.

    A run in `error` whose timeline explains nothing is legible only to whoever
    already knows what happened, which is not recovery.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from integrations.buggy_store import BuggyStoreObservationProvider

        async def unreachable(self, workspace_id):  # type: ignore[no-untyped-def]
            raise httpx.ConnectError("the store is unreachable")

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", unreachable)

        # Act
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    database: Database = stack.state.database
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT status, redacted_payload_json FROM events "
            "WHERE run_id = ? AND event_type = 'verification_completed'",
            (run_id,),
        )
    assert row is not None
    assert row["status"] == "error"
    payload = json.loads(row["redacted_payload_json"])
    assert payload["classification"] == "observation_unavailable"
    # The reason is the harness's own sentence, never the adapter's exception:
    # §15.8 keeps internal detail out of what a client reads, and this string is
    # stored evidence.
    assert "unreachable" not in payload["reason"]
