"""007-T1 — the core eval vocabulary (§24.1, §24.3, §9.8, FR-081, FR-088).

**This file replaces a tripwire.** Until this milestone it asserted that
`actionwitness_core.evals` had grown *no* public behaviour, so that the moment
M6 landed any, the placeholder failed and forced real §24 coverage to arrive in
the same change rather than after it. That is what has now happened; the
assertions below are the coverage it was holding a place for.

Two properties carry the rest of the milestone.

**Eval status is not business outcome.** §24.3: a `reproduce_source` run that
faithfully reproduces a recorded `failed` outcome has eval status `passed`.
`test_a_reproduced_failure_is_a_passing_expectation` is that sentence made
executable, and it is the test that stops somebody "fixing" the apparent
contradiction later.

**The expectation is set equality.** An extra critical classification is a
*different* failure from the one the case was cut from, so a suite that accepted
supersets would pass while a new regression rode along inside it.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.enums import AssertionSeverity
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.evals import (
    CASE_SCHEMA_VERSION,
    ConfirmationStrategy,
    EmbeddedContract,
    EnvironmentExpectation,
    EvalEnvironment,
    EvalExpectations,
    EvalFixture,
    EvalReport,
    EvalSource,
    EvalStatus,
    EvalTarget,
    RegressionEvalCase,
    ReplayConfiguration,
    SourceFinding,
    TrajectoryStep,
    expectation_matches,
)
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.security.canonical import content_hash
from pydantic import ValidationError

pytestmark = pytest.mark.evals

MISMATCH = FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH
MISSING_CONFIRMATION = FailureClassification.MISSING_CONFIRMATION

CONTRACT_DOCUMENT = {
    "schema_version": "1.0",
    "name": "one-mug-save20-no-checkout",
    "description": "Add one mug, apply SAVE20, and do not create an order.",
    "target_id": "buggy-store",
    "intent": "Add exactly one ceramic mug and apply SAVE20 without creating an order.",
    "assertions": [
        {
            "id": "discounted-total",
            "path": "target.cart.total",
            "operator": "equals",
            "value": "20.00",
            "severity": "critical",
        }
    ],
}


def _contract() -> EmbeddedContract:
    from actionwitness_core.contracts.models import OutcomeContract

    document = OutcomeContract.model_validate(CONTRACT_DOCUMENT)
    return EmbeddedContract(
        content_hash=content_hash(document.canonical_document()), document=document
    )


def _case(**overrides: object) -> RegressionEvalCase:
    defaults: dict[str, object] = {
        "id": "eval_example_001",
        "name": "save20-updates-canonical-cart-total",
        "source": EvalSource(
            run_id="run_example_001",
            implementation_version="0.1.0",
            scenario_mode="pre_fix",
            failure_profile="discount_reported_but_not_applied",
            overall_result=LayerResult.FAILED,
            critical_classifications=(MISMATCH,),
        ),
        "target": EvalTarget(
            type="managed_application", id="buggy-store", adapter="integrations.buggy_store"
        ),
        "fixture": EvalFixture(
            content_hash=content_hash({"cart": {}}), target_state={"cart": {}, "order": {}}
        ),
        "trajectory": (
            TrajectoryStep(sequence=1, tool="update_cart", arguments={"quantity": 1}),
            TrajectoryStep(sequence=2, tool="apply_discount", arguments={"code": "SAVE20"}),
        ),
        "contract": _contract(),
        "expected": EvalExpectations(
            current=EnvironmentExpectation(overall_result=LayerResult.PASSED),
            reproduce_source=EnvironmentExpectation(
                overall_result=LayerResult.FAILED, required_classifications=(MISMATCH,)
            ),
        ),
    }
    return RegressionEvalCase(**{**defaults, **overrides})  # type: ignore[arg-type]


# --- the distinction the milestone turns on ----------------------------------


def test_a_reproduced_failure_is_a_passing_expectation() -> None:
    """§24.3, made executable.

    "Eval-run status is based on expectation matching, not on whether the actual
    business outcome string is literally `passed`." A run that recreates the
    recorded failure did what it was asked to do.
    """
    # Arrange
    expectation = _case().expected.for_environment(EvalEnvironment.REPRODUCE_SOURCE)

    # Act
    matched = expectation_matches(
        expectation,
        actual_result=LayerResult.FAILED,
        actual_classifications=[MISMATCH],
    )

    # Assert
    assert matched is True


def test_a_run_that_stopped_failing_does_not_meet_the_reproduce_expectation() -> None:
    """The counterpart. Without it, "always true" would satisfy the test above.

    A `reproduce_source` run that suddenly passes has *not* reproduced the
    source failure — which is a real signal, not a success: the case can no
    longer prove the bug it was cut from.
    """
    # Arrange
    expectation = _case().expected.for_environment(EvalEnvironment.REPRODUCE_SOURCE)

    # Act / Assert
    assert (
        expectation_matches(
            expectation, actual_result=LayerResult.PASSED, actual_classifications=[]
        )
        is False
    )


def test_an_additional_critical_classification_fails_the_expectation() -> None:
    """Set equality, not containment (§24.1).

    An extra critical failure is a *different* failure from the one the case was
    cut from. Accepting supersets would let a new regression ride along inside a
    passing eval — exactly what a regression suite exists to catch.
    """
    # Arrange
    expectation = _case().expected.for_environment(EvalEnvironment.REPRODUCE_SOURCE)

    # Act / Assert
    assert (
        expectation_matches(
            expectation,
            actual_result=LayerResult.FAILED,
            actual_classifications=[MISMATCH, MISSING_CONFIRMATION],
        )
        is False
    )


def test_an_unrelated_classification_fails_even_at_the_same_count() -> None:
    """One failure swapped for another is not a reproduction."""
    # Arrange
    expectation = _case().expected.for_environment(EvalEnvironment.REPRODUCE_SOURCE)

    # Act / Assert
    assert (
        expectation_matches(
            expectation,
            actual_result=LayerResult.FAILED,
            actual_classifications=[MISSING_CONFIRMATION],
        )
        is False
    )


def test_order_and_duplicates_are_ignored() -> None:
    """§24.1: "ordering is ignored and duplicates are collapsed"."""
    # Arrange
    expectation = EnvironmentExpectation(
        overall_result=LayerResult.FAILED,
        required_classifications=(MISMATCH, MISSING_CONFIRMATION),
    )

    # Act / Assert
    assert expectation_matches(
        expectation,
        actual_result=LayerResult.FAILED,
        actual_classifications=[MISSING_CONFIRMATION, MISMATCH, MISMATCH],
    )


def test_the_current_expectation_wants_a_clean_pass() -> None:
    """§24.2 step 7: `current` expects `passed` with no critical classifications."""
    # Arrange
    expectation = _case().expected.for_environment(EvalEnvironment.CURRENT)

    # Act / Assert
    assert expectation.overall_result is LayerResult.PASSED
    assert expectation.classification_set() == frozenset()
    assert expectation_matches(
        expectation, actual_result=LayerResult.PASSED, actual_classifications=[]
    )


# --- the case document -------------------------------------------------------


def test_the_hash_excludes_the_documents_own_hash_member() -> None:
    """§24.2 step 11 and §17.2.

    A reader handed a stored case must be able to recompute its hash from the
    document alone — which only works if the hash was taken over everything
    *except* itself.
    """
    # Arrange
    case = _case()

    # Act
    stored = case.as_stored_document()
    recorded = stored.pop("content_hash")

    # Assert
    assert recorded == content_hash(stored)
    assert recorded == case.content_hash()


def test_two_identical_cases_serialize_byte_identically() -> None:
    """FR-080's idempotence, as a property of the content.

    "The same source run yields the same case" has to be true of the *bytes*, or
    a CI job that regenerated a case would see a spurious diff and a reviewer
    would learn to ignore them.
    """
    # Arrange / Act
    first = _case().canonical_bytes()
    second = _case().canonical_bytes()

    # Assert
    assert first == second


def test_classification_order_does_not_change_the_hash() -> None:
    """§17.2 normalizes unordered collections used in hashes.

    Two generators that walked findings in different orders must produce the
    same case, or the hash stops identifying the case and starts identifying the
    walk.
    """
    # Arrange
    one = _case(
        source=EvalSource(
            run_id="run_example_001",
            implementation_version="0.1.0",
            overall_result=LayerResult.FAILED,
            critical_classifications=(MISMATCH, MISSING_CONFIRMATION),
        )
    )
    other = _case(
        source=EvalSource(
            run_id="run_example_001",
            implementation_version="0.1.0",
            overall_result=LayerResult.FAILED,
            critical_classifications=(MISSING_CONFIRMATION, MISMATCH),
        )
    )

    # Act / Assert
    assert one.content_hash() == other.content_hash()


def test_a_gap_in_the_trajectory_is_refused() -> None:
    """A dropped step would replay a different journey than the one recorded —
    the way a case comes to pass for a reason other than the one it was cut
    for."""
    # Arrange / Act / Assert
    # Pydantic wraps a validator's rejection, which is the shape every other
    # core model test asserts against.
    with pytest.raises(ValidationError, match="no gaps"):
        _case(
            trajectory=(
                TrajectoryStep(sequence=1, tool="update_cart"),
                TrajectoryStep(sequence=3, tool="apply_discount"),
            )
        )


def test_a_case_carries_no_executable_content(subtests: object = None) -> None:
    """FR-086: a case is data a CI job runs, so anything executable in it would
    be arbitrary code execution wearing a fixture's clothes.

    Enforced structurally: a trajectory step has a tool name and arguments and
    no field that could hold a command, a URL, or an import.
    """
    # Arrange
    fields = set(TrajectoryStep.model_fields)

    # Act / Assert
    assert fields == {"sequence", "tool", "arguments"}
    with pytest.raises(Exception):  # noqa: B017 - any rejection is the point
        TrajectoryStep(sequence=1, tool="update_cart", command="rm -rf /")  # type: ignore[call-arg]


def test_the_case_defaults_to_current_and_to_no_inferred_consent() -> None:
    """§24.4 and FR-087, in the defaults.

    A case that defaulted to `reproduce_source` would report a reproduced
    failure as routine CI success; one that defaulted to an approval would
    grant consent the recording never contained.
    """
    # Arrange
    replay = ReplayConfiguration()

    # Act / Assert
    assert replay.default_environment is EvalEnvironment.CURRENT
    assert replay.confirmation_strategy is ConfirmationStrategy.NO_CONFIRMATION


def test_no_confirmation_strategy_can_grant_consent() -> None:
    """FR-087 has no "approve now" member, and that is the whole point.

    Every value replays what a recording contained or supplies nothing. A mode
    that synthesized approval would turn a missing-confirmation regression into
    its own opposite.
    """
    # Arrange / Act
    values = {member.value for member in ConfirmationStrategy}

    # Assert
    assert values == {"recorded_approval", "recorded_denial", "no_confirmation"}


def test_a_case_embeds_its_contract_rather_than_referencing_one() -> None:
    """FR-082: creating or running an eval requires no private package,
    repository, schema, or credential. A case pointing at a contract row could
    not be handed to anybody."""
    # Arrange
    case = _case()

    # Act / Assert
    assert case.contract.document.name == "one-mug-save20-no-checkout"
    assert case.contract.content_hash.startswith("sha256:")
    assert case.schema_version == CASE_SCHEMA_VERSION


def test_the_source_failure_profile_is_provenance_not_configuration() -> None:
    """§24.2 step 9 records it "without automatically activating it".

    A case that forced its own fault profile would make `current` untestable —
    and `current` is the profile CI actually runs.
    """
    # Arrange
    case = _case()

    # Act / Assert
    assert case.source.failure_profile == "discount_reported_but_not_applied"
    assert case.replay.default_environment is EvalEnvironment.CURRENT


# --- the report --------------------------------------------------------------


def test_the_report_keeps_status_and_outcome_in_separate_fields() -> None:
    """FR-088, and §24.3's distinction rendered as data.

    A reproduced failure is `overall_result: failed` with `status: passed`.
    Collapsing them into one field is the misreading the spec warns about, and
    it is a misreading a shared field would make unavoidable.
    """
    # Arrange
    report = EvalReport(
        eval_case_id="eval_example_001",
        eval_case_hash=_case().content_hash(),
        implementation_version="0.1.0",
        environment=EvalEnvironment.REPRODUCE_SOURCE,
        status=EvalStatus.PASSED,
        overall_result=LayerResult.FAILED,
        actual_classifications=(MISMATCH,),
        expected_classifications=(MISMATCH,),
        classification_match=True,
    )

    # Act
    document = report.canonical_document()

    # Assert
    assert document["status"] == "passed"
    assert document["overall_result"] == "failed"


def test_the_report_names_the_environment_and_both_classification_sets() -> None:
    """§24.4: "so a passing eval cannot hide the environment or failure it
    produced"."""
    # Arrange
    report = EvalReport(
        eval_case_id="eval_example_001",
        eval_case_hash=_case().content_hash(),
        implementation_version="0.1.0",
        environment=EvalEnvironment.REPRODUCE_SOURCE,
        status=EvalStatus.PASSED,
        overall_result=LayerResult.FAILED,
        actual_classifications=(MISMATCH,),
        expected_classifications=(MISMATCH,),
        classification_match=True,
        non_replayable_policies=("stable_tool_surface",),
    )

    # Act
    document = report.canonical_document()

    # Assert
    assert document["environment"] == "reproduce_source"
    assert document["actual_classifications"] == ["false_success_or_state_mismatch"]
    assert document["expected_classifications"] == ["false_success_or_state_mismatch"]
    # A policy that could not be evaluated is named, so "passed" never quietly
    # means "not checked" (§24.3a).
    assert document["non_replayable_policies"] == ["stable_tool_surface"]


def test_an_error_status_says_nothing_about_the_target() -> None:
    """§9.8 and FR-088: an invalid case or a broken harness is not a failure of
    the thing under test, which is why it has its own status and its own exit
    code rather than being folded into a failure."""
    # Arrange
    report = EvalReport(
        eval_case_id="eval_example_001",
        eval_case_hash=_case().content_hash(),
        implementation_version="0.1.0",
        environment=EvalEnvironment.CURRENT,
        status=EvalStatus.ERROR,
        detail="the case did not validate",
    )

    # Act / Assert
    assert report.status is EvalStatus.ERROR
    assert report.overall_result is None
    assert report.actual_classifications == ()


def test_a_source_finding_records_what_the_run_actually_found() -> None:
    """§24.1's `source_findings`, kept so a reader can see what the case is
    about without running it."""
    # Arrange
    finding = SourceFinding(
        check_id="discounted-total",
        classification=MISMATCH,
        severity=AssertionSeverity.CRITICAL,
        status=CheckStatus.FAILED,
        path="target.cart.total",
        expected="20.00",
        actual="25.00",
    )

    # Act
    document = finding.canonical_document()

    # Assert
    assert document["classification"] == "false_success_or_state_mismatch"
    assert document["expected"] == "20.00"
    assert document["actual"] == "25.00"
