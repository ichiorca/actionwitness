"""Causal-classification and transition gates (spec v1.9 §12.6, §16, §22; 002-T10).

FR-055 names four conditions that must all hold before the engine says
`false_success_or_state_mismatch`, and each one gets a test that removes exactly
that condition and asserts the classification falls back to `assertion_mismatch`.
That shape is deliberate: the strong claim is an accusation, and a classifier
that reached it by accident would make the product's headline finding
untrustworthy.

The transition tests read the §16 tables back the other way - every state, every
permitted target, and a sweep asserting that every *unlisted* pair is refused, so
a row silently widened in the table is caught rather than merely unused.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from actionwitness_core.contracts.models import Assertion
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.assertions import evaluate_assertions
from actionwitness_core.engine.classification import (
    classify_assertion_failures,
    execution_findings,
    tool_execution_layer,
)
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.evidence.enums import ToolReportedStatus
from actionwitness_core.evidence.models import RunEvent
from actionwitness_core.journeys.enums import (
    BenchmarkSuiteState,
    EvalRunState,
    EventActor,
    OutcomeEventType,
    RunState,
)
from actionwitness_core.journeys.transitions import (
    BENCHMARK_SUITE_TRANSITIONS,
    EVAL_RUN_TRANSITIONS,
    PROPOSAL_RUN_STATES,
    RUN_TRANSITIONS,
    TERMINAL_RUN_STATES,
    VERDICT_RUN_STATES,
    can_reset,
    is_terminal,
    validate_benchmark_suite_transition,
    validate_eval_run_transition,
    validate_run_transition,
)
from actionwitness_core.kernel import TransitionError
from actionwitness_core.reports.enums import LayerResult

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

#: The §13.4 Buggy Store effect map, transcribed as target-neutral data.
EFFECT_MAP = {
    "search_catalog": (),
    "get_cart": (),
    "update_cart": (
        ObservationPath.parse("target.cart.items"),
        ObservationPath.parse("target.cart.subtotal"),
        ObservationPath.parse("target.cart.total"),
    ),
    "apply_discount": (
        ObservationPath.parse("target.cart.discount"),
        ObservationPath.parse("target.cart.total"),
    ),
    "proceed_to_checkout": (ObservationPath.parse("target.order"),),
}

DISCOUNTED_TOTAL = Assertion(
    id="discounted-total", path="target.cart.total", operator="equals", value="20.00"
)

INITIAL = {"target": {"cart": {"total": "25.00"}}}
FINAL = {"target": {"cart": {"total": "25.00"}}}


def _event(sequence: int, event_type: OutcomeEventType, **extra: object) -> RunEvent:
    fields: dict = {
        "sequence_number": sequence,
        "event_type": event_type,
        "actor": EventActor.AGENT,
        "created_at": EPOCH + timedelta(seconds=sequence),
    }
    fields.update(extra)
    return RunEvent(**fields)


def _classify(events, *, effect_map=EFFECT_MAP, assertion=DISCOUNTED_TOTAL, final=FINAL):
    findings = evaluate_assertions([assertion], initial=INITIAL, final=final)
    return classify_assertion_failures(
        findings, [assertion], events=events, effect_map=effect_map, initial=INITIAL
    )[0]


def _successful_discount(**extra: object) -> RunEvent:
    return _event(
        2,
        OutcomeEventType.TOOL_INVOCATION_COMPLETED,
        tool_name="apply_discount",
        reported_status=ToolReportedStatus.SUCCESS,
        **extra,
    )


# --- FR-055: the false-success rule -----------------------------------------


@pytest.mark.unit
def test_a_tool_that_reported_success_while_state_disagreed_is_a_false_success() -> None:
    """The product's headline finding: all four FR-055 conditions hold."""
    finding = _classify(
        [
            _event(1, OutcomeEventType.TOOL_INVOCATION_STARTED, tool_name="apply_discount"),
            _successful_discount(post_call_effect_state={"target": {"cart": {"total": "25.00"}}}),
        ]
    )
    assert finding.classification is FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH
    assert finding.causal_event_sequence == 2
    assert finding.attributed_cause["tool_name"] == "apply_discount"


@pytest.mark.unit
def test_without_declared_effect_metadata_causality_is_not_inferred() -> None:
    """§12.2: missing effect metadata disables causal attribution and nothing else."""
    finding = _classify(
        [
            _successful_discount(post_call_effect_state={"target": {"cart": {"total": "25.00"}}}),
        ],
        effect_map={},
    )
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH
    assert finding.attributed_cause["kind"] == "none"


@pytest.mark.unit
def test_a_tool_whose_declared_effect_does_not_overlap_is_not_blamed() -> None:
    """Overlap is at a dotted-key boundary (§13.4), not by string prefix."""
    finding = _classify(
        [
            _event(
                2,
                OutcomeEventType.TOOL_INVOCATION_COMPLETED,
                tool_name="proceed_to_checkout",
                reported_status=ToolReportedStatus.SUCCESS,
                post_call_effect_state={"target": {"cart": {"total": "25.00"}}},
            )
        ]
    )
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH


@pytest.mark.unit
def test_a_failed_action_is_never_a_false_success() -> None:
    """FR-055: "if the relevant action failed... use assertion_mismatch"."""
    finding = _classify(
        [
            _event(
                2,
                OutcomeEventType.TOOL_INVOCATION_FAILED,
                tool_name="apply_discount",
                post_call_effect_state={"target": {"cart": {"total": "25.00"}}},
            )
        ]
    )
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH
    assert "did not complete" in finding.attributed_cause["reason"]


@pytest.mark.unit
def test_a_cancelled_action_is_never_a_false_success() -> None:
    finding = _classify(
        [_event(2, OutcomeEventType.TOOL_INVOCATION_CANCELLED, tool_name="apply_discount")]
    )
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH


@pytest.mark.unit
@pytest.mark.parametrize(
    "status",
    [
        ToolReportedStatus.BLOCKED_BY_USER,
        ToolReportedStatus.BLOCKED_BY_EXPIRY,
        ToolReportedStatus.ALREADY_APPLIED,
    ],
)
def test_a_completion_that_claimed_no_success_is_never_a_false_success(
    status: ToolReportedStatus,
) -> None:
    finding = _classify(
        [
            _event(
                2,
                OutcomeEventType.TOOL_INVOCATION_COMPLETED,
                tool_name="apply_discount",
                reported_status=status,
                post_call_effect_state={"target": {"cart": {"total": "25.00"}}},
            )
        ]
    )
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH
    assert "without claiming success" in finding.attributed_cause["reason"]


@pytest.mark.unit
def test_without_an_immediate_post_call_observation_causality_is_not_inferred() -> None:
    """FR-055: "lacks an immediate observation" is one of the four fallbacks."""
    finding = _classify([_successful_discount()])
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH
    assert "no immediate authoritative" in finding.attributed_cause["reason"]


@pytest.mark.unit
def test_a_post_call_observation_that_satisfied_the_assertion_shifts_the_blame() -> None:
    """The tool did its job; something later undid it, so it is not a false success."""
    finding = _classify(
        [_successful_discount(post_call_effect_state={"target": {"cart": {"total": "20.00"}}})]
    )
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH
    assert "caused by something later" in finding.attributed_cause["reason"]


@pytest.mark.unit
def test_only_the_last_relevant_action_is_considered() -> None:
    """§13.4: this prevents an earlier tool being blamed for a later change."""
    finding = _classify(
        [
            _event(
                1,
                OutcomeEventType.TOOL_INVOCATION_COMPLETED,
                tool_name="apply_discount",
                reported_status=ToolReportedStatus.SUCCESS,
                post_call_effect_state={"target": {"cart": {"total": "25.00"}}},
            ),
            _event(
                3,
                OutcomeEventType.TOOL_INVOCATION_COMPLETED,
                tool_name="update_cart",
                reported_status=ToolReportedStatus.ALREADY_APPLIED,
                post_call_effect_state={"target": {"cart": {"total": "25.00"}}},
            ),
        ]
    )
    assert finding.attributed_cause["tool_name"] == "update_cart"
    assert finding.classification is FailureClassification.ASSERTION_MISMATCH


@pytest.mark.unit
def test_a_replayed_action_classifies_the_same_as_its_source() -> None:
    """AC-15: a regression case must reproduce its source failure exactly."""
    agent = _classify(
        [_successful_discount(post_call_effect_state={"target": {"cart": {"total": "25.00"}}})]
    )
    replay = _classify(
        [
            _successful_discount(
                actor=EventActor.EVAL,
                post_call_effect_state={"target": {"cart": {"total": "25.00"}}},
            )
        ]
    )
    assert replay.classification is agent.classification
    assert replay.classification is FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH


@pytest.mark.unit
def test_a_passing_assertion_is_never_reclassified() -> None:
    passing = Assertion(id="total", path="target.cart.total", operator="equals", value="25.00")
    finding = _classify([_successful_discount()], assertion=passing)
    assert finding.status is CheckStatus.PASSED
    assert finding.classification is None


@pytest.mark.unit
def test_classification_is_deterministic() -> None:
    events = [_successful_discount(post_call_effect_state={"target": {"cart": {"total": "25.00"}}})]
    assert _classify(events) == _classify(events)


# --- the tool-execution layer (§23.1, FR-033) -------------------------------


@pytest.mark.unit
def test_a_run_with_no_invocations_leaves_the_execution_layer_not_evaluated() -> None:
    assert tool_execution_layer([]) is LayerResult.NOT_EVALUATED


@pytest.mark.unit
def test_successful_calls_pass_the_execution_layer() -> None:
    events = [_successful_discount()]
    assert tool_execution_layer(events) is LayerResult.PASSED
    assert execution_findings(events) == ()


@pytest.mark.unit
def test_an_unexpected_error_fails_the_execution_layer() -> None:
    events = [_event(1, OutcomeEventType.TOOL_INVOCATION_FAILED, tool_name="apply_discount")]
    assert tool_execution_layer(events) is LayerResult.FAILED
    findings = execution_findings(events)
    assert len(findings) == 1
    assert findings[0].classification is FailureClassification.TOOL_EXECUTION_ERROR


@pytest.mark.unit
@pytest.mark.parametrize(
    "status", [ToolReportedStatus.BLOCKED_BY_USER, ToolReportedStatus.BLOCKED_BY_EXPIRY]
)
def test_a_safely_refused_action_is_blocked_not_failed(status: ToolReportedStatus) -> None:
    """FR-033 and §23.1: a denied or expired protected action is a safe outcome."""
    events = [
        _event(
            1,
            OutcomeEventType.TOOL_INVOCATION_COMPLETED,
            tool_name="proceed_to_checkout",
            reported_status=status,
        )
    ]
    assert tool_execution_layer(events) is LayerResult.BLOCKED_SAFELY
    assert execution_findings(events) == ()


@pytest.mark.unit
def test_a_cancellation_is_a_safe_outcome_not_an_execution_error() -> None:
    events = [_event(1, OutcomeEventType.TOOL_INVOCATION_CANCELLED, tool_name="update_cart")]
    assert tool_execution_layer(events) is LayerResult.BLOCKED_SAFELY
    assert execution_findings(events) == ()


# --- run transitions (§16) --------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "current,target",
    [(current, target) for current, targets in RUN_TRANSITIONS.items() for target in targets],
    ids=lambda arg: arg.value,
)
def test_every_transition_the_spec_lists_is_permitted(current: RunState, target: RunState) -> None:
    validate_run_transition(current, target)


@pytest.mark.unit
def test_every_transition_the_spec_does_not_list_is_refused() -> None:
    """A row silently widened in the table is caught here, not in production."""
    refused = 0
    for current in RunState:
        for target in RunState:
            if target in RUN_TRANSITIONS[current]:
                continue
            with pytest.raises(TransitionError):
                validate_run_transition(current, target)
            refused += 1
    assert refused > 0


@pytest.mark.unit
def test_a_terminal_run_cannot_move_anywhere() -> None:
    assert {
        RunState.PROPOSED,
        RunState.PASSED,
        RunState.PASSED_WITH_WARNINGS,
        RunState.FAILED,
        RunState.ERROR,
        RunState.CANCELLED,
    } == TERMINAL_RUN_STATES
    for state in TERMINAL_RUN_STATES:
        assert is_terminal(state)
        with pytest.raises(TransitionError, match="terminal"):
            validate_run_transition(state, RunState.RUNNING)


@pytest.mark.unit
def test_verification_is_the_only_route_to_a_verdict() -> None:
    """A verdict that could be reached without verifying would be unevidenced."""
    for state in RunState:
        reachable_verdicts = RUN_TRANSITIONS[state] & VERDICT_RUN_STATES
        if state is not RunState.VERIFYING:
            assert reachable_verdicts == frozenset(), state


@pytest.mark.unit
def test_a_proposal_run_never_reaches_a_verdict_state() -> None:
    """§16: a proposal run "never enters running, verifying, or any verdict state"."""
    forbidden = VERDICT_RUN_STATES | {RunState.RUNNING, RunState.VERIFYING}
    for state in PROPOSAL_RUN_STATES:
        assert RUN_TRANSITIONS[state] & forbidden == frozenset(), state


@pytest.mark.unit
def test_a_verification_run_never_enters_a_proposal_state() -> None:
    """The two subgraphs stay disjoint in both directions."""
    for state in RunState:
        if state in PROPOSAL_RUN_STATES:
            continue
        assert RUN_TRANSITIONS[state] & PROPOSAL_RUN_STATES == frozenset(), state


@pytest.mark.unit
def test_a_refusal_names_what_was_permitted_instead() -> None:
    with pytest.raises(TransitionError) as excinfo:
        validate_run_transition(RunState.ARMED, RunState.PASSED)
    assert "armed" in excinfo.value.message
    assert "running" in excinfo.value.message
    assert excinfo.value.details


@pytest.mark.unit
def test_reset_is_valid_from_every_run_state() -> None:
    """§16: reset "is valid from every workspace/run state" and is not a state."""
    assert all(can_reset(state) for state in RunState)
    assert "reset" not in {state.value for state in RunState}


# --- eval and benchmark transitions (§16.2, §16.4) --------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "current,target",
    [(c, t) for c, targets in EVAL_RUN_TRANSITIONS.items() for t in targets],
    ids=lambda arg: arg.value,
)
def test_every_eval_transition_the_spec_lists_is_permitted(
    current: EvalRunState, target: EvalRunState
) -> None:
    validate_eval_run_transition(current, target)


@pytest.mark.unit
def test_an_eval_run_cannot_skip_execution() -> None:
    with pytest.raises(TransitionError):
        validate_eval_run_transition(EvalRunState.QUEUED, EvalRunState.PASSED)


@pytest.mark.unit
@pytest.mark.parametrize(
    "current,target",
    [(c, t) for c, targets in BENCHMARK_SUITE_TRANSITIONS.items() for t in targets],
    ids=lambda arg: arg.value,
)
def test_every_benchmark_transition_the_spec_lists_is_permitted(
    current: BenchmarkSuiteState, target: BenchmarkSuiteState
) -> None:
    validate_benchmark_suite_transition(current, target)


@pytest.mark.unit
def test_a_ready_suite_may_complete_without_running() -> None:
    """§16.4: an executed_browser suite's outcome runs already exist."""
    validate_benchmark_suite_transition(BenchmarkSuiteState.READY, BenchmarkSuiteState.COMPLETED)


@pytest.mark.unit
def test_a_completed_suite_is_immutable() -> None:
    """§16.4: "completed suites are immutable"; a changed manifest needs a new suite."""
    for target in BenchmarkSuiteState:
        with pytest.raises(TransitionError):
            validate_benchmark_suite_transition(BenchmarkSuiteState.COMPLETED, target)


@pytest.mark.unit
def test_a_draft_suite_cannot_jump_straight_to_completed() -> None:
    with pytest.raises(TransitionError):
        validate_benchmark_suite_transition(
            BenchmarkSuiteState.DRAFT, BenchmarkSuiteState.COMPLETED
        )


@pytest.mark.unit
def test_a_state_from_another_machine_is_refused() -> None:
    """Cross-machine confusion would silently permit an impossible move."""
    with pytest.raises(TransitionError, match="not a run state"):
        validate_run_transition(RunState.ARMED, EvalRunState.QUEUED)  # type: ignore[arg-type]
