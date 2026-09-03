"""Layered-report gates (spec v1.9 §23.1, §17.2, FR-070/073; 002-T11).

Two properties carry this module.

**The five layers stay distinct.** BUILD_ORDER invariant 10 makes that a release
gate, and §23.1 gives each layer its own closed value set. The tests assert both
directions: that a permitted value is accepted and that a value the layer may not
report is refused, because a layer quietly accepting `not_evaluated` is how an
unevaluated policy ends up looking satisfied.

**Byte-identical serialization.** Spec 002's exit gate is "same inputs and hashes
produce byte-identical reports". A report whose bytes depend on member order,
dictionary iteration, or the order findings were evaluated in cannot anchor an
evidence chain, so the determinism tests here compare bytes rather than objects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from actionwitness_core.contracts.enums import AssertionSeverity
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.engine.findings import Finding
from actionwitness_core.evidence.models import RunEvent
from actionwitness_core.journeys.enums import (
    EventActor,
    GuidanceActor,
    OutcomeEventType,
    RunState,
)
from actionwitness_core.reports.enums import (
    ALLOWED_LAYER_RESULTS,
    LayerResult,
    ReportLayer,
    RunMode,
)
from actionwitness_core.reports.models import (
    ContractReference,
    ExternalCaptureReference,
    ExternalTargetReference,
    GuidanceReference,
    LayeredResult,
    OutcomeReport,
    ScenarioReference,
    TargetReference,
    UndeclaredChangesBlock,
    compose_outcome_report,
    undeclared_changes_from,
)
from actionwitness_core.security.canonical import canonicalize
from pydantic import ValidationError

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
CONTRACT_HASH = "sha256:" + "1" * 64

TARGET = TargetReference(id="buggy-store", adapter_id="integrations.buggy_store")
SCENARIO = ScenarioReference(
    mode="pre_fix", fault_profile="discount_reported_but_not_applied", fault_active=True
)
CONTRACT = ContractReference(id="contract_1", schema_version="1.0", content_hash=CONTRACT_HASH)


def _finding(check_id: str, status: CheckStatus, severity: AssertionSeverity, **extra) -> Finding:
    return Finding(
        check_id=check_id,
        check_type=CheckType.ASSERTION,
        status=status,
        severity=severity,
        **extra,
    )


def _event(sequence: int, event_type: OutcomeEventType, **extra: object) -> RunEvent:
    fields: dict = {
        "sequence_number": sequence,
        "event_type": event_type,
        "actor": EventActor.AGENT,
        "created_at": EPOCH + timedelta(seconds=sequence),
    }
    fields.update(extra)
    return RunEvent(**fields)


def _report(**overrides) -> OutcomeReport:
    fields: dict = {
        "run_id": "run_1",
        "target": TARGET,
        "scenario": SCENARIO,
        "contract": CONTRACT,
    }
    fields.update(overrides)
    return compose_outcome_report(**fields)


# --- the five layers stay distinct (BUILD_ORDER invariant 10) ---------------


@pytest.mark.unit
def test_a_report_carries_all_five_layers_separately() -> None:
    document = _report().canonical_document()
    assert set(document["layers"]) == {layer.value for layer in ReportLayer}


@pytest.mark.unit
@pytest.mark.parametrize(
    "layer,value",
    [(layer, value) for layer, values in ALLOWED_LAYER_RESULTS.items() for value in values],
    ids=lambda arg: arg.value,
)
def test_every_permitted_layer_value_is_accepted(layer: ReportLayer, value: LayerResult) -> None:
    assert getattr(LayeredResult(**{layer.value: value}), layer.value) is value


@pytest.mark.unit
@pytest.mark.parametrize(
    "layer,value",
    [
        (layer, value)
        for layer, values in ALLOWED_LAYER_RESULTS.items()
        for value in set(LayerResult) - values
    ],
    ids=lambda arg: arg.value,
)
def test_a_layer_refuses_a_value_the_spec_does_not_permit_it(
    layer: ReportLayer, value: LayerResult
) -> None:
    """A layer quietly widening its value set is how an unevaluated check hides."""
    with pytest.raises(ValidationError, match="may not report"):
        LayeredResult(**{layer.value: value})


@pytest.mark.unit
def test_the_source_report_never_claims_a_model_selection_result() -> None:
    """§23.1: a Tier 2 import "does not update this field or the source report hash"."""
    report = _report(
        assertion_findings=[_finding("a", CheckStatus.PASSED, AssertionSeverity.CRITICAL)]
    )
    assert report.layers.model_tool_selection is LayerResult.NOT_EVALUATED
    # There is deliberately no parameter for it: the source report cannot be
    # composed with a call-level result, so a Tier 2 import cannot reach in.
    with pytest.raises(TypeError):
        compose_outcome_report(  # type: ignore[call-arg]
            run_id="run_1",
            target=TARGET,
            scenario=SCENARIO,
            contract=CONTRACT,
            model_tool_selection=LayerResult.PASSED,
        )


@pytest.mark.unit
def test_the_headline_journey_shows_execution_passing_while_outcome_fails() -> None:
    """The M4 exit gate in miniature: a successful call, a wrong business result."""
    report = _report(
        assertion_findings=[
            _finding(
                "discounted-total",
                CheckStatus.FAILED,
                AssertionSeverity.CRITICAL,
                classification=FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH,
                causal_event_sequence=2,
            )
        ],
        trajectory_finding=Finding(
            check_id="expected_tools",
            check_type=CheckType.EXPECTED_TOOLS,
            status=CheckStatus.PASSED,
            severity=AssertionSeverity.CRITICAL,
        ),
        tool_execution=LayerResult.PASSED,
    )
    assert report.layers.observed_trajectory is LayerResult.PASSED
    assert report.layers.tool_execution is LayerResult.PASSED
    assert report.layers.business_outcome is LayerResult.FAILED
    assert report.layers.model_tool_selection is LayerResult.NOT_EVALUATED
    assert report.status is RunState.FAILED
    assert report.primary_failure is FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH


# --- composition derives, never decides -------------------------------------


@pytest.mark.unit
def test_an_omitted_trajectory_term_leaves_the_layer_not_evaluated() -> None:
    assert _report().layers.observed_trajectory is LayerResult.NOT_EVALUATED


@pytest.mark.unit
def test_warning_only_failures_yield_passed_with_warnings() -> None:
    report = _report(
        assertion_findings=[_finding("a", CheckStatus.FAILED, AssertionSeverity.WARNING)]
    )
    assert report.status is RunState.PASSED_WITH_WARNINGS
    assert report.layers.business_outcome is LayerResult.PASSED_WITH_WARNINGS
    assert report.counts.warnings == 1


@pytest.mark.unit
def test_a_failing_policy_fails_the_run_through_the_safety_layer() -> None:
    report = _report(
        policy_findings=[
            Finding(
                check_id="requires_confirmation",
                check_type=CheckType.POLICY,
                status=CheckStatus.FAILED,
                severity=AssertionSeverity.CRITICAL,
                classification=FailureClassification.MISSING_CONFIRMATION,
                causal_event_sequence=4,
            )
        ]
    )
    assert report.layers.safety_policy is LayerResult.FAILED
    assert report.layers.business_outcome is LayerResult.PASSED
    assert report.status is RunState.FAILED
    assert report.primary_failure is FailureClassification.MISSING_CONFIRMATION


@pytest.mark.unit
def test_counts_are_derived_from_the_recorded_timeline() -> None:
    events = [
        _event(1, OutcomeEventType.TOOL_INVOCATION_STARTED, tool_name="update_cart"),
        _event(2, OutcomeEventType.TOOL_INVOCATION_STARTED, tool_name="apply_discount"),
        _event(3, OutcomeEventType.CONFIRMATION_APPROVED, actor=EventActor.HUMAN),
        _event(4, OutcomeEventType.GUIDANCE_TRANSITIONED, actor=EventActor.HARNESS),
        _event(5, OutcomeEventType.GUIDANCE_TRANSITIONED, actor=EventActor.HARNESS),
    ]
    counts = _report(events=events).counts
    assert counts.tool_calls == 2
    assert counts.human_confirmations == 1
    assert counts.guidance_handoffs == 2


@pytest.mark.unit
def test_a_replayed_call_is_not_counted_as_an_agent_tool_call() -> None:
    """§23.1 keeps actor-`eval` starts in the eval report as replayed calls."""
    events = [
        _event(
            1,
            OutcomeEventType.TOOL_INVOCATION_STARTED,
            tool_name="update_cart",
            actor=EventActor.EVAL,
        )
    ]
    assert _report(events=events).counts.tool_calls == 0


@pytest.mark.unit
def test_primary_failure_follows_the_spec_ordering_across_layers() -> None:
    """§22 orders by severity, then causal sequence, then check ID - across all findings."""
    report = _report(
        assertion_findings=[
            _finding(
                "z-assertion",
                CheckStatus.FAILED,
                AssertionSeverity.CRITICAL,
                classification=FailureClassification.ASSERTION_MISMATCH,
                causal_event_sequence=9,
            )
        ],
        policy_findings=[
            Finding(
                check_id="forbidden_tool",
                check_type=CheckType.POLICY,
                status=CheckStatus.FAILED,
                severity=AssertionSeverity.CRITICAL,
                classification=FailureClassification.UNEXPECTED_TOOL,
                causal_event_sequence=2,
            )
        ],
    )
    assert report.primary_failure is FailureClassification.UNEXPECTED_TOOL


# --- proposal mode (§23.1, §16) ---------------------------------------------


@pytest.mark.unit
def test_a_proposal_report_carries_no_verdict() -> None:
    report = _report(mode=RunMode.PROPOSAL)
    assert report.status is RunState.PROPOSED
    assert report.layers.business_outcome is LayerResult.NOT_EVALUATED
    assert report.primary_failure is None


@pytest.mark.unit
def test_a_proposal_report_that_claims_a_verdict_is_refused() -> None:
    with pytest.raises(ValidationError, match="carries no verdict"):
        OutcomeReport(
            run_id="run_1",
            status=RunState.PROPOSED,
            mode=RunMode.PROPOSAL,
            target=TARGET,
            scenario=SCENARIO,
            contract=CONTRACT,
            layers=LayeredResult(),
            primary_failure=FailureClassification.ASSERTION_MISMATCH,
        )


@pytest.mark.unit
def test_a_verification_report_must_be_finalized_in_a_terminal_verdict_state() -> None:
    with pytest.raises(ValidationError, match="terminal verdict state"):
        OutcomeReport(
            run_id="run_1",
            status=RunState.RUNNING,
            target=TARGET,
            scenario=SCENARIO,
            contract=CONTRACT,
            layers=LayeredResult(),
        )


# --- undeclared changes (§9.10, §23.1) --------------------------------------


@pytest.mark.unit
def test_the_undeclared_block_is_absent_when_the_policy_did_not_run() -> None:
    """An empty block would read as "nothing was undeclared"."""
    assert "undeclared_changes" not in _report().canonical_document()


@pytest.mark.unit
def test_an_undeclared_change_block_reports_its_waivers_and_metadata() -> None:
    from actionwitness_core.contracts.paths import ObservationPath

    finding = Finding(
        check_id="no_undeclared_changes",
        check_type=CheckType.POLICY,
        status=CheckStatus.FAILED,
        severity=AssertionSeverity.CRITICAL,
        classification=FailureClassification.UNDECLARED_STATE_CHANGE,
        paths=(ObservationPath.parse("target.preferences.delivery_note"),),
        applied_exemptions=(ObservationPath.parse("target.cart.updated_at"),),
        evidence={"effect_metadata_published": True},
    )
    block = undeclared_changes_from(finding, changed_paths=7)
    assert block.changed_paths == 7
    assert block.undeclared == 1
    assert block.declared == 6
    assert block.effect_metadata_published is True

    document = _report(undeclared_changes=block).canonical_document()["undeclared_changes"]
    assert document["applied_exemptions"] == ["target.cart.updated_at"]
    # §23.1 renders each entry as an object, not a bare path. This finding
    # recorded no per-path evidence, so the entry says so rather than inventing
    # values: both sides `null`, and FR-159's ordinary `none` as the cause.
    assert document["paths"] == [
        {
            "path": "target.preferences.delivery_note",
            "before": None,
            "after": None,
            "attributed_cause": "none",
        }
    ]


# --- external-target provenance (§23.9, FR-117) -----------------------------


@pytest.mark.unit
def test_an_external_target_report_carries_complete_canonical_provenance() -> None:
    external = ExternalTargetReference(
        target_type="shopify_development_store",
        origin="https://dev-store.myshopify.com",
        pairing_id="pair_1",
        bridge_version="1.0.0",
        theme_build_id="theme-build-7",
        observation_provider="shopify_cart_state",
        provenance="platform_session_api",
        before=ExternalCaptureReference(
            path="/en/cart.js", captured_at=EPOCH, content_hash="sha256:" + "2" * 64
        ),
        after=ExternalCaptureReference(
            path="/en/cart.js",
            captured_at=EPOCH + timedelta(seconds=1),
            content_hash="sha256:" + "3" * 64,
        ),
        safe_scope_result=LayerResult.PASSED,
    )

    document = _report(external_target=external).canonical_document()

    assert document["schema_version"] == "1.2"
    assert document["external_target"] == {
        "target_type": "shopify_development_store",
        "origin": "https://dev-store.myshopify.com",
        "pairing_id": "pair_1",
        "bridge_version": "1.0.0",
        "theme_build_id": "theme-build-7",
        "observation_provider": "shopify_cart_state",
        "provenance": "platform_session_api",
        "captures": {
            "before": {
                "path": "/en/cart.js",
                "captured_at": "2026-01-01T00:00:00Z",
                "content_hash": "sha256:" + "2" * 64,
            },
            "after": {
                "path": "/en/cart.js",
                "captured_at": "2026-01-01T00:00:01Z",
                "content_hash": "sha256:" + "3" * 64,
            },
        },
        "safe_scope_result": "passed",
    }


@pytest.mark.unit
@pytest.mark.parametrize("path", ["cart.js", "/cart.js?token=secret", "/cart.js#secret"])
def test_an_external_capture_refuses_noncanonical_or_sensitive_paths(path: str) -> None:
    with pytest.raises(ValidationError, match=r"absolute.*no query or fragment"):
        ExternalCaptureReference(path=path, captured_at=EPOCH, content_hash="sha256:" + "2" * 64)


# --- byte-identical serialization (002 exit gate 5) -------------------------


@pytest.mark.unit
def test_the_same_inputs_produce_byte_identical_reports() -> None:
    findings = [
        _finding(
            "a",
            CheckStatus.FAILED,
            AssertionSeverity.CRITICAL,
            classification=FailureClassification.ASSERTION_MISMATCH,
            causal_event_sequence=1,
        )
    ]
    first = _report(assertion_findings=findings)
    second = _report(assertion_findings=findings)
    assert canonicalize(first.canonical_document()) == canonicalize(second.canonical_document())
    assert first.content_hash() == second.content_hash()


@pytest.mark.unit
def test_the_order_findings_were_evaluated_in_does_not_change_the_bytes() -> None:
    """A hash that depended on evaluation order could not anchor an evidence chain."""
    a = _finding(
        "a",
        CheckStatus.FAILED,
        AssertionSeverity.CRITICAL,
        classification=FailureClassification.ASSERTION_MISMATCH,
        causal_event_sequence=1,
    )
    b = _finding(
        "b",
        CheckStatus.FAILED,
        AssertionSeverity.WARNING,
        classification=FailureClassification.ASSERTION_MISMATCH,
        causal_event_sequence=2,
    )
    forward = _report(assertion_findings=[a, b])
    backward = _report(assertion_findings=[b, a])
    assert canonicalize(forward.canonical_document()) == canonicalize(backward.canonical_document())


@pytest.mark.unit
def test_a_changed_layer_changes_the_hash() -> None:
    passing = _report(
        assertion_findings=[_finding("a", CheckStatus.PASSED, AssertionSeverity.CRITICAL)]
    )
    failing = _report(
        assertion_findings=[
            _finding(
                "a",
                CheckStatus.FAILED,
                AssertionSeverity.CRITICAL,
                classification=FailureClassification.ASSERTION_MISMATCH,
            )
        ]
    )
    assert passing.content_hash() != failing.content_hash()


@pytest.mark.unit
def test_the_stored_document_carries_a_hash_a_reader_can_recompute() -> None:
    """§17.2: the artifact hash excludes its own top-level content_hash member."""
    from actionwitness_core.security.canonical import document_content_hash

    report = _report()
    stored = report.as_stored_document()
    assert stored["content_hash"] == report.content_hash()
    assert document_content_hash(stored) == report.content_hash()


@pytest.mark.unit
def test_a_report_is_immutable() -> None:
    report = _report()
    with pytest.raises(ValidationError):
        report.status = RunState.PASSED


@pytest.mark.unit
def test_guidance_at_finalization_is_carried_when_present() -> None:
    """§23.8: the visible report and the WebMCP next_action share one record."""
    guidance = GuidanceReference(
        actor=GuidanceActor.OPERATOR,
        action="create_regression_eval",
        reason="Preserve this failure before switching scenario mode.",
    )
    document = _report(guidance_at_finalization=guidance).canonical_document()
    assert document["guidance_at_finalization"] == {
        "actor": "operator",
        "action": "create_regression_eval",
        "reason": "Preserve this failure before switching scenario mode.",
    }


@pytest.mark.unit
def test_the_report_document_matches_the_spec_field_set() -> None:
    """A field added without a spec line would silently change every stored hash."""
    document = _report(
        undeclared_changes=UndeclaredChangesBlock(),
        guidance_at_finalization=GuidanceReference(actor=GuidanceActor.SYSTEM, action="idle"),
    ).canonical_document()
    assert set(document) == {
        "schema_version",
        "run_id",
        "status",
        "target",
        "scenario",
        "contract",
        "layers",
        "mode",
        "counts",
        "undeclared_changes",
        "guidance_at_finalization",
        "primary_failure",
    }


@pytest.mark.unit
def test_a_report_document_can_be_canonicalized_and_hashed() -> None:
    """Nothing in the document may be a type the canonicalizer refuses."""
    report = _report(
        assertion_findings=[_finding("a", CheckStatus.PASSED, AssertionSeverity.CRITICAL)],
        undeclared_changes=UndeclaredChangesBlock(),
    )
    assert report.content_hash().startswith("sha256:")
    assert canonicalize(report.canonical_document())
