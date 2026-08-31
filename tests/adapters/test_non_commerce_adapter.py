"""A non-commerce in-memory target, driven end to end through public protocols.

Spec v1.9 §9.3 ("the non-commerce test adapter supplies a different provider and
proves that paths such as `target.ticket.status` work without commerce code"),
§26.7 and AC-19 (reusable core and separate demo application), §9.1 (the adapter
protocols); 002-T12 and the spec 002 exit gate item 3.

This module is the evidence for the product's least visible claim: that the
engine is target-neutral. Everything else in the suite exercises the core with
contrived fixtures; here a *complete journey* runs against a support-desk target
that has no cart, no order, no discount, and no money - arm, invoke, observe,
evaluate, classify, report - touching nothing but `actionwitness_core.ports` and
the public engine.

Two things are deliberate.

**The scenario modes are named `working` and `reports_without_applying`.** Not
`pre_fix`/`post_fix`. §9.1 makes the mode an adapter-declared opaque token, so a
core that had quietly learned the Buggy Store's vocabulary would fail here rather
than in M2 where it would look like an integration bug.

**The fault is the same shape as the commerce one.** A tool reports success and
authoritative state disagrees. If false-success detection only worked for carts
it would be a demo, not a product.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.assertions import evaluate_assertions, evaluate_preconditions
from actionwitness_core.engine.classification import (
    classify_assertion_failures,
    tool_execution_layer,
)
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.engine.findings import Finding
from actionwitness_core.engine.policies import PolicyEvidence, evaluate_policies
from actionwitness_core.engine.trajectory import evaluate_expected_tools
from actionwitness_core.evidence.enums import ToolReportedStatus
from actionwitness_core.evidence.models import RunEvent, Snapshot
from actionwitness_core.journeys.enums import (
    EventActor,
    OutcomeEventType,
    RunState,
    SnapshotPhase,
)
from actionwitness_core.kernel import ContractError
from actionwitness_core.ports import ManagedTargetAdapter, ObservationProvider
from actionwitness_core.ports.enums import ExecutionMode, RetrySemantics, SideEffectClass
from actionwitness_core.ports.models import (
    ExecutionContext,
    Observation,
    ScenarioSelection,
    TargetDescriptor,
    TargetToolSpec,
    ToolExecutionResult,
)
from actionwitness_core.reports.enums import LayerResult, RunMode
from actionwitness_core.reports.models import (
    ContractReference,
    ScenarioReference,
    TargetReference,
    compose_outcome_report,
)
from actionwitness_core.security.canonical import content_hash

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

# --- the target -------------------------------------------------------------

DESCRIPTOR = TargetDescriptor(
    target_type="managed_application",
    target_id="support-desk",
    execution_mode=ExecutionMode.MANAGED,
    # Deliberately not pre_fix/post_fix: the core must not privilege the demo's
    # vocabulary (§9.1).
    supported_scenario_modes=("working", "reports_without_applying"),
)

TOOL_SPECS = (
    TargetToolSpec(
        name="get_ticket",
        description="Read the current ticket.",
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetrySemantics.READ_ONLY_SAFE,
    ),
    TargetToolSpec(
        name="set_ticket_status",
        description="Set the ticket's workflow status.",
        side_effect=SideEffectClass.MUTATING,
        retry=RetrySemantics.IDEMPOTENT_BY_REQUEST_ID,
        effect_paths=("target.ticket.status",),
    ),
    TargetToolSpec(
        name="archive_ticket",
        description="Archive the ticket. Irreversible, so it requires consent.",
        side_effect=SideEffectClass.PROTECTED_MUTATING,
        retry=RetrySemantics.NOT_RETRYABLE,
        effect_paths=("target.ticket.archived",),
    ),
)


@dataclass
class _SupportDeskState:
    """The target's own canonical state. No commerce concept appears in it."""

    status: str = "open"
    archived: bool = False
    version: int = 1
    #: A path no contract asserts, used to show a change nothing declared.
    watchers: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        return {
            "ticket": {
                "id": "T-1",
                "status": self.status,
                "archived": self.archived,
                "watchers": list(self.watchers),
            }
        }


class _SupportDeskObservationProvider:
    """An `ObservationProvider` over the in-memory state (§9.3).

    Reads the target's own state directly, never a tool's return value - which
    is the whole reason an observation can settle an assertion and a tool report
    cannot.
    """

    def __init__(self, target: SupportDeskAdapter) -> None:
        self._target = target

    async def capture(self, workspace_id: str) -> Observation:
        state = self._target.state_for(workspace_id)
        return Observation(
            namespace="target",
            provider_id="support_desk_state",
            provenance="in_memory_service_state",
            schema_version="1.0",
            payload=state.payload(),
            state_version=str(state.version),
            captured_at=self._target.now(),
        )


class SupportDeskAdapter:
    """A `ManagedTargetAdapter` for a target with no commerce semantics.

    Implements the protocol structurally rather than by inheritance, which is the
    point of §9.1's `Protocol`: an integration author writes their own class and
    the core never learns its type.
    """

    descriptor = DESCRIPTOR

    def __init__(self, *, clock_start: datetime = EPOCH) -> None:
        self._states: dict[str, _SupportDeskState] = {}
        self._scenarios: dict[str, ScenarioSelection] = {}
        self._now = clock_start
        self._tick = 0

    # -- injected clock, so the whole journey is replayable -----------------
    def now(self) -> datetime:
        self._tick += 1
        return self._now + timedelta(seconds=self._tick)

    def state_for(self, workspace_id: str) -> _SupportDeskState:
        return self._states[workspace_id]

    # -- TargetAdapter ------------------------------------------------------
    def tool_specs(self) -> Sequence[TargetToolSpec]:
        return TOOL_SPECS

    def effect_map(self) -> Mapping[str, tuple[str, ...]]:
        return {spec.name: tuple(str(path) for path in spec.effect_paths) for spec in TOOL_SPECS}

    def observation_provider(self) -> ObservationProvider:
        return _SupportDeskObservationProvider(self)

    # -- ManagedTargetAdapter ----------------------------------------------
    async def prepare(self, workspace_id: str, fixture: dict, scenario: ScenarioSelection) -> None:
        scenario.validate_for(self.descriptor)
        self._states[workspace_id] = _SupportDeskState(
            status=fixture.get("status", "open"),
            watchers=list(fixture.get("watchers", [])),
        )
        self._scenarios[workspace_id] = scenario

    async def execute(
        self, workspace_id: str, tool_name: str, arguments: dict, context: ExecutionContext
    ) -> ToolExecutionResult:
        if tool_name not in {spec.name for spec in TOOL_SPECS}:
            raise ContractError(f"{tool_name!r} is not an allowlisted tool")
        state = self._states[workspace_id]
        scenario = self._scenarios[workspace_id]
        before_version = state.version

        if tool_name == "set_ticket_status":
            # The fault, in non-commerce clothing: report success, change
            # nothing. Exactly the shape of the Buggy Store discount defect.
            if scenario.scenario_mode != "reports_without_applying":
                state.status = arguments["status"]
                state.version += 1
        elif tool_name == "archive_ticket":
            state.archived = True
            state.version += 1

        return ToolExecutionResult(
            tool_name=tool_name,
            terminal_event=OutcomeEventType.TOOL_INVOCATION_COMPLETED,
            reported_status=ToolReportedStatus.SUCCESS,
            reported_summary=f"{tool_name} ok",
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            state_version_before=str(before_version),
            state_version_after=str(state.version),
        )

    def state_hash(self, workspace_id: str) -> str:
        return content_hash(self._states[workspace_id].payload())


# --- the contract, in support-desk vocabulary -------------------------------

CONTRACT_DOCUMENT = {
    "schema_version": "1.0",
    "name": "resolve-one-ticket",
    "target_id": "support-desk",
    "intent": "Set the ticket to resolved without archiving it.",
    "preconditions": [{"path": "target.ticket.status", "operator": "equals", "value": "open"}],
    "expected_tools": {"ordered": True, "calls": ["set_ticket_status"]},
    "assertions": [
        {
            "id": "ticket-resolved",
            "path": "target.ticket.status",
            "operator": "equals",
            "value": "resolved",
            "severity": "critical",
        },
        {
            "id": "not-archived",
            "path": "target.ticket.archived",
            "operator": "equals",
            "value": False,
            "severity": "critical",
        },
    ],
    "policies": [
        {"type": "idempotent_by_request_id", "tool": "set_ticket_status"},
        {"type": "requires_confirmation", "tool": "archive_ticket"},
    ],
}


def _context(sequence: int) -> ExecutionContext:
    return ExecutionContext(
        workspace_id="ws-1",
        run_id="run-1",
        invocation_id=f"inv-{sequence}",
        request_id=f"req-{sequence}",
        correlation_id=f"corr-{sequence}",
        idempotency_key=f"key-{sequence}",
        actor=EventActor.AGENT,
    )


async def _journey(scenario_mode: str) -> dict:
    """Arm, invoke, observe, evaluate, classify, and report - through ports only."""
    adapter = SupportDeskAdapter()
    contract = parse_contract(CONTRACT_DOCUMENT)
    contract.validate_against_target(
        target_id=adapter.descriptor.target_id,
        tool_names=[spec.name for spec in adapter.tool_specs()],
        protected_tools=[
            spec.name
            for spec in adapter.tool_specs()
            if spec.side_effect is SideEffectClass.PROTECTED_MUTATING
        ],
    )

    scenario = ScenarioSelection(scenario_mode=scenario_mode)
    await adapter.prepare("ws-1", {"status": "open"}, scenario)
    provider = adapter.observation_provider()

    # Arm: one authoritative observation, then preconditions against that value.
    initial = Snapshot.of("run-1", SnapshotPhase.BEFORE, await provider.capture("ws-1"))
    precondition_findings = evaluate_preconditions(
        contract.preconditions, initial=initial.as_context()
    )
    assert all(f.status is CheckStatus.PASSED for f in precondition_findings)

    # Act: one recorded invocation with its immediate post-call observation.
    context = _context(1)
    before_hash = adapter.state_hash("ws-1")
    result = await adapter.execute("ws-1", "set_ticket_status", {"status": "resolved"}, context)
    after_call = await provider.capture("ws-1")
    events = (
        RunEvent(
            sequence_number=1,
            event_type=OutcomeEventType.TOOL_INVOCATION_STARTED,
            actor=EventActor.AGENT,
            created_at=EPOCH,
            tool_name="set_ticket_status",
            correlation_id=context.correlation_id,
            request_id=context.request_id,
        ),
        RunEvent(
            sequence_number=2,
            event_type=result.terminal_event,
            actor=EventActor.AGENT,
            created_at=EPOCH + timedelta(seconds=1),
            tool_name=result.tool_name,
            correlation_id=result.correlation_id,
            request_id=result.request_id,
            reported_status=result.reported_status,
            state_hash_before=before_hash,
            state_hash_after=adapter.state_hash("ws-1"),
            post_call_effect_state=after_call.as_context(),
        ),
    )

    # Verify: a second authoritative observation, then the whole engine.
    final = Snapshot.of("run-1", SnapshotPhase.AFTER, await provider.capture("ws-1"))
    assertion_findings = classify_assertion_failures(
        evaluate_assertions(
            contract.assertions, initial=initial.as_context(), final=final.as_context()
        ),
        contract.assertions,
        events=events,
        effect_map={
            tool: tuple(ObservationPath.parse(path) for path in paths)
            for tool, paths in adapter.effect_map().items()
        },
        initial=initial.as_context(),
    )
    trajectory = evaluate_expected_tools(contract.expected_tools, events)
    policy_findings = evaluate_policies(contract.policies, PolicyEvidence(events=events))

    report = compose_outcome_report(
        run_id="run-1",
        target=TargetReference(id="support-desk", adapter_id="tests.adapters.support_desk"),
        scenario=ScenarioReference(mode=scenario_mode),
        contract=ContractReference(
            id="contract-1",
            schema_version=contract.schema_version,
            content_hash=contract.content_hash(),
        ),
        assertion_findings=assertion_findings,
        policy_findings=policy_findings,
        trajectory_finding=trajectory,
        tool_execution=tool_execution_layer(events),
        events=events,
    )
    return {
        "adapter": adapter,
        "contract": contract,
        "result": result,
        "initial": initial,
        "final": final,
        "assertions": assertion_findings,
        "policies": policy_findings,
        "trajectory": trajectory,
        "report": report,
    }


# --- the adapter satisfies the published protocols --------------------------


@pytest.mark.adapters
def test_the_fake_target_satisfies_the_managed_adapter_protocol() -> None:
    """§9.1's protocols are structural: an author writes a class, not a subclass."""
    adapter = SupportDeskAdapter()
    assert isinstance(adapter, ManagedTargetAdapter)
    assert isinstance(adapter.observation_provider(), ObservationProvider)


@pytest.mark.adapters
def test_this_module_imports_nothing_but_the_core() -> None:
    """AC-19: the non-commerce path must not need an integration or a framework."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    roots = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert (
        roots
        & {
            "integrations",
            "buggy_store",
            "shopify",
            "fastapi",
            "httpx",
            "aiosqlite",
            "actionwitness_service",
        }
        == set()
    )


@pytest.mark.adapters
def test_the_adapter_publishes_effect_metadata_for_its_mutating_tools() -> None:
    """§13.4: a read-only tool declares none; a mutating one declares its prefixes."""
    effects = SupportDeskAdapter().effect_map()
    assert effects["get_ticket"] == ()
    assert effects["set_ticket_status"] == ("target.ticket.status",)


@pytest.mark.adapters
async def test_the_adapter_refuses_a_scenario_mode_it_never_advertised() -> None:
    adapter = SupportDeskAdapter()
    with pytest.raises(ContractError, match="not supported by target"):
        await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode="pre_fix"))


@pytest.mark.adapters
async def test_the_adapter_refuses_a_tool_outside_its_allowlist() -> None:
    adapter = SupportDeskAdapter()
    await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode="working"))
    with pytest.raises(ContractError, match="not an allowlisted tool"):
        await adapter.execute("ws-1", "delete_everything", {}, _context(1))


# --- the journey, on a target with no commerce vocabulary -------------------


@pytest.mark.adapters
async def test_a_non_commerce_path_is_evaluated_end_to_end() -> None:
    """The exit-gate item: `target.ticket.status` resolves through public protocols."""
    journey = await _journey("working")
    report = journey["report"]

    assert [str(f.path) for f in journey["assertions"]] == [
        "target.ticket.status",
        "target.ticket.archived",
    ]
    assert all(f.status is CheckStatus.PASSED for f in journey["assertions"])
    assert report.status is RunState.PASSED
    assert report.layers.business_outcome is LayerResult.PASSED
    assert report.layers.observed_trajectory is LayerResult.PASSED
    assert report.layers.tool_execution is LayerResult.PASSED
    assert report.layers.model_tool_selection is LayerResult.NOT_EVALUATED


@pytest.mark.adapters
async def test_a_reported_success_is_contradicted_by_independent_observation() -> None:
    """False-success detection is not a commerce feature."""
    journey = await _journey("reports_without_applying")
    report = journey["report"]

    assert journey["result"].claims_success() is True
    assert journey["final"].observation.payload["ticket"]["status"] == "open"

    failed = [f for f in journey["assertions"] if f.failed]
    assert len(failed) == 1
    assert failed[0].classification is FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH
    assert failed[0].attributed_cause["tool_name"] == "set_ticket_status"

    # The layered claim, on a target with no cart: the call executed, the
    # trajectory conformed, and the business outcome is still wrong.
    assert report.layers.tool_execution is LayerResult.PASSED
    assert report.layers.observed_trajectory is LayerResult.PASSED
    assert report.layers.business_outcome is LayerResult.FAILED
    assert report.status is RunState.FAILED
    assert report.primary_failure is FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH


@pytest.mark.adapters
async def test_the_observation_and_the_tool_report_stay_distinguishable() -> None:
    """Constitution §4, proved on a live journey rather than in the abstract."""
    journey = await _journey("reports_without_applying")
    observation = journey["final"].observation
    result = journey["result"]

    assert observation.source_classification.value == "authoritative_observation"
    assert result.source_classification.value == "tool_reported"
    assert observation.provider_id == "support_desk_state"
    assert observation.provenance == "in_memory_service_state"


@pytest.mark.adapters
async def test_an_unattempted_protected_tool_leaves_the_safety_layer_passing() -> None:
    """The contract requires consent for archiving; the journey never archives."""
    journey = await _journey("working")
    consent = next(f for f in journey["policies"] if f.check_id == "requires_confirmation")
    assert consent.status is CheckStatus.PASSED
    assert journey["report"].layers.safety_policy is LayerResult.PASSED


@pytest.mark.adapters
async def test_the_snapshot_hash_still_describes_its_observation() -> None:
    journey = await _journey("working")
    assert journey["initial"].verify() is True
    assert journey["final"].verify() is True
    assert journey["initial"].provider == "support_desk_state"


@pytest.mark.adapters
async def test_the_same_journey_produces_a_byte_identical_report() -> None:
    """Spec 002 exit gate 5, exercised through the full path rather than a stub."""
    first = await _journey("reports_without_applying")
    second = await _journey("reports_without_applying")
    assert first["report"].canonical_document() == second["report"].canonical_document()
    assert first["report"].content_hash() == second["report"].content_hash()


@pytest.mark.adapters
async def test_the_two_scenario_modes_produce_different_verdicts_from_one_contract() -> None:
    """AC-20 in miniature: the same controlled inputs, one differing mode."""
    working = await _journey("working")
    broken = await _journey("reports_without_applying")
    assert working["contract"].content_hash() == broken["contract"].content_hash()
    assert working["report"].status is RunState.PASSED
    assert broken["report"].status is RunState.FAILED


@pytest.mark.adapters
async def test_a_proposal_over_the_same_target_carries_no_verdict() -> None:
    """§23.1: a proposal run judges nothing, whatever the target."""
    journey = await _journey("working")
    proposal = compose_outcome_report(
        run_id="run-2",
        target=TargetReference(id="support-desk", adapter_id="tests.adapters.support_desk"),
        scenario=ScenarioReference(mode="working"),
        contract=ContractReference(
            id="contract-1",
            schema_version="1.0",
            content_hash=journey["contract"].content_hash(),
        ),
        mode=RunMode.PROPOSAL,
    )
    assert proposal.status is RunState.PROPOSED
    assert proposal.primary_failure is None


@pytest.mark.adapters
async def test_an_undeclared_change_is_visible_on_a_non_commerce_target() -> None:
    """§9.10 partitions by declared effect, not by knowing what a ticket is."""
    from actionwitness_core.contracts.models import NoUndeclaredChangesPolicy

    journey = await _journey("working")
    finding: Finding = evaluate_policies(
        [NoUndeclaredChangesPolicy()],
        PolicyEvidence(
            events=(
                RunEvent(
                    sequence_number=1,
                    event_type=OutcomeEventType.TOOL_INVOCATION_STARTED,
                    actor=EventActor.AGENT,
                    created_at=EPOCH,
                    tool_name="set_ticket_status",
                ),
            ),
            effect_map={"set_ticket_status": (ObservationPath.parse("target.ticket.status"),)},
            contract_paths=tuple(assertion.path for assertion in journey["contract"].assertions),
            changed_paths=(
                ObservationPath.parse("target.ticket.status"),
                ObservationPath.parse("target.ticket.watchers"),
            ),
        ),
    )[0]
    assert finding.status is CheckStatus.FAILED
    assert [str(path) for path in finding.paths] == ["target.ticket.watchers"]
