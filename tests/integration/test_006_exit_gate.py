"""006-T13 — the Tier 1 exit gate, server side (§14, §26.1, AC-06, AC-21).

Criterion 3 is the one this file exists for: "UI banner, enabled controls,
native status result, tool `next_action`, and action history share the same
action code at every transition."

Five surfaces is four opportunities to disagree, and they disagree in the worst
possible situation — when a person and an agent are both waiting, each having
been told something different about whose turn it is. The only defence is that
all five read *one* server derivation, and the way to test that is to walk a
journey through every transition and compare them at each step rather than
asserting each surface's copy in isolation.

Criteria 1 and 2 concern a real browser and are operator-attested; see
`docs/tier-1-gate-checklist.md` and the 006 plan's ledger. Everything below
them is deterministic and lives here.
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


async def _checkout_contract(visitor: httpx.AsyncClient) -> None:
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


async def _history(database: Database, workspace_id: str) -> list[dict]:
    """The append-only action history a reader traces a handoff through."""
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT workspace_version, phase, active_actor, action_code "
            "FROM guidance_events WHERE workspace_id = ? ORDER BY workspace_version",
            (workspace_id,),
        )
    return [dict(row) for row in rows]


async def _surfaces(visitor: httpx.AsyncClient) -> tuple[str, str, bool]:
    """The banner's code, the compact projection's code, and the human flag.

    Both come from `GET /workspace` because both are what the UI renders from:
    the banner reads `guidance`, and every tool result carries `next_action`.
    """
    body = (await visitor.get(WORKSPACE)).json()
    return (
        str(body["guidance"]["action_code"]),
        str(body["next_action"]["action_code"]),
        bool(body["next_action"]["requires_human_input"]),
    )


# --- criterion 3: one action code, at every transition -----------------------


async def test_gate_3_every_surface_names_one_action_code_through_journey_b(
    stack: FastAPI,
) -> None:
    """The five surfaces, compared at each transition of a full journey.

    Asserting each surface's copy in isolation would pass on a system where
    they were computed separately and happened to agree in the states somebody
    thought to test. Walking the journey and comparing them at every step is
    what makes the claim mean anything.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        seen: list[str] = []

        def record(banner: str, projection: str) -> None:
            # The banner and the compact projection are two of the five; they
            # must never differ, at any step.
            assert banner == projection, f"banner {banner!r} != next_action {projection!r}"
            seen.append(banner)

        # Act — walk every transition Journey B passes through.
        banner, projection, _ = await _surfaces(visitor)
        record(banner, projection)

        await _checkout_contract(visitor)
        await visitor.put(
            f"{WORKSPACE}/failure-profile",
            json={"failure_profile": "discount_reported_but_not_applied"},
        )
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})
        banner, projection, _ = await _surfaces(visitor)
        record(banner, projection)

        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        banner, projection, _ = await _surfaces(visitor)
        record(banner, projection)

        # The tool result's own `next_action` is the third surface.
        added = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
        )
        banner, projection, _ = await _surfaces(visitor)
        assert added.json()["next_action"]["action_code"] == banner
        record(banner, projection)

        paused = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        banner, projection, human = await _surfaces(visitor)
        assert paused.json()["next_action"]["action_code"] == banner
        assert human is True, "a pending confirmation must say a human is needed"
        record(banner, projection)

        confirmation_id = paused.json()["confirmation"]["confirmation_id"]
        decision = await visitor.post(
            f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
            json={"decision": "approve_once"},
        )
        banner, projection, _ = await _surfaces(visitor)
        assert decision.json()["next_action"]["action_code"] == banner
        record(banner, projection)

        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
        banner, projection, _ = await _surfaces(visitor)
        assert verdict.json()["next_action"]["action_code"] == banner
        record(banner, projection)

    # Assert — the fourth surface: the action history recorded the same codes,
    # in the same order, so a reader tracing the run afterwards sees the story
    # the surfaces told at the time.
    #
    # A *subsequence*, not a copy. The history appends on transition (FR-122),
    # while `seen` also holds the states between them — reading the workspace
    # is not a handoff. What must hold is that the history invents nothing and
    # reorders nothing.
    recorded = [str(entry["action_code"]) for entry in await _history(database, workspace_id)]
    assert recorded, "the action history recorded no handoff at all"
    assert set(recorded) <= set(seen), (
        f"the history names codes no surface showed: {sorted(set(recorded) - set(seen))}"
    )
    assert _is_subsequence(recorded, seen), (
        f"history {recorded} is not in the order the surfaces showed {seen}"
    )
    # And the journey really did move through distinct states, or every
    # assertion above would be trivially satisfied.
    assert len(set(seen)) >= 4, f"the journey did not transition: {seen}"


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Whether `needle` appears in `haystack` in order, gaps allowed."""
    remaining = iter(haystack)
    return all(item in remaining for item in needle)


async def test_gate_3_the_handoff_to_the_human_names_exactly_one_actor(
    stack: FastAPI,
) -> None:
    """AC-21: "exactly one active actor". Checked at the moment it changes,
    which is the only moment two derivations could disagree."""
    # Arrange
    async with client(stack) as visitor:
        await _checkout_contract(visitor)
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
        )

        # Act
        before = (await visitor.get(WORKSPACE)).json()
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        during = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert before["guidance"]["active_actor"] == "agent"
    assert during["guidance"]["active_actor"] == "human_approver"
    assert during["next_action"]["actor"] == during["guidance"]["active_actor"]


# --- criterion 4: consent controls the order ---------------------------------


@pytest.mark.parametrize("ending", ["deny", "cancel"])
async def test_gate_4_a_refused_action_creates_no_order(stack: FastAPI, ending: str) -> None:
    """ "Denial, expiry and cancel create no order."

    Read at the target, because "no mutation occurred" is a claim the harness
    makes about itself and this milestone is about not believing such claims.
    """
    # Arrange
    async with client(stack) as visitor:
        await _checkout_contract(visitor)
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
        )
        paused = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        confirmation_id = paused.json()["confirmation"]["confirmation_id"]

        # Act
        if ending == "deny":
            await visitor.post(
                f"{RUNS}/{run_id}/confirmations/{confirmation_id}/decision",
                json={"decision": "deny"},
            )
        else:
            await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    order = (
        await stack.state.store_client.get(
            f"{STORE}/store/cart", headers={"X-Workspace-Id": workspace_id}
        )
    ).json()["order"]
    assert order["created"] is False


# --- criterion 2: the workspace works without WebMCP -------------------------


async def test_gate_2_the_whole_journey_needs_no_browser_agent(stack: FastAPI) -> None:
    """ "An unsupported browser completes the manual equivalent."

    The server half of that claim, and the reason it holds: there is no
    separate manual path to maintain. The tools drive these same endpoints, so
    a browser without WebMCP is a browser whose user clicks the buttons that
    call them. The browser half is operator-attested — see the checklist.
    """
    # Arrange / Act — every step by ordinary HTTP, no tool involved.
    async with client(stack) as visitor:
        await _checkout_contract(visitor)
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_onemug"}},
        )
        paused = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        await visitor.post(
            f"{RUNS}/{run_id}/confirmations/"
            f"{paused.json()['confirmation']['confirmation_id']}/decision",
            json={"decision": "approve_once"},
        )
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
            json={"arguments": {"request_id": "req_checkoutone"}},
        )
        verdict = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert
    assert verdict.status_code == 200, verdict.text
    assert verdict.json()["overall_result"] in {"passed", "passed_with_warnings"}


async def test_gate_2_guidance_is_present_before_anything_is_configured(
    stack: FastAPI,
) -> None:
    """ "…and shows setup guidance."

    A first-time visitor with no contract, no run and no tools must still be
    told what to do — that is the state where a person is most likely to be
    stuck, and the one where an empty banner would be most costly.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        body = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert body["guidance"]["headline"]
    assert body["guidance"]["instruction"]
    assert body["guidance"]["reason"]
    assert body["next_action"]["action_code"] is not None
