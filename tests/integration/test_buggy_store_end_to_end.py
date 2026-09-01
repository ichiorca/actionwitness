"""One target call traced end to end, and the M2 exit gate (003-T13).

BUILD_ORDER §7/M2's last exit-gate item: "one target call is traced end to end:
harness-facing arguments -> adapter HTTP request -> store mutation ->
authoritative adapter observation."

That trace is the milestone's whole claim in one test. Each hop is asserted
separately, including the *request the adapter actually sent*, because the
interesting failure is a hop that silently does something other than what the
one before it asked for — an adapter that dropped an argument, or an observation
that echoed the response instead of reading state, would pass a test that only
checked the two ends.

The second half runs the §10.1 contract through the M1 engine against this real
target in both scenario modes. That is the first time the whole stack meets:
core evaluation, a real adapter, a real store, and an injected defect. The
`pre_fix` run must fail with `false_success_or_state_mismatch` while its
execution and trajectory layers pass — which is exactly the shape of AC-04 and
the M4 exit gate, reached here through the target boundary rather than through
FastAPI.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.assertions import evaluate_assertions, evaluate_preconditions
from actionwitness_core.engine.classification import (
    classify_assertion_failures,
    tool_execution_layer,
)
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.engine.policies import PolicyEvidence, evaluate_policies
from actionwitness_core.engine.trajectory import evaluate_expected_tools
from actionwitness_core.evidence.models import RunEvent, Snapshot
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState, SnapshotPhase
from actionwitness_core.ports.models import ExecutionContext, ScenarioSelection
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.reports.models import (
    ContractReference,
    ScenarioReference,
    TargetReference,
    compose_outcome_report,
)
from buggy_store.api import create_app
from integrations.buggy_store.templates import template_for

from integrations.buggy_store import ADAPTER_ID, TARGET_ID, BuggyStoreAdapter

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
MUG = "mug-ceramic-001"


class _RecordingTransport(httpx.AsyncBaseTransport):
    """Wraps the ASGI transport and records what actually crossed the wire.

    The trace has to show the adapter's *request*, not just its result. A
    recording wrapper is the only way to assert the middle hop without the
    adapter cooperating in being observed.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._inner.handle_async_request(request)


@pytest.fixture
async def traced(tmp_path: Path) -> AsyncIterator[tuple[BuggyStoreAdapter, _RecordingTransport]]:
    app = create_app(database_path=tmp_path / "store.sqlite3")
    async with app.router.lifespan_context(app), httpx.ASGITransport(app=app) as asgi:
        transport = _RecordingTransport(asgi)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://buggy-store.test"
        ) as client:
            yield BuggyStoreAdapter(client, clock=lambda: EPOCH), transport


def _context(sequence: int) -> ExecutionContext:
    return ExecutionContext(
        workspace_id="ws-1",
        run_id="run-1",
        invocation_id=f"inv-{sequence}",
        request_id=f"req-{sequence:>012}",
        correlation_id=f"corr-{sequence}",
        idempotency_key=f"key-{sequence}",
        actor=EventActor.AGENT,
    )


# --- the traced call (M2 exit gate, item 5) ---------------------------------


@pytest.mark.integration
async def test_one_target_call_is_traced_end_to_end(traced) -> None:
    """Arguments -> HTTP request -> store mutation -> authoritative observation."""
    adapter, transport = traced
    await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode="post_fix"))
    provider = adapter.observation_provider()

    before = await provider.capture("ws-1")
    assert before.payload["cart"]["items"] == {}

    transport.requests.clear()

    # Hop 1 — harness-facing arguments, exactly as a WebMCP tool would supply them.
    arguments = {"product_id": MUG, "quantity": 2, "request_id": "req-000000000001"}
    result = await adapter.execute("ws-1", "update_cart", arguments, _context(1))

    # Hop 2 — the adapter's HTTP request, over the store's versioned surface.
    mutation = next(
        request
        for request in transport.requests
        if request.method == "POST" and request.url.path.endswith("/store/cart/mutations")
    )
    assert mutation.url.path == "/demo/api/v1/store/cart/mutations"
    assert mutation.headers["X-Workspace-Id"] == "ws-1"
    assert json.loads(mutation.content) == arguments

    # Hop 3 — the store mutated, and said so. This is the self-report channel.
    assert result.terminal_event is OutcomeEventType.TOOL_INVOCATION_COMPLETED
    assert result.claims_success() is True
    assert result.state_version_before == "1"
    assert result.state_version_after == "2"

    # Hop 4 — an authoritative observation, read independently of that report.
    after = await provider.capture("ws-1")
    assert after.payload["cart"]["items"]["mug"]["quantity"] == 2
    assert after.payload["cart"]["subtotal"] == "50.00"
    assert after.state_version == "2"

    # The two channels stay distinguishable all the way through.
    assert result.source_classification.value == "tool_reported"
    assert after.source_classification.value == "authoritative_observation"


@pytest.mark.integration
async def test_the_observation_is_a_separate_read_not_an_echo(traced) -> None:
    """Constitution §4: observed state is never manufactured from a tool response."""
    adapter, transport = traced
    await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode="post_fix"))
    transport.requests.clear()

    await adapter.observation_provider().capture("ws-1")

    reads = [request for request in transport.requests if request.method == "GET"]
    assert [request.url.path for request in reads] == ["/demo/api/v1/store/state"]


# --- the whole stack, in both modes -----------------------------------------


async def _journey(adapter: BuggyStoreAdapter, mode: str) -> dict:
    """Run §10.1's contract against the real store through the real adapter."""
    fault = "discount_reported_but_not_applied"
    await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode=mode, fault_profile=fault))
    provider = adapter.observation_provider()
    contract = parse_contract(template_for("one_mug_save20_no_checkout").document)
    contract.validate_against_target(
        target_id=TARGET_ID,
        tool_names=[spec.name for spec in adapter.tool_specs()],
        protected_tools=["proceed_to_checkout"],
    )

    initial = Snapshot.of("run-1", SnapshotPhase.BEFORE, await provider.capture("ws-1"))
    preconditions = evaluate_preconditions(contract.preconditions, initial=initial.as_context())
    assert all(finding.status is CheckStatus.PASSED for finding in preconditions)

    events: list[RunEvent] = []
    journey = (
        ("search_catalog", {"query": "mug"}),
        ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req-000000000001"}),
        ("apply_discount", {"code": "SAVE20"}),
    )
    for sequence, (tool, arguments) in enumerate(journey, start=1):
        context = _context(sequence)
        events.append(
            RunEvent(
                sequence_number=sequence * 2 - 1,
                event_type=OutcomeEventType.TOOL_INVOCATION_STARTED,
                actor=EventActor.AGENT,
                created_at=EPOCH,
                tool_name=tool,
                correlation_id=context.correlation_id,
                request_id=context.request_id,
            )
        )
        result = await adapter.execute("ws-1", tool, arguments, context)
        # FR-032's immediate post-call observation, captured independently.
        post_call = await provider.capture("ws-1")
        events.append(
            RunEvent(
                sequence_number=sequence * 2,
                event_type=result.terminal_event,
                actor=EventActor.AGENT,
                created_at=EPOCH,
                tool_name=result.tool_name,
                correlation_id=result.correlation_id,
                request_id=result.request_id,
                reported_status=result.reported_status,
                post_call_effect_state=post_call.as_context(),
            )
        )

    final = Snapshot.of("run-1", SnapshotPhase.AFTER, await provider.capture("ws-1"))
    effect_map = {
        tool: tuple(ObservationPath.parse(path) for path in paths)
        for tool, paths in adapter.effect_map().items()
    }
    assertions = classify_assertion_failures(
        evaluate_assertions(
            contract.assertions, initial=initial.as_context(), final=final.as_context()
        ),
        contract.assertions,
        events=events,
        effect_map=effect_map,
        initial=initial.as_context(),
    )
    report = compose_outcome_report(
        run_id=f"run-{mode}",
        target=TargetReference(id=TARGET_ID, adapter_id=ADAPTER_ID),
        scenario=ScenarioReference(
            mode=mode, fault_profile=fault, fault_active=(mode == "pre_fix")
        ),
        contract=ContractReference(
            id="contract-1",
            schema_version=contract.schema_version,
            content_hash=contract.content_hash(),
        ),
        assertion_findings=assertions,
        policy_findings=evaluate_policies(contract.policies, PolicyEvidence(events=tuple(events))),
        trajectory_finding=evaluate_expected_tools(contract.expected_tools, events),
        tool_execution=tool_execution_layer(events),
        events=events,
    )
    return {"contract": contract, "assertions": assertions, "final": final, "report": report}


@pytest.mark.integration
async def test_the_pre_fix_journey_fails_as_a_false_success(traced) -> None:
    """AC-04's shape, reached through the real target boundary.

    The tool said the discount applied. Independent observation says the total
    never moved. The execution and trajectory layers pass, and the business
    outcome does not — which is the distinction the product exists to draw.
    """
    adapter, _ = traced
    journey = await _journey(adapter, "pre_fix")
    report = journey["report"]

    assert journey["final"].observation.payload["cart"]["total"] == "25.00"

    failed = [finding for finding in journey["assertions"] if finding.failed]
    assert [finding.check_id for finding in failed] == ["discounted-total"]
    assert failed[0].classification is FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH
    assert failed[0].expected == "20.00"
    assert failed[0].actual == "25.00"
    assert failed[0].attributed_cause["tool_name"] == "apply_discount"

    assert report.layers.observed_trajectory is LayerResult.PASSED
    assert report.layers.tool_execution is LayerResult.PASSED
    assert report.layers.business_outcome is LayerResult.FAILED
    assert report.layers.safety_policy is LayerResult.PASSED
    assert report.layers.model_tool_selection is LayerResult.NOT_EVALUATED
    assert report.status is RunState.FAILED
    assert report.primary_failure is FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH


@pytest.mark.integration
async def test_the_post_fix_journey_passes(traced) -> None:
    """The same contract, the same build, the corrected implementation profile."""
    adapter, _ = traced
    journey = await _journey(adapter, "post_fix")

    assert journey["final"].observation.payload["cart"]["total"] == "20.00"
    assert all(finding.status is CheckStatus.PASSED for finding in journey["assertions"])
    assert journey["report"].status is RunState.PASSED
    assert journey["report"].layers.business_outcome is LayerResult.PASSED
    assert journey["report"].primary_failure is None


@pytest.mark.integration
async def test_the_matched_pair_differs_only_in_scenario_mode(traced) -> None:
    """FR-019 pairs two runs whose controlled inputs are equal but for the mode."""
    adapter, _ = traced
    failing = await _journey(adapter, "pre_fix")
    passing = await _journey(adapter, "post_fix")

    # The controlled input FR-019 pairs on: the contract's immutable identity.
    assert failing["contract"].content_hash() == passing["contract"].content_hash()
    # And the comparison fault survives the switch, disabled rather than forgotten.
    assert failing["report"].scenario.fault_profile == passing["report"].scenario.fault_profile
    assert failing["report"].scenario.fault_active is True
    assert passing["report"].scenario.fault_active is False
    # The original critical classification disappears, which is what §23.7 shows.
    assert failing["report"].primary_failure is not None
    assert passing["report"].primary_failure is None


@pytest.mark.integration
async def test_the_journey_is_reproducible(traced) -> None:
    """Same target, same contract, same inputs — same report bytes."""
    adapter, _ = traced
    first = await _journey(adapter, "pre_fix")
    second = await _journey(adapter, "pre_fix")
    assert (
        first["report"].canonical_document()["layers"]
        == second["report"].canonical_document()["layers"]
    )
    assert first["report"].primary_failure == second["report"].primary_failure


# --- the M2 exit gate, item by item -----------------------------------------


@pytest.mark.integration
async def test_the_consent_journey_creates_an_order_only_behind_an_approval(
    traced,
) -> None:
    """The third seeded contract, exercised through the adapter."""
    adapter, _ = traced
    await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode="post_fix"))
    provider = adapter.observation_provider()

    await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 1, "request_id": "req-000000000001"},
        _context(1),
    )

    opened = await adapter._client.post(
        "/demo/api/v1/store/checkout/confirmations",
        headers={"X-Workspace-Id": "ws-1"},
        json={},
    )
    confirmation_id = opened.json()["confirmation_id"]

    # Before approval, nothing exists.
    refused = await adapter.execute(
        "ws-1",
        "proceed_to_checkout",
        {"confirmation_id": confirmation_id, "request_id": "req-000000000009"},
        _context(9),
    )
    assert refused.terminal_event is OutcomeEventType.TOOL_INVOCATION_FAILED
    assert (await provider.capture("ws-1")).payload["order"]["created"] is False

    await adapter._client.post(
        f"/demo/api/v1/store/checkout/confirmations/{confirmation_id}/decision",
        headers={"X-Workspace-Id": "ws-1"},
        json={"approved": True},
    )
    ordered = await adapter.execute(
        "ws-1",
        "proceed_to_checkout",
        {"confirmation_id": confirmation_id, "request_id": "req-000000000010"},
        _context(10),
    )
    assert ordered.claims_success() is True
    assert (await provider.capture("ws-1")).payload["order"]["created"] is True


@pytest.mark.integration
async def test_a_normal_retry_returns_the_first_result_through_the_adapter(
    traced,
) -> None:
    """Exit gate: "normal retries return the first persisted result"."""
    adapter, _ = traced
    await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode="post_fix"))
    arguments = {"product_id": MUG, "quantity": 2, "request_id": "req-000000000001"}

    first = await adapter.execute("ws-1", "update_cart", arguments, _context(1))
    repeat = await adapter.execute("ws-1", "update_cart", arguments, _context(2))

    assert repeat.state_version_after == first.state_version_after
    observed = await adapter.observation_provider().capture("ws-1")
    assert observed.payload["cart"]["items"]["mug"]["quantity"] == 2
    assert observed.state_version == "2"


@pytest.mark.integration
async def test_a_conflicting_request_id_is_a_non_retryable_conflict(traced) -> None:
    """Exit gate: "conflicting request-ID reuse returns a non-retryable conflict"."""
    adapter, _ = traced
    await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode="post_fix"))
    await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 2, "request_id": "req-000000000001"},
        _context(1),
    )
    conflict = await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 5, "request_id": "req-000000000001"},
        _context(2),
    )
    assert conflict.terminal_event is OutcomeEventType.TOOL_INVOCATION_FAILED
    assert conflict.error_code == "idempotency_key_reused"

    observed = await adapter.observation_provider().capture("ws-1")
    assert observed.payload["cart"]["items"]["mug"]["quantity"] == 2
