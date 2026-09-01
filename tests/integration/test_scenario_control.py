"""005-T10 — scenario selection and reset reach the target (FR-011, FR-013, §9.1).

Until this task the harness recorded a scenario selection and never told the
target. Every earlier test that needed a genuinely faulty store drove the
store's own `/demo` surface directly and said so — honest, but it meant the
product's own control surface did not work.

So the test that matters is
`test_selecting_pre_fix_through_the_harness_makes_the_target_lie`: the whole
journey now runs through `/api/v1` alone, and the discount fault appears because
the harness put the target into `pre_fix`.

The other property is ordering. **Preparation precedes persistence** on the
selection routes, because a workspace whose column says `pre_fix` while the
target is still honest would arm a run whose evidence is labelled with a
scenario nobody selected. **Cancellation precedes reseeding** on reset, because
FR-013 says so and because a reseed that ran first would wipe the state the
cancelled run's evidence describes.
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


async def _choose_scenario(visitor: httpx.AsyncClient, mode: str) -> httpx.Response:
    profile = await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    assert profile.status_code == 200, profile.text
    return await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})


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


async def _observed_cart(app: FastAPI, workspace_id: str) -> dict:
    """Read the target's canonical state through the adapter's own provider."""
    adapter = app.state.adapters.adapter("buggy_store")
    observation = await adapter.observation_provider().capture(workspace_id)
    return dict(observation.payload)["cart"]


# --- the whole journey through the harness alone ----------------------------


async def test_selecting_pre_fix_through_the_harness_makes_the_target_lie(
    stack: FastAPI,
) -> None:
    """The product's own control surface, end to end.

    No test helper touches the store. The harness selects the scenario, arms,
    drives the tools, and the discount fault appears — which it could not have
    done before this task, because the target was never told.
    """
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        assert (await _choose_scenario(visitor, "pre_fix")).status_code == 200
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        await _journey(visitor, run_id)

        # Act
        verdict = (await visitor.post(f"{RUNS}/{run_id}/verify")).json()

    # Assert
    assert verdict["overall_result"] == "failed"
    assert verdict["layers"]["tool_execution"] == "passed"
    assert verdict["layers"]["business_outcome"] == "failed"


async def test_selecting_post_fix_through_the_harness_makes_the_target_honest(
    stack: FastAPI,
) -> None:
    """The matched pair, both halves now driven by the harness.

    FR-011 keeps the same profile recorded in `post_fix` and lets the adapter
    disable it, so the two runs differ in exactly one variable.
    """
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        assert (await _choose_scenario(visitor, "post_fix")).status_code == 200
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        await _journey(visitor, run_id)

        # Act
        verdict = (await visitor.post(f"{RUNS}/{run_id}/verify")).json()
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert verdict["overall_result"] in {"passed", "passed_with_warnings"}
    # The profile is still recorded — it is the comparison fault, disabled.
    assert workspace["failure_profile"] == FAULT
    assert workspace["scenario_mode"] == "post_fix"


# --- selection reaches the target -------------------------------------------


async def test_a_selection_reports_that_the_target_was_reseeded(
    stack: FastAPI,
) -> None:
    """ "When supported" is answered rather than assumed."""
    # Arrange / Act
    async with client(stack) as visitor:
        await _select_contract(visitor)
        response = await _choose_scenario(visitor, "pre_fix")

    # Assert
    body = response.json()
    assert body["scenario_mode"] == "pre_fix"
    assert body["target_reseeded"] is True
    assert "pre_fix" in body["reseed_detail"]


async def test_selecting_a_scenario_reseeds_mutable_target_state(
    stack: FastAPI,
) -> None:
    """The store reseeds on scenario selection (003), and the harness now
    triggers it — so a cart left over from earlier work does not survive into
    the next configuration."""
    # Arrange — put something in the cart through the harness.
    async with client(stack) as visitor:
        await _select_contract(visitor)
        assert (await _choose_scenario(visitor, "post_fix")).status_code == 200
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        await _journey(visitor, run_id)
        assert (await _observed_cart(stack, workspace_id))["items"]

        # Act — a fresh scenario selection.
        await visitor.post(f"{WORKSPACE}/reset")
        assert (await _choose_scenario(visitor, "pre_fix")).status_code == 200

        # Assert
        assert (await _observed_cart(stack, workspace_id))["items"] == {}


async def test_a_selection_the_adapter_refuses_records_nothing(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preparation precedes persistence.

    A workspace whose column said `pre_fix` while the target was still honest
    would arm a run whose evidence carries a scenario label that is not true.
    """
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        before = (await visitor.get(WORKSPACE)).json()

        from integrations.buggy_store import BuggyStoreAdapter

        async def refuse(self, workspace_id, fixture, scenario):  # type: ignore[no-untyped-def]
            raise RuntimeError("the store refused the scenario")

        monkeypatch.setattr(BuggyStoreAdapter, "prepare", refuse)

        # Act
        response = await visitor.put(
            f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"}
        )
        after = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TARGET_UNAVAILABLE"
    assert after["scenario_mode"] == before["scenario_mode"] is None


async def test_an_unadvertised_mode_never_reaches_the_target(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9.1: validated against the descriptor first.

    The adapter would refuse it too, but a refusal that arrives *after* a reseed
    attempt is harder to reason about than one that arrives instead of it.
    """
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)

        from integrations.buggy_store import BuggyStoreAdapter

        attempted = {"count": 0}
        original = BuggyStoreAdapter.prepare

        async def counting(self, workspace_id, fixture, scenario):  # type: ignore[no-untyped-def]
            attempted["count"] += 1
            return await original(self, workspace_id, fixture, scenario)

        monkeypatch.setattr(BuggyStoreAdapter, "prepare", counting)

        # Act
        response = await visitor.put(
            f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "external_current"}
        )

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"
    assert attempted["count"] == 0


# --- FR-013's reseed on reset -----------------------------------------------


async def test_reset_reseeds_the_target(stack: FastAPI) -> None:
    """FR-013: "reseed managed-target state through the adapter when supported"."""
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        assert (await _choose_scenario(visitor, "pre_fix")).status_code == 200
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        await _journey(visitor, run_id)
        assert (await _observed_cart(stack, workspace_id))["items"]

        # Act
        response = await visitor.post(f"{WORKSPACE}/reset")

        # Assert
        assert response.json()["target_reseeded"] is True
        assert (await _observed_cart(stack, workspace_id))["items"] == {}


async def test_reset_cancels_before_it_reseeds(stack: FastAPI) -> None:
    """FR-013's order. A reseed that ran first would wipe the state the
    cancelled run's evidence describes — so the cancellation event is recorded
    against a run whose observations still meant something."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)
        assert (await _choose_scenario(visitor, "pre_fix")).status_code == 200
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        await _journey(visitor, run_id)

        # Act
        body = (await visitor.post(f"{WORKSPACE}/reset")).json()

    # Assert
    assert body["runs_cancelled"] == 1
    assert body["target_reseeded"] is True
    async with database.reading() as work:
        run = await work.fetch_one("SELECT status FROM runs WHERE id = ?", (run_id,))
        events = await work.fetch_all(
            "SELECT event_type FROM events WHERE run_id = ? ORDER BY sequence_number",
            (run_id,),
        )
    assert run["status"] == "cancelled"
    assert "run_cancelled" in [row["event_type"] for row in events]


async def test_reset_without_a_scenario_skips_the_reseed_and_says_so(
    stack: FastAPI,
) -> None:
    """ "When supported". A workspace that has chosen nothing has nothing to
    prepare, and a silent no-op would leave the caller guessing."""
    # Arrange / Act
    async with client(stack) as visitor:
        await visitor.get(WORKSPACE)
        body = (await visitor.post(f"{WORKSPACE}/reset")).json()

    # Assert
    assert body["target_reseeded"] is False
    assert "no target" in body["reseed_detail"] or "no scenario" in body["reseed_detail"]


async def test_reset_with_the_target_disabled_still_resets_the_workspace(
    tmp_path: Path,
) -> None:
    """§21.1: an absent optional target is a bounded state, so reset still does
    the half it can."""
    # Arrange
    seeding = create_app(
        environ={"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"},
        database_path=tmp_path / "harness.sqlite3",
    )
    async with seeding.router.lifespan_context(seeding):
        pass

    harness = create_app(
        environ={"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"},
        database_path=tmp_path / "harness.sqlite3",
    )
    async with harness.router.lifespan_context(harness), client(harness) as visitor:
        # Act
        response = await visitor.post(f"{WORKSPACE}/reset")

    # Assert
    assert response.status_code == 200
    assert response.json()["target_reseeded"] is False


# --- isolation ---------------------------------------------------------------


async def test_one_workspaces_scenario_does_not_reach_another(
    stack: FastAPI,
) -> None:
    """The adapter is told which workspace to prepare, and the store keeps them
    apart (003). A shared target would make two visitors' runs meaningless."""
    # Arrange
    async with client(stack) as alice, client(stack) as bob:
        await _select_contract(alice)
        await _select_contract(bob)
        assert (await _choose_scenario(alice, "pre_fix")).status_code == 200
        assert (await _choose_scenario(bob, "post_fix")).status_code == 200

        alice_run = (await alice.post(RUNS)).json()["run_id"]
        bob_run = (await bob.post(RUNS)).json()["run_id"]
        await _journey(alice, alice_run)
        await _journey(bob, bob_run)

        # Act
        alice_verdict = (await alice.post(f"{RUNS}/{alice_run}/verify")).json()
        bob_verdict = (await bob.post(f"{RUNS}/{bob_run}/verify")).json()

    # Assert — the same journey, opposite outcomes, decided by each workspace's
    # own scenario.
    assert alice_verdict["overall_result"] == "failed"
    assert bob_verdict["overall_result"] in {"passed", "passed_with_warnings"}
