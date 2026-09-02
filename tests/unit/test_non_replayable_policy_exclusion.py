"""§24.3a's exclusion, at both ends of the translation it depends on.

"Excluded from both the actual and the expected critical-classification sets" is
a sentence about two vocabularies. A case names *policies*; a classification set
names *classifications*; the two closed enums share not one spelling. An
implementation that filtered classifications against policy names therefore
excluded nothing at all — and a no-op exclusion is invisible, because the case it
silently fails is exactly the case whose author already declared the policy
unevaluable.

So this module holds the rule down from three sides:

* the vocabularies really are disjoint, which is why a translation is needed;
* the translation is **total** over `PolicyType`, and stays honest about what the
  engine actually reports, so a seventh policy cannot arrive unmapped;
* the exclusion removes *that policy's* classification and nothing else — the
  counterfactual, without which "exclude everything" would pass every test above.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from actionwitness_core.contracts.enums import PolicyType, SurfaceDeltaKind
from actionwitness_core.contracts.models import (
    ForbiddenToolPolicy,
    IdempotencyPolicy,
    MaximumMutationsPolicy,
    NoUndeclaredChangesPolicy,
    Policy,
    RequiresConfirmationPolicy,
    StableToolSurfacePolicy,
)
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.engine.policies import PolicyEvidence, evaluate_policy
from actionwitness_core.evals.models import (
    POLICY_CRITICAL_CLASSIFICATIONS,
    EnvironmentExpectation,
    compare_replay_to_expectation,
    policy_critical_classifications,
)
from actionwitness_core.evidence.enums import ToolNamespace, ToolReportedStatus
from actionwitness_core.evidence.models import RunEvent
from actionwitness_core.evidence.surface import SurfaceDelta, ToolDefinition
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType
from actionwitness_core.reports.enums import LayerResult

pytestmark = pytest.mark.unit

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64

MISMATCH = FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH
SURFACE = FailureClassification.TOOL_SURFACE_MUTATION
CONSENT = FailureClassification.MISSING_CONFIRMATION


def _completed(sequence: int, tool: str, **extra: object) -> RunEvent:
    extra.setdefault("reported_status", ToolReportedStatus.SUCCESS)
    return RunEvent(
        sequence_number=sequence,
        event_type=OutcomeEventType.TOOL_INVOCATION_COMPLETED,
        actor=EventActor.AGENT,
        created_at=EPOCH + timedelta(seconds=sequence),
        tool_name=tool,
        **extra,  # type: ignore[arg-type]
    )


def _started(sequence: int, tool: str) -> RunEvent:
    return RunEvent(
        sequence_number=sequence,
        event_type=OutcomeEventType.TOOL_INVOCATION_STARTED,
        actor=EventActor.AGENT,
        created_at=EPOCH + timedelta(seconds=sequence),
        tool_name=tool,
    )


def _mutation(sequence: int, tool: str, request_id: str) -> RunEvent:
    """One completion whose recorded canonical state hashes moved."""
    return _completed(
        sequence,
        tool,
        request_id=request_id,
        state_hash_before=HASH_A,
        state_hash_after=HASH_B,
    )


def _failing_evidence() -> dict[PolicyType, tuple[Policy, PolicyEvidence]]:
    """One genuinely failing case per policy type, in the engine's own terms.

    Written out per policy rather than generated, because the point is to make
    each policy *actually* report its classification: a generated stub would
    prove the mapping agrees with itself.
    """
    surface = ToolDefinition(name="update_cart", namespace=ToolNamespace.TARGET)
    return {
        PolicyType.REQUIRES_CONFIRMATION: (
            RequiresConfirmationPolicy(tool="proceed_to_checkout"),
            PolicyEvidence(
                events=(
                    _started(1, "proceed_to_checkout"),
                    _mutation(2, "proceed_to_checkout", "req_checkout"),
                )
            ),
        ),
        PolicyType.IDEMPOTENT_BY_REQUEST_ID: (
            IdempotencyPolicy(tool="update_cart"),
            PolicyEvidence(
                events=(
                    _mutation(1, "update_cart", "req_same"),
                    _mutation(2, "update_cart", "req_same"),
                )
            ),
        ),
        PolicyType.MAXIMUM_MUTATIONS: (
            MaximumMutationsPolicy(limit=0),
            PolicyEvidence(events=(_mutation(1, "update_cart", "req_one"),)),
        ),
        PolicyType.FORBIDDEN_TOOL: (
            ForbiddenToolPolicy(tool="drop_database"),
            PolicyEvidence(events=(_started(1, "drop_database"),)),
        ),
        PolicyType.NO_UNDECLARED_CHANGES: (
            NoUndeclaredChangesPolicy(),
            PolicyEvidence(changed_paths=(ObservationPath.parse("target.preferences.theme"),)),
        ),
        PolicyType.STABLE_TOOL_SURFACE: (
            StableToolSurfacePolicy(),
            PolicyEvidence(
                surface_baseline_recorded=True,
                observed_surface_deltas=(
                    SurfaceDelta(
                        tool_name="update_cart",
                        namespace=ToolNamespace.TARGET,
                        kind=SurfaceDeltaKind.ADDED,
                        before=surface,
                        after=surface,
                    ),
                ),
            ),
        ),
    }


# --- the two vocabularies ----------------------------------------------------


def test_policy_names_and_classification_values_share_no_spelling() -> None:
    """Why §24.3a needs a translation and not a filter.

    The defect this suite exists for compared one enum's values against the
    other's. Asserting the disjointness makes that a permanent property rather
    than a coincidence somebody has to notice: `requires_confirmation` is not
    `missing_confirmation`, and no filter written across the two can ever
    exclude anything.
    """
    # Arrange / Act
    policy_names = {policy_type.value for policy_type in PolicyType}
    classification_values = {item.value for item in FailureClassification}

    # Assert
    assert policy_names & classification_values == set()


# --- totality ----------------------------------------------------------------


def test_the_mapping_covers_every_policy_type() -> None:
    """A policy nobody mapped would be a policy nobody excludes."""
    # Arrange / Act / Assert
    assert set(POLICY_CRITICAL_CLASSIFICATIONS) == set(PolicyType)


def test_every_policy_maps_to_at_least_one_published_classification() -> None:
    """An empty mapping is the silent no-op wearing a table's clothes."""
    # Arrange / Act / Assert
    for policy_type, classifications in POLICY_CRITICAL_CLASSIFICATIONS.items():
        assert classifications, f"{policy_type.value} maps to no classification"
        assert classifications <= frozenset(FailureClassification)


def test_a_policy_type_nobody_mapped_raises_rather_than_excluding_nothing() -> None:
    """The exhaustiveness check, exercised.

    A seventh policy type reaching this function must stop the process rather
    than return an empty set — an empty set is exactly the behaviour §24.3a was
    silently getting for all six.
    """
    # Arrange
    unmapped = cast(PolicyType, "policy_added_without_a_mapping")

    # Act / Assert
    with pytest.raises(AssertionError):
        policy_critical_classifications(unmapped)


@pytest.mark.parametrize("policy_type", list(PolicyType))
def test_the_mapping_names_the_classification_the_engine_actually_reports(
    policy_type: PolicyType,
) -> None:
    """The mapping is checked against the engine, not against itself.

    `engine.policies` chooses a classification when a policy fails; this table
    says which one that will be. Two statements of one relation drift silently,
    so this drives each policy to a real failure and demands the table already
    contain what came back.
    """
    # Arrange
    policy, evidence = _failing_evidence()[policy_type]

    # Act
    finding = evaluate_policy(policy, evidence)

    # Assert
    assert finding.status is CheckStatus.FAILED, "this evidence must fail the policy"
    assert finding.classification is not None
    assert finding.classification in POLICY_CRITICAL_CLASSIFICATIONS[policy_type]


# --- the exclusion itself ----------------------------------------------------


def test_an_unevaluable_policys_classification_leaves_the_expected_set() -> None:
    """§24.3a, the half that turns a spurious failure into a pass.

    The case expects the surface policy's classification; the policy could not be
    evaluated here; so the expectation it carries is not something this run can
    be held to.
    """
    # Arrange
    expectation = EnvironmentExpectation(
        overall_result=LayerResult.FAILED, required_classifications=(MISMATCH, SURFACE)
    )

    # Act
    comparison = compare_replay_to_expectation(
        expectation,
        actual_result=LayerResult.FAILED,
        actual_classifications=(MISMATCH,),
        non_replayable_policies=("stable_tool_surface",),
    )

    # Assert
    assert comparison.expected_classifications == (MISMATCH,)
    assert comparison.excluded_classifications == (SURFACE,)
    assert comparison.matched is True


def test_the_exclusion_is_symmetric_and_also_leaves_the_actual_set() -> None:
    """The other half: an unevaluable policy cannot manufacture a failure either.

    Set equality is symmetric, so an exclusion applied to one side only would
    trade one spurious failure for another.
    """
    # Arrange
    expectation = EnvironmentExpectation(
        overall_result=LayerResult.FAILED, required_classifications=(MISMATCH,)
    )

    # Act
    comparison = compare_replay_to_expectation(
        expectation,
        actual_result=LayerResult.FAILED,
        actual_classifications=(MISMATCH, SURFACE),
        non_replayable_policies=("stable_tool_surface",),
    )

    # Assert
    assert comparison.actual_classifications == (MISMATCH,)
    assert comparison.matched is True


def test_an_unrelated_classification_survives_the_exclusion() -> None:
    """The counterfactual, without which the fix could be "ignore everything".

    An unevaluable `stable_tool_surface` says nothing about consent. If naming
    one policy excused every classification, every eval expectation in the suite
    would become vacuous — which is worse than the defect being fixed, because
    it would be green.
    """
    # Arrange
    expectation = EnvironmentExpectation(
        overall_result=LayerResult.FAILED, required_classifications=(MISMATCH,)
    )

    # Act
    comparison = compare_replay_to_expectation(
        expectation,
        actual_result=LayerResult.FAILED,
        actual_classifications=(CONSENT,),
        non_replayable_policies=("stable_tool_surface",),
    )

    # Assert
    assert comparison.actual_classifications == (CONSENT,)
    assert comparison.expected_classifications == (MISMATCH,)
    assert comparison.matched is False


def test_an_excluded_policy_does_not_excuse_a_different_policys_classification() -> None:
    """Exclusion is scoped to the policy that was named, not to policies at large."""
    # Arrange
    expectation = EnvironmentExpectation(
        overall_result=LayerResult.FAILED, required_classifications=(SURFACE,)
    )

    # Act
    comparison = compare_replay_to_expectation(
        expectation,
        actual_result=LayerResult.FAILED,
        actual_classifications=(),
        non_replayable_policies=("requires_confirmation",),
    )

    # Assert — the consent policy was unevaluable; the surface policy was not.
    assert comparison.expected_classifications == (SURFACE,)
    assert comparison.matched is False


def test_a_result_mismatch_still_fails_however_much_was_excluded() -> None:
    """§24.1 compares the overall result too, and §24.3a never touches it."""
    # Arrange
    expectation = EnvironmentExpectation(
        overall_result=LayerResult.FAILED, required_classifications=(SURFACE,)
    )

    # Act
    comparison = compare_replay_to_expectation(
        expectation,
        actual_result=LayerResult.PASSED,
        actual_classifications=(),
        non_replayable_policies=("stable_tool_surface",),
    )

    # Assert
    assert comparison.expected_classifications == ()
    assert comparison.matched is False


def test_an_unknown_policy_name_excludes_nothing_and_is_still_named() -> None:
    """A case is untrusted input (constitution §5).

    A name outside the closed §9.5 vocabulary cannot be translated into the
    classification it would have produced, and guessing is not available. Keeping
    the check is the safe direction: it can only make an eval fail visibly, never
    make one pass by dropping a classification nobody chose. §24.3a's other half
    still holds — the name is reported.
    """
    # Arrange
    expectation = EnvironmentExpectation(
        overall_result=LayerResult.FAILED, required_classifications=(SURFACE,)
    )

    # Act
    comparison = compare_replay_to_expectation(
        expectation,
        actual_result=LayerResult.FAILED,
        actual_classifications=(),
        non_replayable_policies=("a_policy_this_build_never_heard_of",),
    )

    # Assert
    assert comparison.expected_classifications == (SURFACE,)
    assert comparison.matched is False
    assert comparison.excluded_policies == ("a_policy_this_build_never_heard_of",)


def test_a_run_with_nothing_excluded_compares_both_sets_whole() -> None:
    """The empty case: no policy named, no classification removed."""
    # Arrange
    expectation = EnvironmentExpectation(
        overall_result=LayerResult.FAILED, required_classifications=(MISMATCH, SURFACE)
    )

    # Act
    comparison = compare_replay_to_expectation(
        expectation,
        actual_result=LayerResult.FAILED,
        actual_classifications=(SURFACE, MISMATCH),
    )

    # Assert
    assert comparison.expected_classifications == (MISMATCH, SURFACE)
    assert comparison.excluded_policies == ()
    assert comparison.excluded_classifications == ()
    assert comparison.matched is True
