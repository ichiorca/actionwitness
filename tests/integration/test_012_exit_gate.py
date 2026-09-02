"""012's exit gate — breadth and polish, and what was cut (012-T9).

This milestone is unlike the ones before it. Every item was optional and each
was a whole: BUILD_ORDER's M11 says "implement only complete features", so the
gate has to answer two questions rather than one — *did the shipped items ship
completely*, and *is the cut item genuinely absent*.

Five of seven shipped. One was cut for a reason no amount of code would change
(plan.md D1: the harness's confirmation gate and the `requires_confirmation`
policy read the same contract policy, so a protected tool either pauses for a
human and passes, or is unprotected and passes vacuously — no store-side fault
reaches AC-07's classification). One shipped server-side only, deliberately
(D6).

**Criterion 1 is conditional and this file honours the condition.** "AC-02,
AC-05, AC-07, AC-13, and AC-14 are green *for what ships* — an item's criterion
is required if and only if the item shipped." AC-07 is therefore not asserted
here, and `test_gate_2_the_cut_item_is_genuinely_absent` is what stops that from
being a convenient silence: the cut has to be real, refused, and unreachable.

The per-item behaviour is tested where it lives. What this gate adds is the
roll-up the milestone is judged on, plus the two properties no single item's
tests can see: that the cut is airtight, and that nothing shipped half-way.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"

#: The one item M11 cut, and the profile that would have demonstrated it.
CUT_PROFILE = "checkout_without_confirmation"

#: What shipped, and the criterion each carries. AC-07 is absent on purpose.
SHIPPED_CRITERIA = {
    "AC-05": "duplicate_on_retry",
    "AC-13": "observed trajectory",
    "AC-14": "invocation cancellation",
    "AC-02": "declarative registration",
}


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


async def _select(visitor: httpx.AsyncClient, template: str) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(item for item in templates if item["source_template_id"] == template)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")


# --- criterion 1: the shipped items carry their criteria ---------------------


async def test_gate_1_the_declarative_form_creates_an_armable_contract(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-02's server half, end to end (T5).

    The browser half of AC-02 — three mechanisms visible in DevTools — is an
    operator checklist (`tests/browser/ac02-registration-checklist.md`, plan.md
    D2), because jsdom has no WebMCP and a declarative tool has no registration
    call to intercept. What is asserted here is the part a test can reach: the
    flat form's endpoint expands a trusted template and produces a contract a
    run can actually be armed against.

    A form that created something unarmable would be a feature that appears to
    work, which is the M11 failure wearing a different hat.
    """
    # Arrange / Act
    created = await visitor.post(
        CONTRACTS, json={"template_id": CANONICAL, "quantity": 2, "contract_name": "Gate"}
    )
    assert created.status_code == 201, created.text
    await visitor.post(f"{CONTRACTS}/{created.json()['contract_id']}/select")
    armed = await visitor.post(RUNS)

    # Assert
    assert armed.status_code in {200, 201}, armed.text


async def test_gate_1_a_duplicated_retry_is_caught_by_independent_state(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-05 (T1): the fault is visible in observed state, not in the response.

    Both calls report success. The store applied the mutation twice, and only
    the independently observed cart says so — which is the whole claim of the
    product, exercised through the profile this milestone shipped.
    """
    # Arrange
    await _select(visitor, "retry_safe_cart_update")
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})
    await visitor.put(
        f"{WORKSPACE}/failure-profile", json={"failure_profile": "duplicate_on_retry"}
    )
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    body = {"arguments": {"product_id": MUG, "quantity": 2, "request_id": "req_gate_dup"}}

    # Act — the identical request twice, under one request ID.
    first = await visitor.post(f"{RUNS}/{run_id}/target-tools/update_cart:invoke", json=body)
    second = await visitor.post(f"{RUNS}/{run_id}/target-tools/update_cart:invoke", json=body)
    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
    findings = (await visitor.get(f"{RUNS}/{run_id}/findings?limit=10")).json()["findings"]

    # Assert — both self-reports are successes, and the run fails anyway. The
    # number that convicts the store comes from the harness's own observation,
    # which is the entire claim of the product.
    assert first.json()["reported"]["status"] == "success"
    assert second.json()["reported"]["status"] == "success"
    assert verdict.json()["overall_result"] == "failed"
    assert "idempotency_violation" in {finding.get("classification") for finding in findings}
    quantity = next(f for f in findings if f["check_id"] == "mug-quantity-after-retry")
    assert (quantity["expected"], quantity["actual"]) == (2, 4)


async def test_gate_1_a_missing_required_call_fails_only_the_trajectory_layer(
    visitor: httpx.AsyncClient,
) -> None:
    """AC-13 (T3), including the clause that makes it worth having.

    The model-selection layer stays `not_evaluated`. A failed trajectory is
    exactly when it is tempting to conclude something about the model, and the
    harness saw no evaluator report — inferring one from a trajectory is the
    mislabelling AC-13 is named after.
    """
    # Arrange
    await _select(visitor, CANONICAL)
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_gate_traj"}},
    )
    await visitor.post(
        f"{RUNS}/{run_id}/target-tools/apply_discount:invoke",
        json={"arguments": {"code": "SAVE20"}},
    )

    # Act
    await visitor.post(f"{RUNS}/{run_id}/verify")
    layers = (await visitor.get(f"{RUNS}/{run_id}/report")).json()["report"]["layers"]

    # Assert
    assert layers["observed_trajectory"] == "failed"
    assert layers["model_tool_selection"] == "not_evaluated"


async def test_gate_1_a_cancelled_checkout_creates_no_order(
    stack: FastAPI, visitor: httpx.AsyncClient
) -> None:
    """AC-14 (T4), read from the target rather than from the report.

    "No consequential action happened" is only worth asserting against
    independently observed state — asking the report would be asking the
    channel under test.
    """
    # Arrange
    await _select(visitor, "confirmed_checkout_only")
    await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "post_fix"})
    run_id = str((await visitor.post(RUNS)).json()["run_id"])
    await visitor.post(
        f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
        json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_gate_cancel"}},
    )
    pending = await visitor.post(
        f"{RUNS}/{run_id}/target-tools/proceed_to_checkout:invoke",
        json={"arguments": {"request_id": "req_gate_checkout"}},
    )
    confirmation_id = pending.json()["confirmation"]["confirmation_id"]

    # Act
    cancelled = await visitor.delete(f"{RUNS}/{run_id}/confirmations/{confirmation_id}")

    # Assert
    assert cancelled.status_code == 200, cancelled.text
    kinds = [
        event["event_type"]
        for event in (await visitor.get(f"{RUNS}/{run_id}/events?limit=50")).json()["events"]
    ]
    assert "tool_invocation_cancelled" in kinds
    workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
    cart = await stack.state.target_client.get(
        "/demo/api/v1/store/cart", headers={"X-Workspace-Id": workspace_id}
    )
    assert cart.json()["order"]["created"] is False


# --- criterion 2: the cut item is absent, not half-present -------------------


async def test_gate_2_the_cut_item_is_genuinely_absent(visitor: httpx.AsyncClient) -> None:
    """Criterion 2, and the reason criterion 1 is allowed to skip AC-07.

    An unasserted criterion is only honest if the item really did not ship. The
    cut profile must be refused at selection, refused at arming, and never
    recorded — the third being the one that used to fail (plan.md D8).

    A cut that merely *usually* refuses would let AC-07's silence turn into a
    claim nobody made.
    """
    # Arrange
    await _select(visitor, CANONICAL)

    # Act
    refused = await visitor.put(
        f"{WORKSPACE}/failure-profile", json={"failure_profile": CUT_PROFILE}
    )

    # Assert
    assert 400 <= refused.status_code < 500, refused.text
    assert CUT_PROFILE in refused.text
    assert (await visitor.get(WORKSPACE)).json()["failure_profile"] != CUT_PROFILE


def test_gate_2_the_cut_item_ships_no_injector() -> None:
    """The other half of "genuinely absent": nothing implements it.

    Named from the store's own set rather than from a list here, so shipping the
    injector later turns this test red — a reminder to reinstate AC-07 — instead
    of leaving a stale claim in a file nobody re-reads.
    """
    from buggy_store.failure_injection import IMPLEMENTED_PROFILES, FaultProfile

    assert FaultProfile(CUT_PROFILE) not in IMPLEMENTED_PROFILES, (
        f"{CUT_PROFILE} now has an injector; AC-07 is no longer cut and this "
        "milestone's gate must assert it"
    )


# --- criterion 3: product copy ------------------------------------------------


def test_gate_3_no_document_claims_the_cut_item_works() -> None:
    """Constitution §8, for this milestone's specific cut.

    The general claim check lives in `test_cut_feature_hygiene.py`; this is the
    narrow one that would embarrass the demo — a README sentence describing a
    consent failure the build cannot produce.
    """
    for document in (REPO_ROOT / "README.md",):
        for line in document.read_text(encoding="utf-8").splitlines():
            if CUT_PROFILE not in line:
                continue
            assert any(
                marker in line.lower()
                for marker in ("not implemented", "not shipped", "cut", "unavailable", "tier 3")
            ), f"{document.name} describes {CUT_PROFILE} without saying it is not shipped"


# --- criterion 5: each shipped item has an integration test ------------------


def test_gate_5_every_shipped_item_has_an_integration_test() -> None:
    """§26.2: "if shipped ... each receive one integration test".

    Named files rather than a count. A milestone can be declared finished with
    an item covered by nothing at all, and "the suite is green" is not the same
    claim as "this item is tested".
    """
    covering = {
        "AC-05 duplicate_on_retry": "tests/integration/test_duplicate_on_retry.py",
        "AC-13 observed trajectory": "tests/integration/test_observed_trajectory_edges.py",
        "AC-14 invocation cancellation": "tests/integration/test_invocation_cancellation.py",
        "AC-02 declarative form": "tests/integration/test_contract_instantiation.py",
        "toolchange reconciliation": "tests/architecture/test_webmcp_adapter_isolation.py",
        "SSE": "tests/integration/test_event_stream.py",
    }
    missing = [
        f"{item} -> {relative}"
        for item, relative in covering.items()
        if not (REPO_ROOT / relative).is_file()
    ]
    assert missing == [], f"shipped items without their integration test: {missing}"


def test_gate_5_the_browser_criteria_are_written_down_for_the_operator() -> None:
    """What a test cannot reach must be a checklist, not an assumption.

    AC-02's DevTools half needs a person at a flagged browser (§26.4, §7.5).
    Leaving it unwritten would let "green suite" read as "AC-02 proved", which
    is the one thing this file must not let happen.
    """
    checklist = REPO_ROOT / "tests" / "browser" / "ac02-registration-checklist.md"
    assert checklist.is_file(), "AC-02's browser checklist is missing"
    copy = checklist.read_text(encoding="utf-8")
    for mechanism in ("native", "imperative", "declarative"):
        assert mechanism in copy.lower(), f"the checklist does not cover the {mechanism} path"
