"""005-T13 — the exit gate for the Tier 1 outcome-run vertical slice.

The five criteria from `specs/005-run-slice/spec.md`, exercised through the
harness API alone. Nothing here reaches into the store's own surface to set up a
fault or read a result: the point of the milestone is that Journey A works
*through FastAPI*, and a gate that arranged the interesting parts by hand would
pass on a harness that could not.

The criterion the whole product turns on is the first one. A tool reports
success, the discount is not applied, and the harness says so — not because it
inspected the tool's answer more carefully, but because it looked somewhere the
tool does not control. `test_gate_1_the_same_journey_fails_before_and_passes
_after` is that claim, and the `post_fix` half is what stops it passing on a
harness that simply fails everything.

Criterion 3 is recorded here as it actually behaves rather than as the spec's
sentence reads in isolation. See the plan's ledger: because verification is
synchronous, `RUN_ALREADY_VERIFYING` is the answer during the overlap window,
and an action arriving after the seal gets `RUN_TIMELINE_SEALED`. Both are
tested; neither is a partial write.
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
STORE_PREFIX = "/demo/api/v1"
CANONICAL = "one_mug_save20_no_checkout"
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


async def _select_contract(visitor: httpx.AsyncClient) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    assert (await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")).status_code == 200


async def _scenario(visitor: httpx.AsyncClient, mode: str) -> None:
    assert (
        await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    ).status_code == 200
    assert (
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})
    ).status_code == 200


_JOURNEY_A: tuple[tuple[str, dict], ...] = (
    ("search_catalog", {"query": "mug"}),
    ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
    ("apply_discount", {"code": "SAVE20"}),
)


async def _arm(visitor: httpx.AsyncClient, *, source: str | None = None) -> str:
    body = {} if source is None else {"comparison_source_run_id": source}
    armed = await visitor.post(RUNS, json=body)
    assert armed.status_code == 201, armed.text
    return str(armed.json()["run_id"])


async def _journey(visitor: httpx.AsyncClient, run_id: str) -> None:
    """Journey A: find the mug, add it, apply SAVE20. No checkout (§6)."""
    for tool, arguments in _JOURNEY_A:
        response = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
        assert response.status_code == 200, response.text


async def _run_journey_a(
    visitor: httpx.AsyncClient, mode: str, *, source: str | None = None
) -> dict:
    """One complete pass of Journey A in `mode`, returning the verdict."""
    await _scenario(visitor, mode)
    run_id = await _arm(visitor, source=source)
    await _journey(visitor, run_id)
    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
    assert verdict.status_code == 200, verdict.text
    return {"run_id": run_id, **verdict.json()}


def _quantities(cart: dict) -> dict[str, int]:
    """Product id to quantity, from either reader's cart document.

    Keyed on each line's own `product_id` rather than on the mapping key, which
    is a short line slug (`mug`) and not the catalogue identifier.
    """
    return {line["product_id"]: line["quantity"] for line in cart["items"].values()}


async def _classifications(database: Database, run_id: str) -> set[str]:
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT classification FROM findings WHERE run_id = ? AND status = 'failed' "
            "AND classification IS NOT NULL",
            (run_id,),
        )
    return {str(row["classification"]) for row in rows}


# --- criterion 1 -------------------------------------------------------------


async def test_gate_1_the_same_journey_fails_before_and_passes_after(
    stack: FastAPI,
) -> None:
    """ "API-level Journey A fails with `false_success_or_state_mismatch` in
    `pre_fix` and passes in `post_fix`."

    The two halves matter equally. The failure shows the harness catching a
    tool that reported success it did not deliver; the pass shows it is
    reporting the target's behaviour and not its own pessimism. One without the
    other proves nothing.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)

        # Act
        before = await _run_journey_a(visitor, "pre_fix")
        await visitor.post(f"{WORKSPACE}/reset")
        after = await _run_journey_a(visitor, "post_fix", source=before["run_id"])

    # Assert
    assert before["overall_result"] == "failed"
    assert "false_success_or_state_mismatch" in await _classifications(database, before["run_id"])
    assert after["overall_result"] in {"passed", "passed_with_warnings"}
    assert await _classifications(database, after["run_id"]) == set()


async def test_gate_1_the_failing_tool_call_itself_reported_success(
    stack: FastAPI,
) -> None:
    """The premise the criterion rests on, stated separately.

    If `apply_discount` had returned an error, the harness would have caught
    nothing interesting — any client could see that. The finding is only
    meaningful because the tool's own answer was a success.
    """
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        run_id = await _arm(visitor)
        # Asserted, not fired and forgotten: an unnoticed failure here would
        # leave the cart empty, and a discount that changes nothing on an empty
        # cart would satisfy the assertions below for entirely the wrong reason.
        added = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
        )
        assert added.status_code == 200, added.text
        assert added.json()["observed"]["state_changed"] is True

        # Act
        discount = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/apply_discount:invoke",
            json={"arguments": {"code": "SAVE20"}},
        )

    # Assert — the tool says it worked...
    assert discount.status_code == 200
    body = discount.json()
    assert body["reported"]["status"] == "success"
    # ...and the independent read says the state did not move.
    assert body["observed"]["state_changed"] is False


# --- criterion 2 -------------------------------------------------------------


async def test_gate_2_the_report_separates_execution_from_outcome(
    stack: FastAPI,
) -> None:
    """ "The report shows observed trajectory pass, execution pass, business
    outcome fail, and model selection `not_evaluated` for the source run."

    This is the shape of the whole argument: the call was made, it was made
    correctly, it returned successfully — and the business outcome is still
    wrong. A report that collapsed these into one verdict could not say that.
    """
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        source = await _run_journey_a(visitor, "pre_fix")

        # Act
        report = (await visitor.get(f"{RUNS}/{source['run_id']}/report")).json()["report"]

    # Assert
    assert report["layers"]["observed_trajectory"] == "passed"
    assert report["layers"]["tool_execution"] == "passed"
    assert report["layers"]["business_outcome"] == "failed"
    # `not_evaluated` because nothing in Tier 1 judges which tool a model chose;
    # a `passed` here would claim an evaluation that never ran.
    assert report["layers"]["model_tool_selection"] == "not_evaluated"


# --- criterion 3 -------------------------------------------------------------


async def test_gate_3_an_action_overlapping_verification_loses_cleanly(
    stack: FastAPI,
) -> None:
    """ "New target actions lose cleanly to verification with
    `RUN_ALREADY_VERIFYING` and no partial snapshot."

    The two requests are genuinely overlapped rather than ordered, because the
    race is the thing under test. Either may win. What must never happen is
    both winning, or the loser leaving evidence behind.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        run_id = await _arm(visitor)
        await _journey(visitor, run_id)

        # Act
        late, verification = await asyncio.gather(
            visitor.post(
                f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
                json={
                    "arguments": {
                        "product_id": MUG,
                        "quantity": 2,
                        "request_id": "req_late",
                    }
                },
            ),
            visitor.post(f"{RUNS}/{run_id}/verify"),
        )

    # Assert — exactly one winner.
    assert not (late.status_code == 200 and verification.status_code != 200), (
        "an action and the verification that should have excluded it both succeeded"
    )
    if late.status_code != 200:
        assert late.json()["error"]["code"] in {
            "RUN_ALREADY_VERIFYING",
            "RUN_TIMELINE_SEALED",
        }

    # And no partial final snapshot: one row per phase, never two or a fragment.
    async with database.reading() as work:
        phases = await work.fetch_all(
            "SELECT phase, COUNT(*) AS n FROM snapshots WHERE run_id = ? GROUP BY phase",
            (run_id,),
        )
    assert all(int(row["n"]) == 1 for row in phases), "a phase captured twice"


async def test_gate_3_a_rejected_late_action_leaves_no_evidence(
    stack: FastAPI,
) -> None:
    """ "That rejection creates no finding and no evidence" (FR-038).

    A losing action that had written a start event would leave the timeline
    describing a call that never happened, and the trajectory check would then
    judge the run on it.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)
        source = await _run_journey_a(visitor, "pre_fix")
        run_id = source["run_id"]

        async with database.reading() as work:
            before = await work.fetch_one(
                "SELECT COUNT(*) AS n FROM events WHERE run_id = ?", (run_id,)
            )

        # Act — the run is sealed; this action is too late.
        late = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/apply_discount:invoke",
            json={"arguments": {"code": "SAVE20"}},
        )

    # Assert
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "RUN_TIMELINE_SEALED"
    async with database.reading() as work:
        after = await work.fetch_one("SELECT COUNT(*) AS n FROM events WHERE run_id = ?", (run_id,))
    assert before is not None and after is not None
    assert int(after["n"]) == int(before["n"]), "the refusal appended to a sealed timeline"


# --- criterion 4 -------------------------------------------------------------


async def test_gate_4_a_mismatched_rerun_stays_valid_and_is_not_comparable(
    stack: FastAPI,
) -> None:
    """ "A mismatched rerun remains valid but returns `not_comparable` with the
    differing fields."

    Both halves are asserted: the rerun keeps its own verdict and its own
    report, *and* the comparison declines. A rerun that had been refused
    outright would also fail to compare, and that is not what the gate asks
    for.
    """
    # Arrange — a source run, then a rerun against a different contract.
    async with client(stack) as visitor:
        await _select_contract(visitor)
        source = await _run_journey_a(visitor, "pre_fix")

        await visitor.post(f"{WORKSPACE}/reset")
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        other = next(t for t in templates if t["source_template_id"] != CANONICAL)
        assert (await visitor.post(f"{CONTRACTS}/{other['contract_id']}/select")).status_code == 200
        await _scenario(visitor, "post_fix")
        rerun_id = await _arm(visitor, source=source["run_id"])
        await _journey(visitor, rerun_id)
        rerun = await visitor.post(f"{RUNS}/{rerun_id}/verify")

        # Act
        comparison = await visitor.get(f"{RUNS}/{rerun_id}/comparison")
        report = await visitor.get(f"{RUNS}/{rerun_id}/report")

    # Assert — the rerun is an ordinary, complete run...
    assert rerun.status_code == 200
    assert report.status_code == 200
    assert rerun.json()["overall_result"] in {"passed", "passed_with_warnings", "failed"}

    # ...that simply cannot be read as the other one's counterpart.
    assert comparison.status_code == 200
    body = comparison.json()
    assert body["comparable"] is False
    assert body["differing_fields"], "a refusal must name what differed"
    assert "contract_content_hash" in body["differing_fields"]


# --- criterion 5: AC-03, AC-04, AC-11, AC-19, and the API portion of AC-20 ---


async def test_gate_5_ac_03_the_human_view_and_the_observation_agree(
    stack: FastAPI,
) -> None:
    """AC-03: "the target's human-facing UI and the adapter's independently
    captured canonical observation show the same legitimate mutation."

    Two genuinely different readers of one target: the store's own cart
    endpoint — what a person looking at the page would see — and the adapter's
    observation provider. A legitimate mutation must appear in both, or the
    harness and the human are looking at different systems.
    """
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "post_fix")
        run_id = await _arm(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

        # Act — one legitimate mutation through the agent surface.
        added = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 2, "request_id": "req_twomugs"}},
        )
        assert added.status_code == 200, added.text
        # HTTP 200 only means the harness handled the call. Whether the *tool*
        # succeeded is a separate claim, and this test is worthless if the
        # mutation it is checking for never happened.
        assert added.json()["reported"]["status"] == "success", added.text
        assert added.json()["observed"]["state_changed"] is True, added.text

    # Assert — the human-facing read...
    human = await stack.state.store_client.get(
        f"{STORE_PREFIX}/store/cart", headers={"X-Workspace-Id": workspace_id}
    )
    assert human.status_code == 200
    # §13.2 keys `items` by product id, so quantity is what the two readers must
    # agree on rather than list order.
    human_items = _quantities(human.json()["cart"])

    # ...and the adapter's independent canonical observation.
    adapter = stack.state.adapters.adapter("buggy_store")
    observed = await adapter.observation_provider().capture(workspace_id)
    observed_items = _quantities(dict(observed.payload)["cart"])

    assert human_items == observed_items
    assert human_items[MUG] == 2


async def test_gate_5_ac_04_execution_passes_while_the_outcome_fails(
    stack: FastAPI,
) -> None:
    """AC-04, in its own words: "tool execution passes but business outcome
    fails with `false_success_or_state_mismatch`"."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)

        # Act
        verdict = await _run_journey_a(visitor, "pre_fix")

    # Assert
    assert verdict["layers"]["tool_execution"] == "passed"
    assert verdict["layers"]["business_outcome"] == "failed"
    assert "false_success_or_state_mismatch" in await _classifications(database, verdict["run_id"])


async def test_gate_5_ac_11_two_visitors_share_nothing(stack: FastAPI) -> None:
    """AC-11: "each session sees only its own contract, cart, runs ... events,
    and artifacts."

    Two clients, because a single-client test proves a route works rather than
    that a second client is locked out. Each resource is probed with the *other*
    workspace's real identifier, which is the case a scoping bug actually
    produces — an unguessable id is not a boundary.
    """
    # Arrange — two independent visitors, each with a completed run.
    async with client(stack) as first, client(stack) as second:
        await _select_contract(first)
        one = await _run_journey_a(first, "pre_fix")

        await _select_contract(second)
        two = await _run_journey_a(second, "post_fix")

        first_workspace = (await first.get(WORKSPACE)).json()["workspace_id"]
        second_workspace = (await second.get(WORKSPACE)).json()["workspace_id"]

        # Act / Assert — each visitor's own run is theirs...
        assert (await first.get(f"{RUNS}/{one['run_id']}/report")).status_code == 200
        assert (await second.get(f"{RUNS}/{two['run_id']}/report")).status_code == 200

        # ...and the other's is not, by any route.
        for path in (
            f"{RUNS}/{two['run_id']}/report",
            f"{RUNS}/{two['run_id']}/events",
            f"{RUNS}/{two['run_id']}/comparison",
        ):
            crossed = await first.get(path)
            assert crossed.status_code == 404, path
            assert crossed.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        # A mutation aimed at the other's run is refused too, not just a read.
        mutation = await first.post(
            f"{RUNS}/{two['run_id']}/target-tools/apply_discount:invoke",
            json={"arguments": {"code": "SAVE20"}},
        )

    assert mutation.status_code == 404
    assert first_workspace != second_workspace
    # Their verdicts differ, which is only possible if their target state did.
    assert one["overall_result"] == "failed"
    assert two["overall_result"] in {"passed", "passed_with_warnings"}


async def test_gate_5_ac_20_two_immutable_runs_differ_in_one_variable(
    stack: FastAPI,
) -> None:
    """The API portion of AC-20: "creates two immutable runs ... keeps target
    adapter, contract hash, fixture hash, intent hash ... and replayed
    trajectory ... equal ... reports the expected failure before and pass after
    ... displays a matched comparison with the original critical classification
    resolved."

    The panel, the disabled-choice copy, and the "demo implementation profile"
    labelling are UI and belong to 006; everything above is API and is here.
    """
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        before = await _run_journey_a(visitor, "pre_fix")
        await visitor.post(f"{WORKSPACE}/reset")
        after = await _run_journey_a(visitor, "post_fix", source=before["run_id"])

        # Act
        comparison = (await visitor.get(f"{RUNS}/{after['run_id']}/comparison")).json()

    # Assert — one variable moved...
    assert comparison["comparable"] is True
    assert comparison["source"]["scenario_mode"] == "pre_fix"
    assert comparison["candidate"]["scenario_mode"] == "post_fix"
    assert (
        comparison["source"]["comparison_key_hash"]
        == (comparison["candidate"]["comparison_key_hash"])
    )
    # ...the expected failure before and pass after...
    assert comparison["source"]["overall_result"] == "failed"
    assert comparison["candidate"]["overall_result"] in {"passed", "passed_with_warnings"}
    # ...and the original critical classification resolved.
    assert "false_success_or_state_mismatch" in comparison["resolved_classifications"]
    assert comparison["introduced_classifications"] == []


async def test_gate_5_ac_20_a_completed_runs_scenario_never_changes(
    stack: FastAPI,
) -> None:
    """AC-20: "never changes the active scenario of an in-progress or completed
    run".

    The second run selects `post_fix` in the same workspace. If the scenario
    were stored per workspace and read back per run, the first run's record
    would silently follow — and the matched comparison would then be comparing
    a run against a relabelled version of itself.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)
        before = await _run_journey_a(visitor, "pre_fix")

        async with database.reading() as work:
            recorded = await work.fetch_one(
                "SELECT scenario_mode, failure_profile FROM runs WHERE id = ?",
                (before["run_id"],),
            )

        # Act — reconfigure the workspace and run again.
        await visitor.post(f"{WORKSPACE}/reset")
        await _run_journey_a(visitor, "post_fix", source=before["run_id"])

    # Assert — the completed run is untouched.
    async with database.reading() as work:
        still = await work.fetch_one(
            "SELECT scenario_mode, failure_profile, status FROM runs WHERE id = ?",
            (before["run_id"],),
        )
    assert recorded is not None and still is not None
    assert still["scenario_mode"] == recorded["scenario_mode"] == "pre_fix"
    assert still["failure_profile"] == recorded["failure_profile"] == FAULT


async def test_gate_5_ac_20_the_fault_activation_field_is_not_yet_recorded(
    stack: FastAPI,
) -> None:
    """AC-20's "activates the fault only for the `pre_fix` run" — the known gap.

    **This test records what the system does, not what it should do.** The
    behavioural half of the bullet holds: the fault really is active only in
    `pre_fix`, which is why the two runs reach opposite verdicts, and that is
    asserted below so this cannot be read as "the scenario does nothing".

    What is missing is the recorded *field*. `runs.fault_active` is never
    populated: §23.1 says it is "derived by the adapter", no protocol method
    reports it, and deriving it from the mode name is what §9.1 forbids. The 005
    plan carries it as an open operator decision.

    Written as an ordinary assertion rather than an `xfail`, because the lane
    gate forbids quarantined failures and weakening a gate to admit one is an
    escalation, not a fix. The effect is the same: implementing `fault_active`
    breaks this test, which is the prompt to delete it and restore AC-20's
    bullet in the traceability map.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)
        before = await _run_journey_a(visitor, "pre_fix")
        await visitor.post(f"{WORKSPACE}/reset")
        after = await _run_journey_a(visitor, "post_fix", source=before["run_id"])

    # Assert — the fault is behaviourally active only before...
    assert before["overall_result"] == "failed"
    assert after["overall_result"] in {"passed", "passed_with_warnings"}

    # ...and is recorded on neither run, which is the gap.
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT id, fault_active FROM runs WHERE id IN (?, ?)",
            (before["run_id"], after["run_id"]),
        )
    recorded = {str(row["id"]): bool(row["fault_active"]) for row in rows}
    assert recorded == {before["run_id"]: False, after["run_id"]: False}, (
        "`fault_active` is now populated — AC-20's bullet can be asserted "
        "properly; delete this characterization test and update the 005 "
        "traceability map"
    )
