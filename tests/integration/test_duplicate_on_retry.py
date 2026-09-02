"""012-T1 — the `duplicate_on_retry` injector and AC-05.

AC-05: "Given `duplicate_on_retry`, when the same cart request ID is executed
twice, then verification reports `idempotency_violation` and the evidence shows
the duplicate state change."

**Why the injected retry adds instead of re-assigning.** Appendix D.2 makes
`quantity` absolute, so a retry that re-applied the same absolute value would
leave the cart byte-identical and change no state at all. `idempotent_by_request_id`
judges repetition by whether canonical state moved more than once — so that
version of the fault would be *invisible*, and the policy would pass while the
store misbehaved. The realistic bug is a retry treated as a fresh delta, and
that is the one AC-05 can see.

**The pair of tests matters more than either alone.** `post_fix` proves the
store is genuinely retry-safe, so the `pre_fix` failure is attributable to the
injected fault rather than to a store that never deduplicated at all.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
RETRY_TEMPLATE = "retry_safe_cart_update"
MUG = "mug-ceramic-001"
FAULT = "duplicate_on_retry"
REQUEST_ID = "req_retry_onemug"


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


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


async def _retry_journey(visitor: httpx.AsyncClient, mode: str) -> dict:
    """Arm the retry contract, send one request ID twice, and verify."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == RETRY_TEMPLATE)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})

    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    # Two, because that is the journey the retry contract describes: set two
    # mugs, repeat the identical request, and end with two.
    arguments = {"product_id": MUG, "quantity": 2, "request_id": REQUEST_ID}
    for _ in range(2):
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke", json={"arguments": arguments}
        )
    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
    findings = await visitor.get(f"{RUNS}/{run_id}/findings?limit=10")
    return {"run_id": run_id, "verdict": verdict.json(), "findings": findings.json()}


def _finding(outcome: dict, check_id: str) -> dict:
    return next(f for f in outcome["findings"]["findings"] if f["check_id"] == check_id)


# --- AC-05 -------------------------------------------------------------------


async def test_a_repeated_request_id_is_reported_as_an_idempotency_violation(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-05: "verification reports `idempotency_violation`"."""
    # Arrange / Act
    outcome = await _retry_journey(visitor, "pre_fix")

    # Assert
    assert outcome["verdict"]["overall_result"] == "failed"
    classifications = {finding.get("classification") for finding in outcome["findings"]["findings"]}
    assert "idempotency_violation" in classifications


async def test_the_evidence_shows_the_duplicate_state_change(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-05's second clause, read from the harness's own observation.

    Two mugs were requested, twice, under one request ID. A retry-safe store
    ends with two; this one ends with four, and the assertion finding carries
    that number — which is the point of the product: the evidence is the
    independently observed state, not the tool's report.
    """
    # Arrange / Act
    outcome = await _retry_journey(visitor, "pre_fix")

    # Assert
    quantity = _finding(outcome, "mug-quantity-after-retry")
    assert quantity["status"] == "failed"
    assert quantity["expected"] == 2
    assert quantity["actual"] == 4


async def test_the_same_journey_is_clean_when_the_fault_is_disabled(
    visitor: httpx.AsyncClient,
) -> None:
    """The counterpart that makes the two tests above mean something.

    FR-011 keeps the profile *recorded* in `post_fix` and *inactive*. If the
    store never deduplicated at all, the failure above would be a property of
    the store rather than of the injected fault — and this test would fail too.
    """
    # Arrange / Act
    outcome = await _retry_journey(visitor, "post_fix")

    # Assert
    assert outcome["verdict"]["overall_result"] in {"passed", "passed_with_warnings"}
    assert _finding(outcome, "mug-quantity-after-retry")["status"] == "passed"


async def test_the_retried_call_still_returns_a_valid_response(
    visitor: httpx.AsyncClient,
) -> None:
    """§13.3: "while the tool response stays syntactically valid".

    The point of the profile is that nothing in the *response* betrays the
    defect — only the independently observed state does. A retry that errored
    would be a different, easier bug.
    """
    # Arrange
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == RETRY_TEMPLATE)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    arguments = {"product_id": MUG, "quantity": 2, "request_id": REQUEST_ID}

    # Act
    first = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke", json={"arguments": arguments}
    )
    second = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke", json={"arguments": arguments}
    )

    # Assert
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["reported"]["status"] == "success"


# --- the profile is shipped, not merely recognised ---------------------------


async def test_the_profile_can_be_selected(visitor: httpx.AsyncClient) -> None:
    """An injector that could not be selected would be a described fault the
    store refuses to produce — which is what it was before 012-T1."""
    # Arrange / Act
    response = await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})

    # Assert
    assert response.status_code == 200, response.text
    assert (await visitor.get(WORKSPACE)).json()["failure_profile"] == FAULT
