"""Trajectory and policy gates (spec v1.9 §9.5, §10.3, §12.7, §16.1; 002-T9).

The rule under test throughout is BUILD_ORDER §7/M1's: every policy type is
recognised and safely evaluated from the beginning, and a policy the engine
cannot judge reports that it could not, rather than passing. So each policy has a
test for the vacuous pass, the real failure, and - where evidence can be missing
- the unresolved case, and one test asserts that no policy type can reach a
`passed` status without evidence that actually supports it.

For trajectory, the case worth naming is the split between the two
classifications: a call that never happened is `missing_expected_tool` even when
`ordered` is true, because reporting it as an ordering fault would send a reader
looking for a call that is not there.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from actionwitness_core.contracts.enums import AssertionSeverity, SurfaceDeltaKind
from actionwitness_core.contracts.models import (
    ExpectedTools,
    ForbiddenToolPolicy,
    IdempotencyPolicy,
    MaximumMutationsPolicy,
    NoUndeclaredChangesPolicy,
    RequiresConfirmationPolicy,
    StableToolSurfacePolicy,
)
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.engine.policies import (
    PolicyEvidence,
    evaluate_policies,
    evaluate_policy,
    surface_evidence,
)
from actionwitness_core.engine.trajectory import evaluate_expected_tools, observed_calls
from actionwitness_core.evidence.enums import ToolNamespace, ToolReportedStatus
from actionwitness_core.evidence.models import RunEvent
from actionwitness_core.evidence.surface import SurfaceDelta, ToolDefinition
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _event(sequence: int, event_type: OutcomeEventType, **extra: object) -> RunEvent:
    fields: dict = {
        "sequence_number": sequence,
        "event_type": event_type,
        "actor": EventActor.AGENT,
        "created_at": EPOCH + timedelta(seconds=sequence),
    }
    fields.update(extra)
    return RunEvent(**fields)


def _start(sequence: int, tool: str, **extra: object) -> RunEvent:
    return _event(sequence, OutcomeEventType.TOOL_INVOCATION_STARTED, tool_name=tool, **extra)


def _completed(sequence: int, tool: str, **extra: object) -> RunEvent:
    extra.setdefault("reported_status", ToolReportedStatus.SUCCESS)
    return _event(sequence, OutcomeEventType.TOOL_INVOCATION_COMPLETED, tool_name=tool, **extra)


def _path(text: str) -> ObservationPath:
    return ObservationPath.parse(text)


# --- observed trajectory (§10.3, FR-056) ------------------------------------


@pytest.mark.unit
def test_only_eligible_invocation_starts_count_as_occurrences() -> None:
    """§10.3: an occurrence is an invocation *start*; FR-056 excludes human events."""
    events = [
        _start(1, "search_catalog"),
        _completed(2, "search_catalog"),
        _start(3, "update_cart", actor=EventActor.HUMAN),
        _event(4, OutcomeEventType.SNAPSHOT_CAPTURED, actor=EventActor.HARNESS),
        _start(5, "apply_discount", actor=EventActor.EVAL),
    ]
    assert observed_calls(events) == ("search_catalog", "apply_discount")


@pytest.mark.unit
def test_occurrences_are_read_in_sequence_order_not_list_order() -> None:
    events = [_start(3, "apply_discount"), _start(1, "search_catalog"), _start(2, "update_cart")]
    assert observed_calls(events) == ("search_catalog", "update_cart", "apply_discount")


@pytest.mark.unit
def test_a_failed_call_still_counts_as_an_occurrence() -> None:
    """§10.3: "success or failure is evaluated separately in the tool-execution layer"."""
    events = [
        _start(1, "update_cart"),
        _event(2, OutcomeEventType.TOOL_INVOCATION_FAILED, tool_name="update_cart"),
    ]
    finding = evaluate_expected_tools(ExpectedTools(calls=("update_cart",)), events)
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_omitting_expected_tools_reports_not_evaluated_rather_than_passed() -> None:
    """§10.3: the observed trajectory is `not_evaluated` when the term is absent."""
    finding = evaluate_expected_tools(None, [_start(1, "update_cart")])
    assert finding.status is CheckStatus.NOT_EVALUATED
    assert finding.check_type is CheckType.EXPECTED_TOOLS
    assert finding.failed is False


@pytest.mark.unit
def test_an_unordered_check_requires_multiset_containment() -> None:
    """§10.3: "the observed call multiset must contain at least the required count"."""
    expected = ExpectedTools(ordered=False, calls=("update_cart", "update_cart"))
    twice = [_start(1, "update_cart"), _start(2, "update_cart"), _start(3, "search_catalog")]
    once = [_start(1, "update_cart"), _start(2, "search_catalog")]
    assert evaluate_expected_tools(expected, twice).status is CheckStatus.PASSED
    assert evaluate_expected_tools(expected, once).status is CheckStatus.FAILED


@pytest.mark.unit
def test_an_unordered_check_ignores_the_order_calls_occurred_in() -> None:
    expected = ExpectedTools(ordered=False, calls=("search_catalog", "update_cart"))
    reversed_order = [_start(1, "update_cart"), _start(2, "search_catalog")]
    assert evaluate_expected_tools(expected, reversed_order).status is CheckStatus.PASSED


@pytest.mark.unit
def test_an_ordered_check_matches_as_a_greedy_subsequence() -> None:
    """§10.3: "unrelated extra calls may occur between matched calls"."""
    expected = ExpectedTools(ordered=True, calls=("search_catalog", "apply_discount"))
    interleaved = [
        _start(1, "search_catalog"),
        _start(2, "update_cart"),
        _start(3, "apply_discount"),
    ]
    assert evaluate_expected_tools(expected, interleaved).status is CheckStatus.PASSED


@pytest.mark.unit
def test_an_out_of_order_trajectory_is_an_order_violation() -> None:
    expected = ExpectedTools(ordered=True, calls=("search_catalog", "apply_discount"))
    swapped = [_start(1, "apply_discount"), _start(2, "search_catalog")]
    finding = evaluate_expected_tools(expected, swapped)
    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.TRAJECTORY_ORDER_VIOLATION


@pytest.mark.unit
def test_a_call_that_never_happened_is_missing_not_misordered() -> None:
    """Reporting it as an ordering fault sends a reader looking for a call that is absent."""
    expected = ExpectedTools(ordered=True, calls=("search_catalog", "apply_discount"))
    finding = evaluate_expected_tools(expected, [_start(1, "search_catalog")])
    assert finding.classification is FailureClassification.MISSING_EXPECTED_TOOL
    assert finding.evidence["missing_calls"] == ["apply_discount"]


@pytest.mark.unit
def test_extra_calls_do_not_fail_the_trajectory_check() -> None:
    """§10.3: "extra calls are allowed unless a forbidden_tool policy disallows them"."""
    expected = ExpectedTools(calls=("update_cart",))
    events = [_start(1, "update_cart"), _start(2, "delete_account")]
    assert evaluate_expected_tools(expected, events).status is CheckStatus.PASSED


@pytest.mark.unit
def test_a_trajectory_failure_is_always_critical_and_carries_one_check_id() -> None:
    finding = evaluate_expected_tools(ExpectedTools(calls=("update_cart",)), [])
    assert finding.severity is AssertionSeverity.CRITICAL
    assert finding.check_id == "expected_tools"


@pytest.mark.unit
def test_trajectory_evaluation_is_deterministic() -> None:
    expected = ExpectedTools(ordered=True, calls=("a_tool", "b_tool"))
    events = [_start(1, "b_tool"), _start(2, "a_tool")]
    assert evaluate_expected_tools(expected, events) == evaluate_expected_tools(expected, events)


# --- requires_confirmation (FR-060, FR-061, FR-066) -------------------------


@pytest.mark.unit
def test_an_unattempted_protected_tool_passes_vacuously() -> None:
    """FR-060: "if the protected tool was never attempted, the policy passes"."""
    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"),
        PolicyEvidence(events=(_start(1, "update_cart"),)),
    )
    assert finding.status is CheckStatus.PASSED
    assert finding.evidence["attempts"] == 0


@pytest.mark.unit
def test_an_approved_mutation_satisfies_the_confirmation_policy() -> None:
    events = (
        _start(1, "proceed_to_checkout", correlation_id="c-1"),
        _event(2, OutcomeEventType.CONFIRMATION_REQUESTED, correlation_id="c-1"),
        _event(
            3, OutcomeEventType.CONFIRMATION_APPROVED, correlation_id="c-1", actor=EventActor.HUMAN
        ),
        _completed(4, "proceed_to_checkout", correlation_id="c-1"),
    )
    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"), PolicyEvidence(events=events)
    )
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_a_successful_mutation_with_no_approval_fails_the_policy() -> None:
    events = (
        _start(1, "proceed_to_checkout", correlation_id="c-1"),
        _completed(2, "proceed_to_checkout", correlation_id="c-1"),
    )
    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"), PolicyEvidence(events=events)
    )
    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.MISSING_CONFIRMATION
    assert finding.causal_event_sequence == 2


@pytest.mark.unit
def test_an_approval_that_arrives_after_the_mutation_does_not_authorize_it() -> None:
    """FR-066: a stale approval "shall never authorize a mutation"."""
    events = (
        _start(1, "proceed_to_checkout", correlation_id="c-1"),
        _completed(2, "proceed_to_checkout", correlation_id="c-1"),
        _event(
            3, OutcomeEventType.CONFIRMATION_APPROVED, correlation_id="c-1", actor=EventActor.HUMAN
        ),
    )
    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"), PolicyEvidence(events=events)
    )
    assert finding.status is CheckStatus.FAILED


@pytest.mark.unit
def test_an_approval_for_a_different_invocation_does_not_authorize_this_one() -> None:
    """FR-066 binds a confirmation to its own invocation; correlation is not optional."""
    events = (
        _event(
            1,
            OutcomeEventType.CONFIRMATION_APPROVED,
            correlation_id="other",
            actor=EventActor.HUMAN,
        ),
        _start(2, "proceed_to_checkout", correlation_id="c-1"),
        _completed(3, "proceed_to_checkout", correlation_id="c-1"),
    )
    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"), PolicyEvidence(events=events)
    )
    assert finding.status is CheckStatus.FAILED


@pytest.mark.unit
@pytest.mark.parametrize(
    "outcome",
    [
        OutcomeEventType.CONFIRMATION_DENIED,
        OutcomeEventType.CONFIRMATION_EXPIRED,
        OutcomeEventType.CONFIRMATION_CANCELLED,
    ],
)
def test_a_safely_blocked_attempt_passes_the_policy(outcome: OutcomeEventType) -> None:
    """§9.5: "a denied, expired, or cancelled attempt with no mutation passes"."""
    events = (
        _start(1, "proceed_to_checkout", correlation_id="c-1"),
        _event(2, outcome, correlation_id="c-1", actor=EventActor.HUMAN),
        _completed(
            3,
            "proceed_to_checkout",
            correlation_id="c-1",
            reported_status=ToolReportedStatus.BLOCKED_BY_USER,
        ),
    )
    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"), PolicyEvidence(events=events)
    )
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
@pytest.mark.parametrize(
    "claim",
    [
        ToolReportedStatus.BLOCKED_BY_USER,
        ToolReportedStatus.BLOCKED_BY_EXPIRY,
        ToolReportedStatus.ALREADY_APPLIED,
    ],
)
def test_a_mutation_that_claims_it_was_blocked_still_needs_consent(
    claim: ToolReportedStatus,
) -> None:
    """The self-report is not the gate; the recorded state hashes are.

    `blocked_by_user` means "no state changed and the consent policy passes",
    and this completion's canonical state hashes say otherwise. Reading the
    claim instead of the hashes let an unapproved mutation walk past the one
    policy whose whole job is consent — the exact lie the product exists to
    catch, inside the product.
    """
    events = (
        _start(1, "proceed_to_checkout", correlation_id="c-1"),
        _completed(
            2,
            "proceed_to_checkout",
            correlation_id="c-1",
            reported_status=claim,
            state_hash_before=HASH_A,
            state_hash_after=HASH_B,
        ),
    )

    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"), PolicyEvidence(events=events)
    )

    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.MISSING_CONFIRMATION
    assert finding.severity is AssertionSeverity.CRITICAL
    assert finding.causal_event_sequence == 2
    assert finding.evidence["observed_state_change_sequences"] == [2]


@pytest.mark.unit
def test_a_blocked_call_that_changed_nothing_still_passes() -> None:
    """§9.5's safe block, now proved by hashes rather than taken on trust.

    The denial is recorded, the tool reports it was blocked, and the canonical
    state either side of the call is the same hash. Nothing here needed consent,
    so tightening the gate must not fail it.
    """
    events = (
        _start(1, "proceed_to_checkout", correlation_id="c-1"),
        _event(
            2, OutcomeEventType.CONFIRMATION_DENIED, correlation_id="c-1", actor=EventActor.HUMAN
        ),
        _completed(
            3,
            "proceed_to_checkout",
            correlation_id="c-1",
            reported_status=ToolReportedStatus.BLOCKED_BY_USER,
            state_hash_before=HASH_A,
            state_hash_after=HASH_A,
        ),
    )

    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"), PolicyEvidence(events=events)
    )

    assert finding.status is CheckStatus.PASSED
    assert finding.evidence["safely_blocked"] == ["c-1"]


@pytest.mark.unit
def test_an_approved_call_that_mutated_still_passes() -> None:
    """Consent that arrived first authorizes the mutation the hashes record."""
    events = (
        _start(1, "proceed_to_checkout", correlation_id="c-1"),
        _event(
            2, OutcomeEventType.CONFIRMATION_APPROVED, correlation_id="c-1", actor=EventActor.HUMAN
        ),
        _completed(
            3,
            "proceed_to_checkout",
            correlation_id="c-1",
            state_hash_before=HASH_A,
            state_hash_after=HASH_B,
        ),
    )

    finding = evaluate_policy(
        RequiresConfirmationPolicy(tool="proceed_to_checkout"), PolicyEvidence(events=events)
    )

    assert finding.status is CheckStatus.PASSED


# --- idempotent_by_request_id (FR-063) --------------------------------------


@pytest.mark.unit
def test_an_unrepeated_request_passes_the_idempotency_policy() -> None:
    events = (
        _completed(
            1, "update_cart", request_id="r-1", state_hash_before=HASH_A, state_hash_after=HASH_B
        ),
    )
    finding = evaluate_policy(IdempotencyPolicy(tool="update_cart"), PolicyEvidence(events=events))
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_a_repeated_request_that_changed_state_once_passes() -> None:
    """The correct retry: the second call returns the first persisted result."""
    events = (
        _completed(
            1, "update_cart", request_id="r-1", state_hash_before=HASH_A, state_hash_after=HASH_B
        ),
        _completed(
            2,
            "update_cart",
            request_id="r-1",
            state_hash_before=HASH_B,
            state_hash_after=HASH_B,
            reported_status=ToolReportedStatus.ALREADY_APPLIED,
        ),
    )
    finding = evaluate_policy(IdempotencyPolicy(tool="update_cart"), PolicyEvidence(events=events))
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_a_repeated_request_that_changed_state_twice_fails() -> None:
    events = (
        _completed(
            1, "update_cart", request_id="r-1", state_hash_before=HASH_A, state_hash_after=HASH_B
        ),
        _completed(
            2, "update_cart", request_id="r-1", state_hash_before=HASH_B, state_hash_after=HASH_C
        ),
    )
    finding = evaluate_policy(IdempotencyPolicy(tool="update_cart"), PolicyEvidence(events=events))
    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.IDEMPOTENCY_VIOLATION
    assert finding.causal_event_sequence == 2


@pytest.mark.unit
def test_a_repeated_request_with_no_state_hashes_is_unresolved_not_passed() -> None:
    """Constitution §5: evidence that cannot answer the question is not a pass."""
    events = (
        _completed(1, "update_cart", request_id="r-1"),
        _completed(2, "update_cart", request_id="r-1"),
    )
    finding = evaluate_policy(IdempotencyPolicy(tool="update_cart"), PolicyEvidence(events=events))
    assert finding.status is CheckStatus.OBSERVATION_UNAVAILABLE
    assert finding.failed is True


# --- maximum_mutations (FR-064) ---------------------------------------------


@pytest.mark.unit
def test_mutations_within_the_limit_pass() -> None:
    events = (
        _completed(1, "update_cart", state_hash_before=HASH_A, state_hash_after=HASH_B),
        _completed(2, "update_cart", state_hash_before=HASH_B, state_hash_after=HASH_B),
    )
    finding = evaluate_policy(MaximumMutationsPolicy(limit=1), PolicyEvidence(events=events))
    assert finding.status is CheckStatus.PASSED
    assert finding.evidence["observed"] == 1


@pytest.mark.unit
def test_mutations_over_the_limit_fail() -> None:
    events = (
        _completed(1, "update_cart", state_hash_before=HASH_A, state_hash_after=HASH_B),
        _completed(2, "update_cart", state_hash_before=HASH_B, state_hash_after=HASH_C),
    )
    finding = evaluate_policy(MaximumMutationsPolicy(limit=1), PolicyEvidence(events=events))
    assert finding.status is CheckStatus.FAILED
    assert finding.causal_event_sequence == 2


@pytest.mark.unit
def test_uncountable_mutations_are_unresolved_not_passed() -> None:
    events = (_completed(1, "update_cart"),)
    finding = evaluate_policy(MaximumMutationsPolicy(limit=0), PolicyEvidence(events=events))
    assert finding.status is CheckStatus.OBSERVATION_UNAVAILABLE


# --- forbidden_tool (FR-065) ------------------------------------------------


@pytest.mark.unit
def test_a_forbidden_tool_that_never_appeared_passes() -> None:
    finding = evaluate_policy(
        ForbiddenToolPolicy(tool="delete_account"),
        PolicyEvidence(events=(_start(1, "update_cart"),)),
    )
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_any_invocation_start_for_a_forbidden_tool_fails() -> None:
    """§9.5 says "any invocation-start event", so the actor does not matter."""
    for actor in (EventActor.AGENT, EventActor.HUMAN, EventActor.EVAL):
        finding = evaluate_policy(
            ForbiddenToolPolicy(tool="delete_account"),
            PolicyEvidence(events=(_start(1, "delete_account", actor=actor),)),
        )
        assert finding.status is CheckStatus.FAILED
        assert finding.classification is FailureClassification.UNEXPECTED_TOOL


# --- no_undeclared_changes (§9.10) ------------------------------------------


@pytest.mark.unit
def test_without_a_full_state_diff_the_policy_is_not_evaluated_rather_than_passed() -> None:
    """The unevaluated case must never read as a satisfied policy."""
    finding = evaluate_policy(NoUndeclaredChangesPolicy(), PolicyEvidence())
    assert finding.status is CheckStatus.NOT_EVALUATED
    assert "FR-157" in finding.evidence["reason"]


@pytest.mark.unit
def test_a_path_covered_by_a_contract_assertion_is_declared() -> None:
    """§9.10(a): a path that resolves a contract term is a declared change."""
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(),
        PolicyEvidence(
            contract_paths=(_path("target.cart.total"),),
            changed_paths=(_path("target.cart.total"),),
        ),
    )
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_a_path_under_an_executed_tools_declared_effect_is_declared() -> None:
    """§9.10(b), matched at a dotted-key boundary per §13.4."""
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(),
        PolicyEvidence(
            events=(_start(1, "update_cart"),),
            effect_map={"update_cart": (_path("target.cart.items"),)},
            changed_paths=(_path("target.cart.items.mug.quantity"),),
        ),
    )
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_an_effect_declared_by_a_tool_that_never_ran_does_not_declare_anything() -> None:
    """§9.10(b) covers "a tool that actually executed in the run"."""
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(),
        PolicyEvidence(
            effect_map={"update_cart": (_path("target.cart.items"),)},
            changed_paths=(_path("target.cart.items"),),
        ),
    )
    assert finding.status is CheckStatus.FAILED


@pytest.mark.unit
def test_one_finding_lists_every_undeclared_path() -> None:
    """§17.1: one finding per run, so the classification set stays stable."""
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(),
        PolicyEvidence(
            changed_paths=(
                _path("target.preferences.delivery_note"),
                _path("target.profile.nickname"),
            ),
        ),
    )
    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.UNDECLARED_STATE_CHANGE
    assert [str(path) for path in finding.paths] == [
        "target.preferences.delivery_note",
        "target.profile.nickname",
    ]


@pytest.mark.unit
def test_an_applied_waiver_is_recorded_so_it_is_never_invisible() -> None:
    """§23.1: `applied_exemptions` lists every waiver actually applied."""
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(allow_paths=(_path("target.cart.updated_at"),)),
        PolicyEvidence(changed_paths=(_path("target.cart.updated_at"),)),
    )
    assert finding.status is CheckStatus.PASSED
    assert [str(path) for path in finding.applied_exemptions] == ["target.cart.updated_at"]


@pytest.mark.unit
def test_an_unused_waiver_is_not_recorded_as_applied() -> None:
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(allow_paths=(_path("target.cart.updated_at"),)),
        PolicyEvidence(changed_paths=()),
    )
    assert finding.applied_exemptions == ()


@pytest.mark.unit
def test_a_precondition_path_declares_a_change() -> None:
    """§9.10(a) reads "assertion **or precondition** path".

    A path the contract read at arming is a path it cares about. Counting only
    end-state assertions would make every precondition-only path undeclared, and
    a contract that checked an opening balance would fail for having checked it.
    """
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(),
        PolicyEvidence(
            contract_paths=(_path("target.cart.subtotal"),),
            changed_paths=(_path("target.cart.subtotal"),),
        ),
    )
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
@pytest.mark.parametrize(
    "declared,changed",
    [
        ("target.cart", "target.cartridge"),
        ("target.cart", "target.cart_backup"),
        ("target.order", "target.orders"),
    ],
)
def test_a_sibling_sharing_a_textual_prefix_is_not_declared(declared: str, changed: str) -> None:
    """§13.4's dotted-key boundary, which is the whole reason for the resolver.

    `target.cart` textually prefixes `target.cartridge` while naming an unrelated
    value. A partition written with `str.startswith` passes every other test in
    this file and silently declares a path nothing covers — the failure mode is
    a *missed* undeclared change, which is the one this feature exists to catch.
    """
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(),
        PolicyEvidence(
            events=(_start(1, "update_cart"),),
            effect_map={"update_cart": (_path(declared),)},
            changed_paths=(_path(changed),),
        ),
    )
    assert finding.status is CheckStatus.FAILED
    assert [str(path) for path in finding.paths] == [changed]


@pytest.mark.unit
def test_a_waiver_respects_the_same_boundary_rule() -> None:
    """An `allow_paths` entry is an escape hatch, not a widening.

    Criterion 3 of the 013 gate: a waiver admits declared churn "without widening
    anything else". A waiver matched by string prefix would silently exempt every
    sibling whose name it happened to prefix.
    """
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(allow_paths=(_path("target.cart.updated_at"),)),
        PolicyEvidence(changed_paths=(_path("target.cart.updated_at_by"),)),
    )
    assert finding.status is CheckStatus.FAILED
    assert finding.applied_exemptions == ()


@pytest.mark.unit
def test_a_run_with_no_effect_metadata_records_that_fact() -> None:
    """§23.1: `effect_metadata_published: false` explains why changes were undeclared."""
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(),
        PolicyEvidence(events=(_start(1, "update_cart"),), changed_paths=(_path("target.cart"),)),
    )
    assert finding.evidence["effect_metadata_published"] is False


# --- stable_tool_surface (§9.5, §16.1) --------------------------------------


def _delta(
    kind: SurfaceDeltaKind,
    *,
    tool: str = "apply_discount",
    namespace: ToolNamespace = ToolNamespace.TARGET,
) -> SurfaceDelta:
    """One observed delta.

    A whole `SurfaceDelta` rather than a bare kind, because 014 gave the policy
    three questions to ask of each one — which partition, which tool, which kind
    — and a bare kind can only answer the last.
    """
    definition = ToolDefinition(name=tool, namespace=namespace)
    return SurfaceDelta(
        tool_name=tool, namespace=namespace, kind=kind, before=definition, after=definition
    )


@pytest.mark.unit
def test_a_missing_surface_baseline_is_unresolved_and_never_passed() -> None:
    """§16.1 states this outcome explicitly."""
    finding = evaluate_policy(StableToolSurfacePolicy(), PolicyEvidence())
    assert finding.status is CheckStatus.OBSERVATION_UNAVAILABLE
    assert finding.failed is True


@pytest.mark.unit
def test_a_recorded_baseline_with_no_deltas_passes() -> None:
    finding = evaluate_policy(
        StableToolSurfacePolicy(), PolicyEvidence(surface_baseline_recorded=True)
    )
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
@pytest.mark.parametrize(
    "kind",
    [
        SurfaceDeltaKind.ADDED,
        SurfaceDeltaKind.REMOVED,
        SurfaceDeltaKind.SCHEMA_CHANGE,
        SurfaceDeltaKind.HINT_CHANGE,
    ],
)
def test_a_delta_in_the_failing_set_fails_the_policy(kind: SurfaceDeltaKind) -> None:
    finding = evaluate_policy(
        StableToolSurfacePolicy(),
        PolicyEvidence(surface_baseline_recorded=True, observed_surface_deltas=(_delta(kind),)),
    )
    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.TOOL_SURFACE_MUTATION


@pytest.mark.unit
def test_a_description_change_is_a_warning_by_default_and_is_still_reported() -> None:
    """§9.5: "benign copy edits should not fail a run" - but they stay visible."""
    finding = evaluate_policy(
        StableToolSurfacePolicy(),
        PolicyEvidence(
            surface_baseline_recorded=True,
            observed_surface_deltas=(_delta(SurfaceDeltaKind.DESCRIPTION_CHANGE),),
        ),
    )
    assert finding.status is CheckStatus.PASSED
    assert finding.evidence["warned_delta_kinds"] == ["description_change"]


@pytest.mark.unit
def test_strictness_can_promote_a_description_change_to_a_failure() -> None:
    finding = evaluate_policy(
        StableToolSurfacePolicy(failing_delta_kinds=(SurfaceDeltaKind.DESCRIPTION_CHANGE,)),
        PolicyEvidence(
            surface_baseline_recorded=True,
            observed_surface_deltas=(_delta(SurfaceDeltaKind.DESCRIPTION_CHANGE),),
        ),
    )
    assert finding.status is CheckStatus.FAILED


# --- every policy type is recognised ----------------------------------------


ALL_POLICIES = (
    RequiresConfirmationPolicy(tool="proceed_to_checkout"),
    IdempotencyPolicy(tool="update_cart"),
    MaximumMutationsPolicy(limit=2),
    ForbiddenToolPolicy(tool="delete_account"),
    NoUndeclaredChangesPolicy(),
    StableToolSurfacePolicy(),
)


@pytest.mark.unit
def test_every_policy_type_produces_an_explicit_finding() -> None:
    """BUILD_ORDER §7/M1: no seeded policy may be silently ignored."""
    findings = evaluate_policies(ALL_POLICIES, PolicyEvidence())
    assert len(findings) == len(ALL_POLICIES)
    assert {finding.check_id for finding in findings} == {
        "requires_confirmation",
        "idempotent_by_request_id",
        "maximum_mutations",
        "forbidden_tool",
        "no_undeclared_changes",
        "stable_tool_surface",
    }


@pytest.mark.unit
def test_every_policy_finding_is_critical() -> None:
    """§9.5: "all MVP policy failures are critical"."""
    for finding in evaluate_policies(ALL_POLICIES, PolicyEvidence()):
        assert finding.severity is AssertionSeverity.CRITICAL
        assert finding.check_type is CheckType.POLICY


@pytest.mark.unit
def test_no_policy_reaches_passed_on_evidence_that_cannot_support_it() -> None:
    """The failure this whole module guards: a green policy nobody evaluated."""
    statuses = {
        finding.check_id: finding.status
        for finding in evaluate_policies(ALL_POLICIES, PolicyEvidence())
    }
    assert statuses["no_undeclared_changes"] is CheckStatus.NOT_EVALUATED
    assert statuses["stable_tool_surface"] is CheckStatus.OBSERVATION_UNAVAILABLE
    # The remaining three pass vacuously, which is what the spec defines for a
    # tool that was never attempted rather than an absence of evidence.
    assert statuses["requires_confirmation"] is CheckStatus.PASSED
    assert statuses["idempotent_by_request_id"] is CheckStatus.PASSED
    assert statuses["forbidden_tool"] is CheckStatus.PASSED


@pytest.mark.unit
def test_policy_evaluation_is_deterministic_over_one_event_stream() -> None:
    """FR-050 defines policy determinism over the same recorded stream."""
    evidence = PolicyEvidence(
        events=(
            _start(1, "proceed_to_checkout", correlation_id="c-1"),
            _completed(2, "proceed_to_checkout", correlation_id="c-1"),
        )
    )
    assert evaluate_policies(ALL_POLICIES, evidence) == evaluate_policies(ALL_POLICIES, evidence)


# --- recorded evidence models -----------------------------------------------


@pytest.mark.unit
def test_an_invocation_event_must_name_its_tool() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must name the tool"):
        _event(1, OutcomeEventType.TOOL_INVOCATION_STARTED)


@pytest.mark.unit
def test_only_a_completion_carries_a_reported_status() -> None:
    """FR-032: failed and cancelled events carry their outcome in the event name."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="carries no reported status"):
        _event(
            1,
            OutcomeEventType.TOOL_INVOCATION_FAILED,
            tool_name="update_cart",
            reported_status=ToolReportedStatus.SUCCESS,
        )


@pytest.mark.unit
def test_an_event_is_immutable() -> None:
    from pydantic import ValidationError

    event = _start(1, "update_cart")
    with pytest.raises(ValidationError):
        event.sequence_number = 2


# --- stable_tool_surface: partition and declared churn (014-T4) -------------


@pytest.mark.unit
def test_a_harness_partition_delta_does_not_fail_the_policy() -> None:
    """§9.11: stability policy applies to the target partition by default.

    The harness's own tools appear and disappear as a run moves through §11.5's
    phases. Judging them would fail every run at its first lifecycle transition,
    which is why the partition exists at all.
    """
    finding = evaluate_policy(
        StableToolSurfacePolicy(),
        PolicyEvidence(
            surface_baseline_recorded=True,
            observed_surface_deltas=(
                _delta(
                    SurfaceDeltaKind.ADDED, tool="verify_outcome", namespace=ToolNamespace.HARNESS
                ),
            ),
        ),
    )
    assert finding.status is CheckStatus.PASSED


@pytest.mark.unit
def test_a_failure_carries_the_side_by_side_definitions() -> None:
    """FR-169: "a side-by-side diff of the tool definition before and after"."""
    finding = evaluate_policy(
        StableToolSurfacePolicy(),
        PolicyEvidence(
            surface_baseline_recorded=True,
            observed_surface_deltas=(_delta(SurfaceDeltaKind.SCHEMA_CHANGE),),
        ),
    )
    assert finding.status is CheckStatus.FAILED
    (recorded,) = finding.evidence["deltas"]
    assert recorded["before"] is not None
    assert recorded["after"] is not None


@pytest.mark.unit
def test_a_delta_the_vocabulary_does_not_know_is_dropped_rather_than_guessed() -> None:
    """Mapping an unrecognised kind onto a known one would manufacture a verdict."""
    events = (
        _event(1, OutcomeEventType.TOOL_SURFACE_CAPTURED, actor=EventActor.HARNESS),
        _event(
            2,
            OutcomeEventType.TOOL_SURFACE_CHANGED,
            actor=EventActor.HARNESS,
            redacted_payload={"kind": "teleported", "namespace": "target", "tool_name": "x"},
        ),
    )
    recorded, deltas = surface_evidence(events)
    assert recorded is True
    assert deltas == ()


@pytest.mark.unit
def test_extra_payload_context_does_not_drop_a_delta() -> None:
    """A replayed event carries `recorded_sequence` beside the delta.

    Strict validation over the whole payload would reject it for the extra key
    and drop the delta silently — turning a poisoned surface into a clean run.
    """
    events = (
        _event(1, OutcomeEventType.TOOL_SURFACE_CAPTURED, actor=EventActor.HARNESS),
        _event(
            2,
            OutcomeEventType.TOOL_SURFACE_CHANGED,
            actor=EventActor.HARNESS,
            redacted_payload={
                "kind": "added",
                "namespace": "target",
                "tool_name": "exfiltrate",
                "recorded_sequence": 3,
            },
        ),
    )
    _, deltas = surface_evidence(events)
    assert [delta.tool_name for delta in deltas] == ["exfiltrate"]
