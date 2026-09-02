"""012-T3 — observed-trajectory edge cases and AC-13.

AC-13: "Given a contract with required expected tools, when a completed run omits
one, then verification reports `missing_expected_tool` in the observed-trajectory
layer; the model-selection layer remains `not_evaluated` unless a compatible
external report was imported."

**The second clause is the one worth guarding.** A run whose trajectory failed
is exactly when somebody would be tempted to say something about the model: the
required call was not made, so surely the model chose badly? The harness cannot
know that. It saw no evaluator report, and §10.3 keeps model selection
`not_evaluated` until one is bound. Reporting anything else would be inferring a
model's behaviour from a trajectory, which is the mislabeling AC-13 is named
after.

The engine's own rules are unit-tested in `test_trajectory_and_policies.py`.
What these tests add is §26.2's requirement of *one integration test through the
real boundary*, plus the edge cases where the distinction between "did not
happen", "happened out of order", and "happened but failed" decides which
classification a reader sees.
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
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"

FULL_JOURNEY = (
    ("search_catalog", {"query": "mug"}),
    ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_traj_onemug"}),
    ("apply_discount", {"code": "SAVE20"}),
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
            yield harness


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


async def _journey(visitor: httpx.AsyncClient, calls: tuple) -> dict:
    """Arm the canonical contract, make exactly `calls`, and verify."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})

    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    for tool, arguments in calls:
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
    await visitor.post(f"{RUNS}/{run_id}/verify")
    report = (await visitor.get(f"{RUNS}/{run_id}/report")).json()["report"]
    findings = (await visitor.get(f"{RUNS}/{run_id}/findings?limit=10")).json()
    return {"report": report, "findings": findings}


def _trajectory_finding(outcome: dict) -> dict:
    return next(
        finding
        for finding in outcome["findings"]["findings"]
        if finding["check_type"] == "expected_tools"
    )


# --- AC-13 -------------------------------------------------------------------


async def test_an_omitted_required_call_is_reported_as_missing(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-13's first clause. The journey succeeds at everything it *did*.

    The cart is right and the discount applies, so every assertion passes —
    only the trajectory layer disagrees, and it names the call that never
    happened rather than reporting a vague failure.
    """
    # Arrange / Act — the same journey, minus `search_catalog`.
    outcome = await _journey(visitor, FULL_JOURNEY[1:])

    # Assert
    assert outcome["report"]["layers"]["observed_trajectory"] == "failed"
    finding = _trajectory_finding(outcome)
    assert finding["classification"] == "missing_expected_tool"
    assert "search_catalog" in str(finding)


async def test_the_model_selection_layer_stays_not_evaluated(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-13's second clause, and the reason the criterion exists.

    A failed trajectory is precisely when it is tempting to conclude something
    about the model — the required call was not made, so surely it chose badly?
    The harness saw no evaluator report and cannot know. Saying anything else
    would infer a model's behaviour from a trajectory.
    """
    # Arrange / Act
    outcome = await _journey(visitor, FULL_JOURNEY[1:])

    # Assert
    layers = outcome["report"]["layers"]
    assert layers["observed_trajectory"] == "failed"
    assert layers["model_tool_selection"] == "not_evaluated"


async def test_a_complete_journey_passes_the_trajectory_layer(
    visitor: httpx.AsyncClient,
) -> None:
    """The counterpart. Without it, a layer that always failed would satisfy
    the two tests above."""
    # Arrange / Act
    outcome = await _journey(visitor, FULL_JOURNEY)

    # Assert
    assert outcome["report"]["layers"]["observed_trajectory"] == "passed"
    assert outcome["report"]["layers"]["model_tool_selection"] == "not_evaluated"


# --- edge cases --------------------------------------------------------------


async def test_a_failed_call_does_not_count_as_an_occurrence(
    visitor: httpx.AsyncClient,
) -> None:
    """ "Attempted" is not "occurred".

    `apply_discount` with an unknown code is refused by the store. The tool was
    called, so a trajectory built from *start* events alone would count it and
    report a pass — the run would look conformant while the discount step never
    took effect.
    """
    # Arrange / Act
    outcome = await _journey(
        visitor,
        (
            FULL_JOURNEY[0],
            FULL_JOURNEY[1],
            ("apply_discount", {"code": "NOT-A-REAL-CODE"}),
        ),
    )

    # Assert
    assert outcome["report"]["layers"]["observed_trajectory"] == "failed"
    assert _trajectory_finding(outcome)["classification"] == "missing_expected_tool"


async def test_a_repeated_call_does_not_satisfy_two_required_entries(
    visitor: httpx.AsyncClient,
) -> None:
    """§10.3: each entry requires one distinct occurrence.

    Calling `search_catalog` twice covers the one entry the contract has for it
    and nothing else — the omitted `apply_discount` is still missing. A
    membership check rather than a count would call this a pass.
    """
    # Arrange / Act
    outcome = await _journey(visitor, (FULL_JOURNEY[0], FULL_JOURNEY[0], FULL_JOURNEY[1]))

    # Assert
    finding = _trajectory_finding(outcome)
    assert finding["classification"] == "missing_expected_tool"
    assert "apply_discount" in str(finding)


async def test_the_finding_lists_what_was_required_and_what_was_seen(
    visitor: httpx.AsyncClient,
) -> None:
    """A trajectory failure has to be actionable.

    "Something was missing" sends a reader back to the timeline; naming the
    required set beside the observed one answers the question in the finding.
    """
    # Arrange / Act
    outcome = await _journey(visitor, FULL_JOURNEY[1:])

    # Assert
    text = str(_trajectory_finding(outcome))
    assert "search_catalog" in text
    assert "update_cart" in text


async def test_a_run_with_no_calls_at_all_cannot_be_verified(
    visitor: httpx.AsyncClient,
) -> None:
    """§16: "a run with no accepted target action cannot be verified".

    The edge below `missing_expected_tool`: with nothing observed there is no
    journey to judge, and reporting a trajectory failure would imply one was
    attempted. Refused rather than verified.
    """
    # Arrange
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    run_id = str((await visitor.post(RUNS)).json()["run_id"])

    # Act
    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")

    # Assert — refused, and by a code that names the reason. The run is armed
    # with nothing observed, so it is still in progress rather than complete.
    assert verdict.status_code >= 400
    assert verdict.json()["error"]["code"] == "RUN_IN_PROGRESS"
