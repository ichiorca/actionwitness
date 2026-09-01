"""005-T11 — matched pre/post comparison (FR-019, §23.7, §17.1).

FR-019 links a pair "only when target adapter, contract, fixture, normalized
intent, scalar parameters, and actual tool trajectory ... match **while scenario
modes differ**". That is a controlled experiment: exactly one variable moves, and
if anything else moved the two runs are not evidence about the scenario — they
are two different experiments.

The exit-gate sentence is the one about mismatches: "a mismatched rerun remains
valid but returns `not_comparable` with the differing fields." So a mismatch is a
**200 with `comparable: false`**, not an error. Returning a failure would push
somebody to make the pair match by weakening what they meant to test, which is
the opposite of what an assurance harness is for.

Every mismatch route below gets its own test, because a comparison that returned
`not_comparable` for everything would pass the headline test and be useless.
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


async def _select_contract(visitor: httpx.AsyncClient, template: str = CANONICAL) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == template)
    assert (await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")).status_code == 200


async def _scenario(visitor: httpx.AsyncClient, mode: str) -> None:
    assert (
        await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    ).status_code == 200
    assert (
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})
    ).status_code == 200


async def _run(
    visitor: httpx.AsyncClient,
    *,
    source: str | None = None,
    calls: tuple[tuple[str, dict], ...] | None = None,
) -> str:
    """Arm, drive the journey, verify — and return the run id."""
    body: dict = {}
    if source is not None:
        body["comparison_source_run_id"] = source
    armed = await visitor.post(RUNS, json=body)
    assert armed.status_code == 201, armed.text
    run_id = str(armed.json()["run_id"])

    for tool, arguments in calls or _JOURNEY:
        response = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
        assert response.status_code == 200, response.text

    verified = await visitor.post(f"{RUNS}/{run_id}/verify")
    assert verified.status_code == 200, verified.text
    return run_id


_JOURNEY: tuple[tuple[str, dict], ...] = (
    ("search_catalog", {"query": "mug"}),
    ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
    ("apply_discount", {"code": "SAVE20"}),
)


async def _matched_pair(visitor: httpx.AsyncClient) -> tuple[str, str]:
    """A `pre_fix` run and the `post_fix` rerun bound to it."""
    await _select_contract(visitor)
    await _scenario(visitor, "pre_fix")
    pre = await _run(visitor)

    await visitor.post(f"{WORKSPACE}/reset")
    await _scenario(visitor, "post_fix")
    post = await _run(visitor, source=pre)
    return pre, post


# --- the matched pair --------------------------------------------------------


async def test_a_pre_post_pair_with_one_variable_moved_is_comparable(
    stack: FastAPI,
) -> None:
    """The controlled experiment, run through the harness alone."""
    # Arrange
    async with client(stack) as visitor:
        pre, post = await _matched_pair(visitor)

        # Act
        response = await visitor.get(f"{RUNS}/{post}/comparison")

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["comparable"] is True
    assert body["source"]["run_id"] == pre
    assert body["candidate"]["run_id"] == post
    assert body["source"]["scenario_mode"] == "pre_fix"
    assert body["candidate"]["scenario_mode"] == "post_fix"


async def test_the_pair_reports_whether_the_critical_classification_disappeared(
    stack: FastAPI,
) -> None:
    """§23.7: "whether the original critical classification disappeared".

    This is what the pair is *for*: the pre-fix run failed with
    `false_success_or_state_mismatch`, the post-fix run did not, and the
    comparison says so rather than leaving a reader to diff two reports.
    """
    # Arrange
    async with client(stack) as visitor:
        _pre, post = await _matched_pair(visitor)

        # Act
        body = (await visitor.get(f"{RUNS}/{post}/comparison")).json()

    # Assert
    assert body["source"]["overall_result"] == "failed"
    assert body["candidate"]["overall_result"] in {"passed", "passed_with_warnings"}
    assert "false_success_or_state_mismatch" in body["resolved_classifications"]
    assert body["introduced_classifications"] == []


async def test_both_sides_share_a_comparison_key(stack: FastAPI) -> None:
    """§17.1: the key hashes "every controlled input except scenario mode and
    derived fault activation", so a matched pair carries the same one."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        pre, post = await _matched_pair(visitor)
        body = (await visitor.get(f"{RUNS}/{post}/comparison")).json()

    # Assert — reported, and stored.
    assert body["source"]["comparison_key_hash"] == body["candidate"]["comparison_key_hash"]
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT id, comparison_key_hash FROM runs WHERE id IN (?, ?)", (pre, post)
        )
    keys = {row["id"]: row["comparison_key_hash"] for row in rows}
    assert keys[pre] == keys[post]
    assert keys[pre].startswith("sha256:")


async def test_the_key_is_null_until_the_run_terminates(stack: FastAPI) -> None:
    """§17.1: "nullable until the run is terminal". A key on an unfinished run
    would invite a comparison against a moving target."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        run_id = (await visitor.post(RUNS)).json()["run_id"]

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one("SELECT comparison_key_hash FROM runs WHERE id = ?", (run_id,))
    assert row["comparison_key_hash"] is None


# --- every way a pair fails to match ----------------------------------------


async def test_the_same_scenario_mode_twice_is_not_a_comparison(
    stack: FastAPI,
) -> None:
    """Two runs in the same mode are a repetition, and calling them a pre/post
    pair would claim an experiment nobody ran."""
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        first = await _run(visitor)
        await visitor.post(f"{WORKSPACE}/reset")
        await _scenario(visitor, "pre_fix")
        second = await _run(visitor, source=first)

        # Act
        body = (await visitor.get(f"{RUNS}/{second}/comparison")).json()

    # Assert
    assert body["comparable"] is False
    assert "scenario_mode" in body["differing_fields"]
    assert "nothing was varied" in body["reason"]


async def test_a_different_contract_is_not_comparable(stack: FastAPI) -> None:
    """A controlled input moved, so the two runs are different experiments."""
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        first = await _run(visitor)

        await visitor.post(f"{WORKSPACE}/reset")
        await _select_contract(visitor, "confirmed_checkout_only")
        await _scenario(visitor, "post_fix")
        second = await _run(visitor, source=first, calls=(("get_cart", {}),))

        # Act
        body = (await visitor.get(f"{RUNS}/{second}/comparison")).json()

    # Assert
    assert body["comparable"] is False
    assert "contract_content_hash" in body["differing_fields"]
    assert "intent_content_hash" in body["differing_fields"]
    assert "different experiments" in body["reason"]


async def test_a_different_trajectory_is_not_comparable(stack: FastAPI) -> None:
    """FR-019's "actual tool trajectory". The agent did something different, so
    the two runs exercised different paths — reported separately from a
    configuration change, because those need different responses."""
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        first = await _run(visitor)

        await visitor.post(f"{WORKSPACE}/reset")
        await _scenario(visitor, "post_fix")
        second = await _run(
            visitor,
            source=first,
            calls=(
                ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_only1"}),
                ("apply_discount", {"code": "SAVE20"}),
            ),
        )

        # Act
        body = (await visitor.get(f"{RUNS}/{second}/comparison")).json()

    # Assert — only the trajectory moved; the configuration is unchanged.
    assert body["comparable"] is False
    assert body["differing_fields"] == ["trajectory"]
    assert "different paths" in body["reason"]


async def test_a_mismatched_rerun_is_still_a_valid_run(stack: FastAPI) -> None:
    """The exit-gate sentence. `not_comparable` is a label on the *pair*, not a
    judgement on the rerun — which keeps its own verdict and its own report."""
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        first = await _run(visitor)
        await visitor.post(f"{WORKSPACE}/reset")
        await _scenario(visitor, "pre_fix")
        second = await _run(visitor, source=first)

        # Act
        comparison = await visitor.get(f"{RUNS}/{second}/comparison")

    # Assert — a 200, and the rerun reached its own terminal verdict.
    assert comparison.status_code == 200
    assert comparison.json()["comparable"] is False

    database: Database = stack.state.database
    async with database.reading() as work:
        run = await work.fetch_one(
            "SELECT status, overall_result FROM runs WHERE id = ?", (second,)
        )
        artifact = await work.fetch_one("SELECT id FROM artifacts WHERE run_id = ?", (second,))
    assert run["status"] == "failed"
    assert run["overall_result"] == "failed"
    assert artifact is not None


async def test_a_mismatch_still_shows_both_sides(stack: FastAPI) -> None:
    """§23.7 lists the differing fields *and* shows the pair, so a reader can
    see what to change to make the next rerun comparable."""
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        first = await _run(visitor)
        await visitor.post(f"{WORKSPACE}/reset")
        await _scenario(visitor, "pre_fix")
        second = await _run(visitor, source=first)

        # Act
        body = (await visitor.get(f"{RUNS}/{second}/comparison")).json()

    # Assert
    assert body["source"]["run_id"] == first
    assert body["candidate"]["run_id"] == second
    assert body["source"]["scenario_mode"] == body["candidate"]["scenario_mode"]
    # The resolved/introduced question is only meaningful of a matched pair.
    assert "resolved_classifications" not in body


# --- binding a source at arming ---------------------------------------------


async def test_a_run_with_no_source_has_nothing_to_compare(stack: FastAPI) -> None:
    """A refusal rather than `not_comparable`: there is no pair at all, so there
    are no differing fields to list."""
    # Arrange
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        run_id = await _run(visitor)

        # Act
        response = await visitor.get(f"{RUNS}/{run_id}/comparison")

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PRECONDITION_FAILED"


async def test_an_unfinished_source_cannot_be_bound(stack: FastAPI) -> None:
    """§15.3 says "eligible immutable". A source still in flight has no outcome,
    and binding one would give the pair a "before" side that changes later.

    Exercised against the guard directly, because the API cannot currently reach
    it: arming already refuses while *any* non-terminal run exists in the
    workspace (FR-039), so a live run can never also be an available source. The
    guard is therefore defensive today and load-bearing the moment a later
    milestone lets a workspace hold more than one run — an eval run alongside an
    outcome run, for instance. Deleting it would remove the check exactly when
    it started to matter.
    """
    # Arrange — a run left non-terminal.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        run_id = (await visitor.post(RUNS)).json()["run_id"]
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

    from actionwitness_service.api.errors import ApiError, ApiErrorCode
    from actionwitness_service.application.run_service import RunService

    service = RunService(database, stack.state.adapters, stack.state.locks)

    # Act / Assert
    async with database.reading() as work:
        with pytest.raises(ApiError) as caught:
            await service._require_eligible_source(work, workspace_id, run_id)
    assert caught.value.code is ApiErrorCode.RUN_IN_PROGRESS
    assert "not finished" in caught.value.message


async def test_a_terminal_source_is_eligible(stack: FastAPI) -> None:
    """The counterpart, so "refuses everything" cannot pass as correct."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select_contract(visitor)
        await _scenario(visitor, "pre_fix")
        run_id = await _run(visitor)
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

    from actionwitness_service.application.run_service import RunService

    service = RunService(database, stack.state.adapters, stack.state.locks)

    # Act / Assert — no exception.
    async with database.reading() as work:
        await service._require_eligible_source(work, workspace_id, run_id)


async def test_a_source_from_another_workspace_is_not_visible(
    stack: FastAPI,
) -> None:
    """FR-006 reaches the comparison: a known run id from elsewhere resolves to
    nothing, so it cannot be bound."""
    # Arrange
    async with client(stack) as alice, client(stack) as bob:
        await _select_contract(alice)
        await _scenario(alice, "pre_fix")
        alice_run = await _run(alice)

        await _select_contract(bob)
        await _scenario(bob, "post_fix")

        # Act — Bob is handed Alice's run id.
        response = await bob.post(RUNS, json={"comparison_source_run_id": alice_run})

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_binding_a_source_writes_nothing_when_it_is_refused(
    stack: FastAPI,
) -> None:
    """A refused arming leaves no run, as every other arming refusal does."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as alice, client(stack) as bob:
        await _select_contract(alice)
        await _scenario(alice, "pre_fix")
        alice_run = await _run(alice)

        await _select_contract(bob)
        await _scenario(bob, "post_fix")
        bob_workspace = (await bob.get(WORKSPACE)).json()["workspace_id"]

        # Act
        await bob.post(RUNS, json={"comparison_source_run_id": alice_run})

    # Assert
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id FROM runs WHERE workspace_id = ?", (bob_workspace,))
    assert rows == []
