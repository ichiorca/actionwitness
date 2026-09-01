"""Closed-vocabulary gates (spec v1.9 §9.4, §9.5, §22, §23.1; 002-T2).

`tests/unit/test_registry.py` proves the registry is *complete and documented*.
This module proves it is *right*: that the operator set is the eight §9.4 names
and not seven, that the classification set is the twelve of §22, and that each
report layer accepts exactly the values §23.1 permits it.

Exhaustiveness is asserted by comparing whole sets against literals transcribed
from the specification. A test that iterated the enum to build its expectation
would pass no matter what the enum contained, which is the failure mode worth
guarding here: these sets are compared *exactly* by eval expectations (§24.1,
AC-15), so a member quietly added or dropped changes what a regression case
means.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.enums import (
    OPERATORS_REQUIRING_VALUE,
    PRECONDITION_OPERATORS,
    TRANSITION_OPERATORS,
    AssertionOperator,
    AssertionSeverity,
    PolicyType,
)
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.evidence.enums import (
    AUTHORITATIVE_SOURCES,
    EvidenceSourceClassification,
    ToolReportedStatus,
)
from actionwitness_core.ports.enums import (
    MUTATING_SIDE_EFFECTS,
    ExecutionMode,
    RetrySemantics,
    SideEffectClass,
)
from actionwitness_core.registry import CLOSED_ENUMS
from actionwitness_core.reports.enums import (
    ALLOWED_LAYER_RESULTS,
    LayerResult,
    ReportLayer,
    RunMode,
)


def _values(enum_cls) -> set[str]:
    return {member.value for member in enum_cls}


# --- contract vocabulary (§9.4, §9.5) ---------------------------------------


@pytest.mark.unit
def test_the_operator_set_is_exactly_the_eight_mvp_operators() -> None:
    assert _values(AssertionOperator) == {
        "equals",
        "not_equals",
        "exists",
        "absent",
        "contains",
        "unchanged",
        "changed_by",
        "count_equals",
    }


@pytest.mark.unit
def test_transition_operators_are_the_two_that_read_both_snapshots() -> None:
    """§9.4: transition operators are invalid in preconditions, which see one snapshot."""
    assert {op.value for op in TRANSITION_OPERATORS} == {"unchanged", "changed_by"}
    assert TRANSITION_OPERATORS.isdisjoint(PRECONDITION_OPERATORS)
    assert frozenset(AssertionOperator) == TRANSITION_OPERATORS | PRECONDITION_OPERATORS


@pytest.mark.unit
def test_only_presence_and_stability_operators_take_no_expected_value() -> None:
    """§10.2: an expected value is required only for operators that need one.

    `unchanged` joins `exists` and `absent` here: §9.4 defines it as "the two
    values are deep-equal", so its comparand is the initial snapshot, not a
    literal the contract supplies.
    """
    valueless = frozenset(AssertionOperator) - OPERATORS_REQUIRING_VALUE
    assert {op.value for op in valueless} == {"exists", "absent", "unchanged"}


@pytest.mark.unit
def test_severity_is_exactly_info_warning_critical() -> None:
    assert _values(AssertionSeverity) == {"info", "warning", "critical"}


@pytest.mark.unit
def test_the_policy_set_is_exactly_the_six_mvp_policies() -> None:
    """Every one is recognised from the first commit; none is silently ignored."""
    assert _values(PolicyType) == {
        "requires_confirmation",
        "idempotent_by_request_id",
        "maximum_mutations",
        "forbidden_tool",
        "no_undeclared_changes",
        "stable_tool_surface",
    }


# --- port vocabulary (§9.1, §13.4) ------------------------------------------


@pytest.mark.unit
def test_execution_modes_are_managed_and_external_webmcp() -> None:
    assert _values(ExecutionMode) == {"managed", "external_webmcp"}


@pytest.mark.unit
def test_a_read_only_tool_is_not_counted_as_a_mutation() -> None:
    """§9.5's maximum_mutations counts state-changing completions, not reads."""
    assert SideEffectClass.READ_ONLY not in MUTATING_SIDE_EFFECTS
    assert (
        frozenset({SideEffectClass.MUTATING, SideEffectClass.PROTECTED_MUTATING})
        == MUTATING_SIDE_EFFECTS
    )


@pytest.mark.unit
def test_protected_mutation_is_distinguishable_from_ordinary_mutation() -> None:
    """Consent policy depends on the distinction; collapsing it would lose FR-060."""
    assert SideEffectClass.PROTECTED_MUTATING is not SideEffectClass.MUTATING


@pytest.mark.unit
def test_retry_semantics_offer_an_explicit_not_retryable_value() -> None:
    """Constitution §5: an ambiguous outcome must be surfaced, not auto-retried."""
    assert RetrySemantics.NOT_RETRYABLE in set(RetrySemantics)
    assert _values(RetrySemantics) == {
        "read_only_safe",
        "idempotent_by_request_id",
        # A mutation that cannot be duplicated by repetition and carries no
        # request ID - Appendix D.2's `apply_discount`. Without it such a tool
        # would have to publish `not_retryable`, which is a false statement.
        "naturally_idempotent",
        "not_retryable",
    }


# --- evidence vocabulary (§9.3, FR-032) -------------------------------------


@pytest.mark.unit
def test_only_an_independent_observation_is_authoritative() -> None:
    """Constitution §4: a tool's self-report is evidence, never proof."""
    assert EvidenceSourceClassification.TOOL_REPORTED not in AUTHORITATIVE_SOURCES
    assert EvidenceSourceClassification.JOURNEY_EVENTS not in AUTHORITATIVE_SOURCES
    assert (
        frozenset({EvidenceSourceClassification.AUTHORITATIVE_OBSERVATION}) == AUTHORITATIVE_SOURCES
    )


@pytest.mark.unit
def test_evidence_sources_are_the_three_required_mvp_kinds() -> None:
    assert _values(EvidenceSourceClassification) == {
        "authoritative_observation",
        "tool_reported",
        "journey_events",
    }


@pytest.mark.unit
def test_reported_status_values_are_the_four_fr032_names() -> None:
    assert _values(ToolReportedStatus) == {
        "success",
        "blocked_by_user",
        "blocked_by_expiry",
        "already_applied",
    }


# --- engine vocabulary (§17.1, §22) -----------------------------------------


@pytest.mark.unit
def test_the_classification_set_is_exactly_the_twelve_mvp_classifications() -> None:
    assert _values(FailureClassification) == {
        "tool_execution_error",
        "false_success_or_state_mismatch",
        "idempotency_violation",
        "missing_confirmation",
        "assertion_mismatch",
        "missing_expected_tool",
        "unexpected_tool",
        "trajectory_order_violation",
        "observation_unavailable",
        "undeclared_state_change",
        "tool_surface_mutation",
        "harness_error",
    }


@pytest.mark.unit
def test_an_unresolved_check_is_neither_passed_nor_failed() -> None:
    """§16.1: a policy with no baseline "shall never be reported as passed"."""
    unresolved = {CheckStatus.NOT_EVALUATED, CheckStatus.OBSERVATION_UNAVAILABLE}
    assert CheckStatus.PASSED not in unresolved
    assert CheckStatus.FAILED not in unresolved


@pytest.mark.unit
def test_check_types_cover_every_kind_of_contract_term() -> None:
    """§10.1's contract carries preconditions, expected_tools, assertions, policies."""
    assert _values(CheckType) == {"precondition", "assertion", "expected_tools", "policy"}


# --- report vocabulary (§23.1) ----------------------------------------------


@pytest.mark.unit
def test_the_five_layers_are_distinct_and_named_as_the_spec_names_them() -> None:
    """BUILD_ORDER invariant 10: the five layers remain distinct."""
    assert _values(ReportLayer) == {
        "model_tool_selection",
        "observed_trajectory",
        "tool_execution",
        "business_outcome",
        "safety_policy",
    }


@pytest.mark.unit
def test_every_layer_declares_its_permitted_values() -> None:
    assert set(ALLOWED_LAYER_RESULTS) == set(ReportLayer)


@pytest.mark.unit
@pytest.mark.parametrize(
    "layer,expected",
    [
        (ReportLayer.MODEL_TOOL_SELECTION, {"passed", "failed", "not_evaluated"}),
        (ReportLayer.OBSERVED_TRAJECTORY, {"passed", "failed", "not_evaluated"}),
        (
            ReportLayer.TOOL_EXECUTION,
            {"passed", "blocked_safely", "failed", "not_evaluated"},
        ),
        (
            ReportLayer.BUSINESS_OUTCOME,
            {"passed", "passed_with_warnings", "failed", "error", "not_evaluated"},
        ),
        (ReportLayer.SAFETY_POLICY, {"passed", "failed", "error"}),
    ],
    ids=lambda arg: arg.value if isinstance(arg, ReportLayer) else "expected",
)
def test_layer_value_sets_match_the_spec_table_exactly(
    layer: ReportLayer, expected: set[str]
) -> None:
    assert {value.value for value in ALLOWED_LAYER_RESULTS[layer]} == expected


@pytest.mark.unit
def test_blocked_safely_belongs_to_the_execution_layer_alone() -> None:
    """A safely refused mutation is an execution fact, not a business verdict."""
    carriers = [
        layer
        for layer, values in ALLOWED_LAYER_RESULTS.items()
        if LayerResult.BLOCKED_SAFELY in values
    ]
    assert carriers == [ReportLayer.TOOL_EXECUTION]


@pytest.mark.unit
def test_the_safety_policy_layer_can_never_report_not_evaluated() -> None:
    """A policy is evaluated, failed, or unresolvable - never quietly skipped."""
    assert LayerResult.NOT_EVALUATED not in ALLOWED_LAYER_RESULTS[ReportLayer.SAFETY_POLICY]


@pytest.mark.unit
def test_every_layer_result_value_is_reachable_from_some_layer() -> None:
    """A value no layer may use is dead vocabulary and would mislead a reader."""
    reachable = set().union(*ALLOWED_LAYER_RESULTS.values())
    assert reachable == set(LayerResult)


@pytest.mark.unit
def test_run_mode_separates_a_proposal_from_a_verdict() -> None:
    assert _values(RunMode) == {"verification", "proposal"}


# --- registry composition ---------------------------------------------------


@pytest.mark.unit
def test_the_new_vocabularies_reach_the_shared_registry() -> None:
    """A closed enum the UI cannot see forks the moment someone retypes a name."""
    registered = {closed.name for closed in CLOSED_ENUMS}
    assert {
        "assertion_operator",
        "assertion_severity",
        "policy_type",
        "execution_mode",
        "side_effect_class",
        "retry_semantics",
        "evidence_source_classification",
        "tool_reported_status",
        "check_type",
        "check_status",
        "failure_classification",
        "report_layer",
        "layer_result",
        "run_mode",
    } <= registered


@pytest.mark.unit
def test_the_lifecycle_enums_kept_their_export_position() -> None:
    """`registry.json` is committed; reordering it would be a diff with no meaning."""
    assert [closed.name for closed in CLOSED_ENUMS][:9] == [
        "run_state",
        "eval_run_state",
        "benchmark_suite_state",
        "outcome_event_type",
        "evaluation_event_type",
        "event_actor",
        "guidance_actor",
        "snapshot_phase",
        "workspace_kind",
    ]
