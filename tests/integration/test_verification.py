"""005-T6 — verification: capture, evaluate, persist, seal (FR-041, FR-053, §16.1).

The test this whole milestone has been building toward is
`test_the_pre_fix_journey_fails_on_independent_observation`. A tool reports
success, the harness observes the target independently, the two disagree, and
the run fails. Everything before it was scaffolding for that sentence.

Its counterpart matters just as much: `test_the_post_fix_journey_passes` runs
the *same* contract and the *same* calls against an honest target and passes. A
harness that failed both would be useless in a different way, and only the pair
shows the verdict is tracking the target rather than a constant.

The other property under test is that the core owns the verdict. Every check
here comes from `actionwitness_core.engine`, so these tests assert what was
*persisted and sealed* rather than re-deriving a judgement — a test that
recomputed the answer would agree with a broken implementation.
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


async def _scenario(app: FastAPI, workspace_id: str, mode: str, profile: str) -> None:
    """Put the *store* into a scenario. Harness reseeding is T10."""
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


async def _findings(app: FastAPI, run_id: str) -> list[dict]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY check_id", (run_id,)
        )
    return [dict(row) for row in rows]


async def _events(app: FastAPI, run_id: str) -> list[dict]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence_number", (run_id,)
        )
    return [dict(row) for row in rows]


# --- the journey this milestone exists for ----------------------------------


async def test_the_pre_fix_journey_fails_on_independent_observation(
    stack: FastAPI,
) -> None:
    """A syntactically successful tool response, contradicted by observed state.

    The store reports the discount applied; the authoritative read says the cart
    total never moved. The contract asserts the discounted total, so the run
    fails — on the observation, not on the tool's word.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["overall_result"] == "failed"
    assert body["status"] == "failed"
    assert body["findings"]["failed"] >= 1

    # The tool said success on every call, and the verdict disagreed.
    events = await _events(stack, run_id)
    discount = next(
        e for e in events if e["tool_name"] == "apply_discount" and e["reported_status"]
    )
    assert discount["reported_status"] == "success"


async def test_the_post_fix_journey_passes(stack: FastAPI) -> None:
    """The same contract and the same calls against an honest target.

    Without this, "fails in pre_fix" could just mean the harness fails
    everything.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "post_fix", "discount_reported_but_not_applied")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 200
    assert response.json()["overall_result"] in {"passed", "passed_with_warnings"}


async def test_the_failing_assertion_names_the_path_that_did_not_move(
    stack: FastAPI,
) -> None:
    """A verdict a person cannot act on is not much of a verdict."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    failed = [f for f in await _findings(stack, run_id) if f["status"] == "failed"]
    assert failed
    assert any(f["path"] == "target.cart.total" for f in failed)
    for finding in failed:
        # Both sides of the disagreement are recorded, so a reader does not have
        # to re-observe to see what happened.
        assert finding["expected_json"] is not None
        assert finding["actual_json"] is not None


# --- FR-041's capture -------------------------------------------------------


async def test_verification_captures_the_final_snapshot(stack: FastAPI) -> None:
    """FR-041: "immediately before verification"."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT phase, content_hash, redacted_state_json FROM snapshots "
            "WHERE run_id = ? ORDER BY phase",
            (run_id,),
        )
    phases = [row["phase"] for row in rows]
    assert phases == ["after", "before"]
    # The two snapshots are distinct readings, not the same row twice.
    assert rows[0]["content_hash"] != rows[1]["content_hash"]


async def test_the_final_snapshot_is_insert_only(stack: FastAPI) -> None:
    """FR-043, enforced by the schema as well as by there being no update path."""
    # Arrange
    import sqlite3

    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Act / Assert
    with pytest.raises(sqlite3.IntegrityError):
        async with database.transaction() as work:
            await work.execute(
                "INSERT INTO snapshots (id, run_id, phase, provider, namespace, provenance, "
                "schema_version, content_hash, redacted_state_json, created_at) "
                "VALUES ('snap_second', ?, 'after', 'p', 'target', 'x', '1.0', "
                "'sha256:x', '{}', ?)",
                (run_id, work.now()),
            )


# --- §16.1's events ---------------------------------------------------------


async def test_verification_records_its_events_in_order(stack: FastAPI) -> None:
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    types = [e["event_type"] for e in await _events(stack, run_id)]
    assert "verification_started" in types
    assert types[-1] in {"verification_completed", "guidance_transitioned"}
    started = types.index("verification_started")
    completed = types.index("verification_completed")
    assert started < completed
    # The final snapshot is captured between the two, not before the gate.
    assert "snapshot_captured" in types[started:completed]


async def test_one_event_per_check(stack: FastAPI) -> None:
    """§16.1: "one contract assertion produced a result", and one per policy."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    events = await _events(stack, run_id)
    findings = await _findings(stack, run_id)
    per_check = [
        e for e in events if e["event_type"] in {"assertion_evaluated", "policy_evaluated"}
    ]
    assert len(per_check) == len(findings)


async def test_the_completion_event_carries_the_overall_result(stack: FastAPI) -> None:
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    events = await _events(stack, run_id)
    completed = next(e for e in events if e["event_type"] == "verification_completed")
    payload = json.loads(completed["redacted_payload_json"])
    assert payload["overall_result"] == "failed"
    assert payload["primary_failure"]


# --- the terminal transition ------------------------------------------------


async def test_the_run_reaches_a_terminal_state_with_its_result(stack: FastAPI) -> None:
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    async with database.reading() as work:
        run = await work.fetch_one(
            "SELECT status, overall_result, completed_at FROM runs WHERE id = ?", (run_id,)
        )
    assert run["status"] in {"passed", "passed_with_warnings", "failed"}
    assert run["overall_result"] == run["status"]
    assert run["completed_at"] is not None


async def test_sealing_releases_the_workspace(stack: FastAPI) -> None:
    """A terminal run must not keep holding the workspace.

    "Released" is asserted through the things a run locks: the active-run
    pointer clears, and contract selection — refused with `RUN_IN_PROGRESS`
    while the run was live — works again.

    Arming a *second* run is deliberately not the assertion. The journey left a
    mug in the cart, so the canonical contract's preconditions no longer hold
    and arming is refused on its merits. That refusal is checked to be
    `PRECONDITION_FAILED` rather than a lock, which is what distinguishes a
    released workspace from a stuck one.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)
        await visitor.post(f"{RUNS}/{run_id}/verify")

        # Act
        workspace = (await visitor.get(WORKSPACE)).json()
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        other = next(t for t in templates if t["source_template_id"] != CANONICAL)
        reselect = await visitor.post(f"{CONTRACTS}/{other['contract_id']}/select")
        second_arm = await visitor.post(RUNS)

    # Assert
    assert workspace["active_run"] is None
    assert reselect.status_code == 200
    assert second_arm.status_code == 409
    assert second_arm.json()["error"]["code"] == "PRECONDITION_FAILED"


async def test_guidance_follows_the_verdict(stack: FastAPI) -> None:
    """FR-120: the banner and the tool result resolve from one server state."""
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.json()["next_action"]["action_code"] == "review_findings"

    database: Database = stack.state.database
    async with database.reading() as work:
        guidance = await work.fetch_all(
            "SELECT phase FROM guidance_events WHERE run_id = ? ORDER BY workspace_version",
            (run_id,),
        )
    assert guidance[-1]["phase"] == "failed"


# --- the verdict commits as one unit ----------------------------------------


async def test_the_whole_verdict_commits_together(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that recorded findings but never reached a terminal state would be
    a report that disagrees with its own evidence."""
    # Arrange — fail the guidance append, which happens last in the seal.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        from actionwitness_service.application import verification_service as module

        async def explode(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("write failed")

        monkeypatch.setattr(module.GuidanceRecorder, "append", explode)

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 500
    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
        findings = await work.fetch_all("SELECT id FROM findings WHERE run_id = ?", (run_id,))
        snapshots = await work.fetch_all("SELECT phase FROM snapshots WHERE run_id = ?", (run_id,))
    # Nothing from the seal survived: no findings, no `after` snapshot, and the
    # run is still where the gate left it.
    assert findings == []
    assert [row["phase"] for row in snapshots] == ["before"]
    assert run["status"] == "verifying"


async def test_evaluation_uses_the_contract_the_run_was_armed_against(
    stack: FastAPI,
) -> None:
    """FR-025 locks the armed contract.

    The workspace's selection is changed out from under the run — via reset,
    which is the only path that frees it — and the verdict still comes from the
    contract the run holds.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        async with database.reading() as work:
            run = await work.fetch_one("SELECT contract_id FROM runs WHERE id = ?", (run_id,))
        armed_contract = run["contract_id"]

        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        other = next(t for t in templates if t["contract_id"] != armed_contract)
        async with database.transaction() as work:
            await work.execute(
                "UPDATE workspaces SET selected_contract_id = ? WHERE active_run_id = ?",
                (other["contract_id"], run_id),
            )

        # Act
        response = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert — the findings are the armed contract's checks, not the other's.
    assert response.status_code == 200
    findings = await _findings(stack, run_id)
    assert any(f["check_id"] == "mug-quantity" for f in findings)


async def test_verification_is_deterministic_for_the_same_evidence(
    stack: FastAPI,
) -> None:
    """The core decides, so two identical journeys reach the same verdict.

    §24's replay rests on this, and a verdict that drifted between runs would
    make a regression case worthless.
    """
    # Arrange / Act
    results = []
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        for _ in range(2):
            await _scenario(stack, workspace_id, "pre_fix", "discount_reported_but_not_applied")
            run_id = await _arm(visitor)
            await _journey(visitor, run_id)
            body = (await visitor.post(f"{RUNS}/{run_id}/verify")).json()
            results.append((body["overall_result"], body["primary_failure"]))
            await visitor.post(f"{WORKSPACE}/reset")

    # Assert
    assert results[0] == results[1]


async def test_a_second_client_cannot_read_the_findings_it_did_not_produce(
    stack: FastAPI,
) -> None:
    """AC-11 holds through the verdict: findings hang off a run, and a run hangs
    off a workspace."""
    # Arrange
    async with client(stack) as alice, client(stack) as bob:
        run_id = await _arm(alice)
        await _journey(alice, run_id)
        await alice.post(f"{RUNS}/{run_id}/verify")
        await bob.get(WORKSPACE)

        # Act
        response = await bob.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert response.status_code == 404
