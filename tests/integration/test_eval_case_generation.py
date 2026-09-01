"""007-T2 — generating a case, and only from an eligible run (§24.2, FR-080).

The property this milestone rests on is byte-identical regeneration: "the same
source run yields the same case". A CI job that regenerated a case and saw a
spurious diff would teach its reviewers to ignore diffs, which is the opposite
of what a portable artifact is for. `test_regenerating_produces_identical_bytes`
is that claim, and it is checked on the *bytes* rather than on a field-by-field
comparison, because equality of parsed objects would hide exactly the kind of
ordering drift a hash notices.

Eligibility has three distinct refusals and they must stay distinct: a run still
running will one day have a verdict (wait), a passing run never will have a
failure (nothing to reproduce), and a proposal run has no verdict by design
(`PROPOSAL_RUN_NOT_ELIGIBLE`). A caller that received one generic refusal for
all three would retry the wrong one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.evals import EvalEnvironment, RegressionEvalCase
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.eval_case_service import EvalCaseService
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


async def _select(visitor: httpx.AsyncClient, template: str = CANONICAL) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == template)
    assert (await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")).status_code == 200


async def _failed_run(visitor: httpx.AsyncClient) -> tuple[str, str]:
    """A run that fails `false_success_or_state_mismatch`. Returns (workspace, run)."""
    await _select(visitor)
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    for tool, arguments in (
        ("search_catalog", {"query": "mug"}),
        ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
        ("apply_discount", {"code": "SAVE20"}),
    ):
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
    assert verdict.json()["overall_result"] == "failed", verdict.text
    workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
    return workspace_id, run_id


async def _generate(database: Database, workspace_id: str, run_id: str, registry=None):
    async with database.transaction() as work:
        return await EvalCaseService(work, workspace_id, registry).generate(run_id)


# --- generation --------------------------------------------------------------


async def test_a_failed_run_produces_a_case_that_records_its_failure(
    stack: FastAPI,
) -> None:
    """§24.2 steps 8–9: the expectation is copied from the recording."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act
    generated = await _generate(database, workspace_id, run_id)

    # Assert
    case = generated.case
    assert generated.created is True
    assert case.source.run_id == run_id
    assert case.source.overall_result.value == "failed"
    assert [c.value for c in case.source.critical_classifications] == [
        "false_success_or_state_mismatch"
    ]
    # Step 8: `reproduce_source` expects exactly what the run produced.
    reproduce = case.expected.for_environment(EvalEnvironment.REPRODUCE_SOURCE)
    assert reproduce.overall_result.value == "failed"
    assert {c.value for c in reproduce.required_classifications} == {
        "false_success_or_state_mismatch"
    }
    # Step 7: `current` expects the corrected implementation to be clean.
    current = case.expected.for_environment(EvalEnvironment.CURRENT)
    assert current.overall_result.value == "passed"
    assert current.required_classifications == ()


async def test_regenerating_produces_identical_bytes(stack: FastAPI) -> None:
    """FR-080's idempotence, as a property of the content.

    Compared on the serialized bytes rather than on parsed objects: equal
    objects would hide ordering drift, and the hash is what a reader actually
    checks a year later.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act — build twice, genuinely. The stored row is removed in between,
    # because a second `generate` would otherwise short-circuit to it and the
    # test would prove round-tripping rather than determinism.
    first = await _generate(database, workspace_id, run_id)
    async with database.transaction() as work:
        await work.execute("DELETE FROM evaluation_cases WHERE source_run_id = ?", (run_id,))
    second = await _generate(database, workspace_id, run_id)

    # Assert
    assert second.created is True, "the second call must have built a case, not read one"
    assert first.case.canonical_bytes() == second.case.canonical_bytes()
    assert first.case.content_hash() == second.case.content_hash()
    # The id is derived from the evidence too (FR-080's key), so a fresh
    # identifier cannot creep into the one field a reader trusts most.
    assert first.case.id == second.case.id


async def test_a_repeat_returns_the_existing_case_and_says_it_did_not_create_one(
    stack: FastAPI,
) -> None:
    """§17.1: "returns the existing case and `created: false`; it never mints a
    duplicate"."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act
    first = await _generate(database, workspace_id, run_id)
    second = await _generate(database, workspace_id, run_id)

    # Assert
    assert first.created is True
    assert second.created is False
    assert second.case_id == first.case_id
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT id FROM evaluation_cases WHERE source_run_id = ?", (run_id,)
        )
    assert len(rows) == 1


async def test_the_stored_case_round_trips_through_its_own_bytes(stack: FastAPI) -> None:
    """A case is only portable if what was written parses back to what was
    generated — otherwise the artifact and the object are two things."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    generated = await _generate(database, workspace_id, run_id)

    # Act
    async with database.reading() as work:
        stored, row = await EvalCaseService(work, workspace_id).get(generated.case_id)

    # Assert
    assert isinstance(stored, RegressionEvalCase)
    assert stored.canonical_bytes() == generated.case.canonical_bytes()
    assert row["content_hash"] == generated.case.content_hash()


async def test_the_case_embeds_the_contract_and_verifies_its_hash(stack: FastAPI) -> None:
    """§24.2 step 6, and FR-082's self-containment."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act
    case = (await _generate(database, workspace_id, run_id)).case

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one("SELECT contract_content_hash FROM runs WHERE id = ?", (run_id,))
    assert row is not None
    assert case.contract.content_hash == row["contract_content_hash"]
    # Embedded, not referenced: a case pointing at a row could not be handed to
    # anybody.
    assert case.contract.document.assertions


async def test_the_trajectory_is_what_the_agent_attempted(stack: FastAPI) -> None:
    """§10.3 builds the observed trajectory from start events, so a case
    replays what was attempted rather than only what succeeded."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act
    case = (await _generate(database, workspace_id, run_id)).case

    # Assert
    assert [step.tool for step in case.trajectory] == [
        "search_catalog",
        "update_cart",
        "apply_discount",
    ]
    assert [step.sequence for step in case.trajectory] == [1, 2, 3]
    # The arguments travel, or a replay could not make the same calls.
    assert case.trajectory[2].arguments == {"code": "SAVE20"}


async def test_the_failure_profile_travels_as_provenance_not_as_configuration(
    stack: FastAPI,
) -> None:
    """§24.2 step 9. A case that forced its own fault would make `current`
    untestable — and `current` is the profile CI runs."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act
    case = (await _generate(database, workspace_id, run_id)).case

    # Assert
    assert case.source.failure_profile == FAULT
    assert case.source.scenario_mode == "pre_fix"
    assert case.replay.default_environment is EvalEnvironment.CURRENT


# --- eligibility -------------------------------------------------------------


async def test_a_run_still_in_flight_cannot_produce_a_case(stack: FastAPI) -> None:
    """A run with no verdict would embed a prediction rather than an
    observation."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select(visitor)
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

    # Act / Assert
    with pytest.raises(ApiError) as caught:
        await _generate(database, workspace_id, run_id)
    assert caught.value.code is ApiErrorCode.PRECONDITION_FAILED
    assert "armed" in caught.value.message


async def test_a_passing_run_has_no_failure_to_reproduce(stack: FastAPI) -> None:
    """FR-080: only failed or warning-bearing runs are eligible.

    A case cut from a passing run would assert that nothing goes wrong, which
    every future build satisfies right up until it doesn't.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        await _select(visitor)
        await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        for tool, arguments in (
            ("search_catalog", {"query": "mug"}),
            ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
            ("apply_discount", {"code": "SAVE20"}),
        ):
            await visitor.post(
                f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
            )
        verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
        assert verdict.json()["overall_result"] == "passed"
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]

    # Act / Assert
    with pytest.raises(ApiError) as caught:
        await _generate(database, workspace_id, run_id)
    assert caught.value.code is ApiErrorCode.PRECONDITION_FAILED
    assert "passed" in caught.value.message


async def test_a_proposal_run_is_refused_by_name(stack: FastAPI) -> None:
    """§24.3a: "refused with `PROPOSAL_RUN_NOT_ELIGIBLE`".

    Constructed directly, because 005 declared proposal mode and refuses it at
    arming — so the state is unreachable through the API today. The guard is
    kept and tested because it becomes reachable the moment proposal mode
    ships, and its refusal is a *different* answer from "not terminal yet": a
    caller told to wait would wait forever.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    async with database.transaction() as work:
        await work.execute("UPDATE runs SET status = 'proposed' WHERE id = ?", (run_id,))

    # Act / Assert
    with pytest.raises(ApiError) as caught:
        await _generate(database, workspace_id, run_id)
    assert caught.value.code is ApiErrorCode.PROPOSAL_RUN_NOT_ELIGIBLE
    assert "no verdict" in caught.value.message


async def test_another_workspaces_run_cannot_be_turned_into_a_case(
    stack: FastAPI,
) -> None:
    """AC-11 again. Two clients, and the stranger holds the real run id."""
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as owner:
        _owner_workspace, run_id = await _failed_run(owner)
    async with client(stack) as stranger:
        stranger_workspace = (await stranger.get(WORKSPACE)).json()["workspace_id"]

    # Act / Assert
    with pytest.raises(ApiError) as caught:
        await _generate(database, stranger_workspace, run_id)
    assert caught.value.code is ApiErrorCode.RESOURCE_NOT_FOUND
    # And nothing was written on the way to refusing.
    async with database.reading() as work:
        rows = await work.fetch_all("SELECT id FROM evaluation_cases")
    assert rows == []


# --- §24.2's content rules, through the real service -------------------------


async def test_the_fixture_keeps_only_what_the_contract_needs(stack: FastAPI) -> None:
    """§24.2 step 2, with a real recorded observation rather than a literal.

    The canonical contract asserts on the cart and the order, so a fixture
    carrying the store's other state has been trimmed — and `complete` says so,
    which is what lets the runner tell "small because nothing else mattered"
    from "small because somebody trimmed it wrongly".
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act
    case = (await _generate(database, workspace_id, run_id, stack.state.adapters)).case

    # Assert
    assert set(case.fixture.target_state) <= {"cart", "order"}
    assert "cart" in case.fixture.target_state
    assert case.fixture.content_hash.startswith("sha256:")


async def test_a_read_only_call_feeding_a_mutation_survives_minimization(
    stack: FastAPI,
) -> None:
    """§24.2 step 3, through the adapter's own read-only metadata.

    `search_catalog` is read-only and nothing asserts on it, but a mutation
    follows it — and nothing here can prove the product id it returned was not
    what that mutation used. Keeping it is the conservative direction, and the
    one that keeps the replay able to start.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)

    # Act
    case = (await _generate(database, workspace_id, run_id, stack.state.adapters)).case

    # Assert
    assert [step.tool for step in case.trajectory] == [
        "search_catalog",
        "update_cart",
        "apply_discount",
    ]
