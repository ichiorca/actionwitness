"""Port-boundary gates (spec v1.9 §9.1, §9.3, §13.4, FR-032; 002-T7).

The section that matters most is the last one. Constitution §4 requires
tool-reported output and authoritative observations to be distinct stored types,
and §4 again forbids persisting "a successful tool response as manufactured
observed state". A type system cannot stop someone copying a payload across by
hand, but it can guarantee the library offers no path that does it for them - so
these tests assert, by introspection, that no constructor, method, or helper on
either type accepts or produces the other.

The rest covers what the boundary must refuse: a scenario mode the adapter never
advertised, a read-only tool that claims effects, and a terminal invocation whose
reported status contradicts its event type.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from actionwitness_core import ports
from actionwitness_core.evidence.enums import EvidenceSourceClassification, ToolReportedStatus
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType
from actionwitness_core.kernel import ContractError
from actionwitness_core.ports.enums import ExecutionMode, RetrySemantics, SideEffectClass
from actionwitness_core.ports.models import (
    ExecutionContext,
    Observation,
    ScenarioSelection,
    TargetDescriptor,
    TargetToolSpec,
    ToolExecutionResult,
)
from pydantic import ValidationError

MANAGED = TargetDescriptor(
    target_type="managed_application",
    target_id="buggy-store",
    execution_mode=ExecutionMode.MANAGED,
    supported_scenario_modes=("pre_fix", "post_fix"),
)

EXTERNAL = TargetDescriptor(
    target_type="shopify_development_store",
    target_id="dev-store",
    execution_mode=ExecutionMode.EXTERNAL_WEBMCP,
    supported_scenario_modes=("external_current",),
)


def _observation(**overrides: object) -> Observation:
    fields: dict = {
        "namespace": "target",
        "provider_id": "fake_state",
        "provenance": "in_memory_fixture",
        "schema_version": "1.0",
        "payload": {"ticket": {"status": "open"}},
        "state_version": "3",
        "captured_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return Observation(**fields)


def _result(**overrides: object) -> ToolExecutionResult:
    fields: dict = {
        "tool_name": "update_cart",
        "terminal_event": OutcomeEventType.TOOL_INVOCATION_COMPLETED,
        "reported_status": ToolReportedStatus.SUCCESS,
        "reported_summary": "cart updated",
        "request_id": "req-1",
        "correlation_id": "corr-1",
    }
    fields.update(overrides)
    return ToolExecutionResult(**fields)


# --- descriptors and scenario selection (§9.1) ------------------------------


@pytest.mark.adapters
def test_a_scenario_mode_the_adapter_advertises_is_accepted() -> None:
    ScenarioSelection(scenario_mode="pre_fix").validate_for(MANAGED)


@pytest.mark.adapters
def test_a_scenario_mode_the_adapter_never_advertised_is_refused() -> None:
    """§9.1: the core validates the mode without interpreting its name."""
    with pytest.raises(ContractError, match="not supported by target"):
        ScenarioSelection(scenario_mode="pre_fix").validate_for(EXTERNAL)


@pytest.mark.adapters
def test_the_core_carries_no_knowledge_of_what_a_scenario_mode_means() -> None:
    """An adapter may advertise any token; `pre_fix` is not privileged."""
    support_target = TargetDescriptor(
        target_type="managed_application",
        target_id="support-desk",
        execution_mode=ExecutionMode.MANAGED,
        supported_scenario_modes=("baseline", "escalated"),
    )
    ScenarioSelection(scenario_mode="escalated").validate_for(support_target)


@pytest.mark.adapters
def test_a_descriptor_with_no_scenario_modes_is_refused() -> None:
    """Every run copies a mode in; a target offering none could never be armed."""
    with pytest.raises(ValidationError):
        TargetDescriptor(
            target_type="managed_application",
            target_id="t",
            execution_mode=ExecutionMode.MANAGED,
            supported_scenario_modes=(),
        )


@pytest.mark.adapters
def test_scenario_selection_carries_the_fault_profile_without_interpreting_it() -> None:
    selection = ScenarioSelection(
        scenario_mode="pre_fix", fault_profile="discount_reported_but_not_applied"
    )
    assert selection.fault_profile == "discount_reported_but_not_applied"


# --- tool specs (§9.1, §13.4) -----------------------------------------------


@pytest.mark.adapters
def test_a_mutating_tool_may_declare_effect_paths() -> None:
    spec = TargetToolSpec(
        name="update_cart",
        side_effect=SideEffectClass.MUTATING,
        retry=RetrySemantics.IDEMPOTENT_BY_REQUEST_ID,
        effect_paths=("target.cart.items", "target.cart.total"),
    )
    assert [str(path) for path in spec.effect_paths] == [
        "target.cart.items",
        "target.cart.total",
    ]


@pytest.mark.adapters
def test_a_read_only_tool_declaring_effects_is_refused() -> None:
    """§13.4 lists a read-only tool's declared effects as none.

    Port models are built in-process by an adapter author, so a rejection
    surfaces as Pydantic's `ValidationError` with the reason attached rather than
    as a structured contract error - the contract parser is where an untrusted
    *document* is turned into addressable detail.
    """
    with pytest.raises(ValidationError, match="read-only"):
        TargetToolSpec(
            name="get_cart",
            side_effect=SideEffectClass.READ_ONLY,
            retry=RetrySemantics.READ_ONLY_SAFE,
            effect_paths=("target.cart",),
        )


@pytest.mark.adapters
def test_a_read_only_tool_claiming_non_retryable_semantics_is_refused() -> None:
    with pytest.raises(ValidationError, match="read_only_safe"):
        TargetToolSpec(
            name="get_cart",
            side_effect=SideEffectClass.READ_ONLY,
            retry=RetrySemantics.NOT_RETRYABLE,
        )


@pytest.mark.adapters
def test_publishing_no_effect_paths_is_allowed() -> None:
    """§12.2: missing effect metadata disables causal attribution and nothing else."""
    spec = TargetToolSpec(
        name="apply_discount",
        side_effect=SideEffectClass.MUTATING,
        retry=RetrySemantics.NOT_RETRYABLE,
    )
    assert spec.effect_paths == ()


@pytest.mark.adapters
def test_a_tool_name_over_the_context_budget_is_refused() -> None:
    """§11.4 caps tool names at 30 characters."""
    with pytest.raises(ValidationError):
        TargetToolSpec(
            name="x" * 31,
            side_effect=SideEffectClass.MUTATING,
            retry=RetrySemantics.NOT_RETRYABLE,
        )


# --- execution context ------------------------------------------------------


@pytest.mark.adapters
def test_an_execution_context_requires_an_idempotency_key() -> None:
    """Constitution §5: every logical mutation has a stable idempotency key."""
    with pytest.raises(ValidationError):
        ExecutionContext(
            workspace_id="ws-1",
            run_id="run-1",
            invocation_id="inv-1",
            request_id="req-1",
            correlation_id="corr-1",
            actor=EventActor.AGENT,
        )


@pytest.mark.adapters
def test_an_execution_context_is_immutable() -> None:
    context = ExecutionContext(
        workspace_id="ws-1",
        run_id="run-1",
        invocation_id="inv-1",
        request_id="req-1",
        correlation_id="corr-1",
        idempotency_key="key-1",
        actor=EventActor.AGENT,
    )
    with pytest.raises(ValidationError):
        context.idempotency_key = "key-2"


# --- terminal invocation shape (FR-032) -------------------------------------


@pytest.mark.adapters
def test_a_completed_invocation_must_record_its_reported_status() -> None:
    with pytest.raises(ValidationError, match="must record its reported status"):
        _result(reported_status=None)


@pytest.mark.adapters
@pytest.mark.parametrize(
    "event",
    [OutcomeEventType.TOOL_INVOCATION_FAILED, OutcomeEventType.TOOL_INVOCATION_CANCELLED],
)
def test_a_failed_or_cancelled_invocation_carries_no_reported_status(
    event: OutcomeEventType,
) -> None:
    """FR-032: those event types carry their outcome in the event name."""
    assert _result(terminal_event=event, reported_status=None).reported_status is None
    with pytest.raises(ValidationError, match="carries no reported status"):
        _result(terminal_event=event, reported_status=ToolReportedStatus.SUCCESS)


@pytest.mark.adapters
@pytest.mark.parametrize(
    "event", [OutcomeEventType.TOOL_INVOCATION_STARTED, OutcomeEventType.RUN_ARMED]
)
def test_a_non_terminal_event_is_refused_as_a_result(event: OutcomeEventType) -> None:
    with pytest.raises(ValidationError, match="not a terminal invocation event"):
        _result(terminal_event=event, reported_status=None)


@pytest.mark.adapters
def test_a_reported_summary_over_the_tool_result_budget_is_refused() -> None:
    """§23.3: tool output carries a compact summary, not the evidence."""
    with pytest.raises(ValidationError):
        _result(reported_summary="x" * 1_501)


@pytest.mark.adapters
@pytest.mark.parametrize(
    "status,claims",
    [
        (ToolReportedStatus.SUCCESS, True),
        (ToolReportedStatus.ALREADY_APPLIED, False),
        (ToolReportedStatus.BLOCKED_BY_USER, False),
        (ToolReportedStatus.BLOCKED_BY_EXPIRY, False),
    ],
)
def test_only_a_success_report_is_a_claim_of_success(
    status: ToolReportedStatus, claims: bool
) -> None:
    """FR-055 uses this claim as one half of false-success detection."""
    assert _result(reported_status=status).claims_success() is claims


# --- observations (§9.3) ----------------------------------------------------


@pytest.mark.adapters
def test_an_observation_mounts_its_payload_under_its_declared_namespace() -> None:
    assert _observation().as_context() == {"target": {"ticket": {"status": "open"}}}


@pytest.mark.adapters
def test_state_version_is_metadata_and_not_part_of_the_asserted_payload() -> None:
    """§9.3: provider state_version "remains observation metadata"."""
    context = _observation(state_version="7").as_context()
    assert "state_version" not in context["target"]


@pytest.mark.adapters
def test_an_observation_from_a_provider_without_versions_is_still_valid() -> None:
    """§17.1 makes state_version nullable for exactly this case."""
    assert _observation(state_version=None).state_version is None


@pytest.mark.adapters
def test_observation_hashes_are_stable_and_value_sensitive() -> None:
    assert _observation().content_hash() == _observation().content_hash()
    assert (
        _observation(payload={"ticket": {"status": "closed"}}).content_hash()
        != _observation().content_hash()
    )


@pytest.mark.adapters
def test_an_observation_is_immutable() -> None:
    with pytest.raises(ValidationError):
        _observation().state_version = "9"


# --- the two channels stay separate (constitution §4) -----------------------


@pytest.mark.adapters
def test_the_two_records_carry_different_source_classifications() -> None:
    assert (
        _observation().source_classification
        is EvidenceSourceClassification.AUTHORITATIVE_OBSERVATION
    )
    assert _result().source_classification is EvidenceSourceClassification.TOOL_REPORTED


@pytest.mark.adapters
def test_source_classification_cannot_be_overridden_by_a_caller() -> None:
    """A settable label could be flipped, and the label is the guarantee."""
    with pytest.raises(ValidationError):
        Observation(
            namespace="target",
            provider_id="fake_state",
            provenance="in_memory_fixture",
            schema_version="1.0",
            payload={},
            captured_at=datetime(2026, 1, 1, tzinfo=UTC),
            source_classification="tool_reported",
        )
    assert "source_classification" not in Observation.model_fields
    assert "source_classification" not in ToolExecutionResult.model_fields


@pytest.mark.adapters
def test_neither_type_offers_a_conversion_into_the_other() -> None:
    """The library must offer no path that manufactures state from a self-report."""
    for owner, forbidden in (
        (Observation, ToolExecutionResult),
        (ToolExecutionResult, Observation),
    ):
        for name, member in inspect.getmembers(owner, callable):
            if name.startswith("_"):
                continue
            try:
                signature = inspect.signature(member)
            except (TypeError, ValueError):  # pragma: no cover - builtins
                continue
            annotations = [str(parameter.annotation) for parameter in signature.parameters.values()]
            annotations.append(str(signature.return_annotation))
            assert forbidden.__name__ not in " ".join(annotations), (
                f"{owner.__name__}.{name} references {forbidden.__name__}; the two "
                "channels must not be convertible"
            )


@pytest.mark.adapters
def test_an_observation_cannot_be_built_from_a_tool_result() -> None:
    """Constructing one from the other must be a type error, not a coercion."""
    with pytest.raises(ValidationError):
        Observation.model_validate(_result().model_dump())


# --- protocols --------------------------------------------------------------


@pytest.mark.adapters
def test_the_published_protocols_match_the_specified_surface() -> None:
    """§9.1 names these by name; renaming one breaks every adapter SDK reader."""
    for name in (
        "ObservationProvider",
        "TargetAdapter",
        "ManagedTargetAdapter",
        "ExternalTargetAdapter",
    ):
        assert hasattr(ports, name), f"ports must publish {name}"


@pytest.mark.adapters
def test_an_external_adapter_has_no_execute_method() -> None:
    """§9.1: an external target runs its own tools and is never impersonated."""
    assert not hasattr(ports.ExternalTargetAdapter, "execute")
    assert hasattr(ports.ManagedTargetAdapter, "execute")


@pytest.mark.adapters
@pytest.mark.parametrize(
    "protocol",
    ["ContractRepository", "SnapshotRepository", "EventRepository", "FindingRepository"],
)
def test_no_insert_only_repository_declares_a_mutation_method(protocol: str) -> None:
    """§17.1: "the repository exposes no update method for this table"."""
    members = {
        name
        for name, _ in inspect.getmembers(getattr(ports, protocol), callable)
        if not name.startswith("_")
    }
    forbidden = {
        name for name in members if name.startswith(("update", "delete", "set_", "remove"))
    }
    assert forbidden == set(), f"{protocol} exposes mutation methods: {sorted(forbidden)}"


@pytest.mark.adapters
def test_importing_the_ports_package_pulls_in_no_target_or_framework(monkeypatch) -> None:
    """The extension surface must be reachable with every integration absent."""
    import sys

    for name in list(sys.modules):
        if name.startswith("actionwitness_core"):
            del sys.modules[name]
    import actionwitness_core.ports  # noqa: F401

    leaked = [
        module
        for module in ("integrations", "buggy_store", "shopify", "fastapi", "httpx", "aiosqlite")
        if any(name == module or name.startswith(f"{module}.") for name in sys.modules)
    ]
    assert leaked == [], f"importing core ports pulled in {leaked}"
