"""FR-159's evidence, and §9.5's warning, as a reader actually receives them.

Spec v1.9 §12.16/FR-159 (an undeclared-change finding lists "the paths **with
redacted before and after values**, and attribute a likely cause"), §23.1 (the
report block, whose `paths` entries are `{path, before, after,
attributed_cause}` objects), §9.5 (`description_change` "warns by default"),
§8.5 and FR-053 (`passed_with_warnings`), §20.3 (redaction before persistence),
§11.4 (the finding-value budget), AC-24.

Two failures are under test here and both are failures of *reporting*, not of
judgement. The engine already decided correctly in each case; what it produced
could not be read.

**An undeclared change threw away its own evidence.** The diff computes bounded
before/after excerpts for every changed path and the policy discarded them, so
AC-24's "names `target.preferences.delivery_note` with redacted before and after
values and a causal attribution" arrived as a bare path. A reader was told that
something moved and refused what it moved from and to.

**A warned surface delta was invisible.** §9.5 warns on `description_change`
rather than failing, which is right - benign copy edits exist. But the warning
was recorded only inside the evidence of a passing finding, so a target that
rewrote a tool's description mid-run produced an unqualified `passed` with
`counts.warnings: 0`. A warning nobody counts is not a warning.

Every test drives the real pipeline - `diff_states`, then `evaluate_policy`,
then `compose_outcome_report` - because each of these defects lived in the seam
between two of those steps and would survive any test that inspected one alone.
The redaction and bounding assertions are made on **serialized** output for the
same reason: a model field can hold whatever it likes if nothing ever asks what
the report actually says.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from actionwitness_core.contracts.enums import AssertionSeverity, SurfaceDeltaKind
from actionwitness_core.contracts.models import (
    NoUndeclaredChangesPolicy,
    StableToolSurfacePolicy,
)
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.diff import MAX_CHANGE_EXCERPT_CHARS, diff_states
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.engine.findings import Finding
from actionwitness_core.engine.policies import PolicyEvidence, evaluate_policy
from actionwitness_core.evidence.enums import ToolNamespace, ToolReportedStatus
from actionwitness_core.evidence.models import RunEvent
from actionwitness_core.evidence.surface import SurfaceDelta, ToolDefinition
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState
from actionwitness_core.kernel import JsonValue
from actionwitness_core.reports.models import (
    ContractReference,
    OutcomeReport,
    ScenarioReference,
    TargetReference,
    UndeclaredChangesBlock,
    compose_outcome_report,
    undeclared_changes_from,
)
from actionwitness_core.security.canonical import canonicalize
from actionwitness_core.security.limits import TRUNCATION_MARKER
from actionwitness_core.security.redaction import REDACTED, RedactionPolicy, redact
from pydantic import ValidationError

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64

TARGET = TargetReference(id="buggy-store", adapter_id="integrations.buggy_store")
SCENARIO = ScenarioReference(mode="pre_fix", fault_profile="undeclared_side_effect")
CONTRACT = ContractReference(
    id="contract_1", schema_version="1.0", content_hash="sha256:" + "1" * 64
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


def _completed(sequence: int, tool: str, **extra: object) -> RunEvent:
    """A terminal completion. By default it recorded no state movement."""
    extra.setdefault("reported_status", ToolReportedStatus.SUCCESS)
    extra.setdefault("state_hash_before", HASH_A)
    extra.setdefault("state_hash_after", HASH_A)
    return _event(sequence, OutcomeEventType.TOOL_INVOCATION_COMPLETED, tool_name=tool, **extra)


def _mutating(sequence: int, tool: str, **extra: object) -> RunEvent:
    """A completion whose recorded canonical state hashes moved."""
    return _completed(sequence, tool, state_hash_before=HASH_A, state_hash_after=HASH_B, **extra)


def _redacted(payload: dict[str, JsonValue], policy: RedactionPolicy) -> dict[str, JsonValue]:
    """One snapshot as it reaches storage.

    §20.3 redacts "before persistence, hashing, or export", so this is the shape
    both snapshots are already in by the time verification diffs them - and the
    only shape from which an excerpt can be taken.
    """
    result = redact(payload, policy)
    assert isinstance(result, dict)
    return result


def _undeclared_finding(before: dict, after: dict, *, events: tuple[RunEvent, ...] = ()):
    """Run the real pipeline: redact, diff, then judge."""
    policy = RedactionPolicy()
    changes = diff_states(_redacted(before, policy), _redacted(after, policy))
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(), PolicyEvidence(events=events, changes=changes)
    )
    return finding, changes


def _report(**overrides) -> OutcomeReport:
    fields: dict = {
        "run_id": "run_1",
        "target": TARGET,
        "scenario": SCENARIO,
        "contract": CONTRACT,
    }
    fields.update(overrides)
    return compose_outcome_report(**fields)


def _entries(finding) -> list[dict]:
    """The finding's per-path evidence, as it is serialized."""
    entries = finding.evidence["undeclared_changes"]
    assert isinstance(entries, list)
    return entries


# --- FR-159: the paths, with their values ------------------------------------


@pytest.mark.unit
def test_an_undeclared_change_carries_its_bounded_before_and_after_values() -> None:
    """AC-24: the finding "names ... with redacted before and after values"."""
    # Arrange - a journey that quietly rewrote a preference nothing declared.
    before = {"target": {"preferences": {"delivery_note": ""}}}
    after = {"target": {"preferences": {"delivery_note": "leave at door"}}}

    # Act
    finding, _ = _undeclared_finding(before, after)

    # Assert
    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.UNDECLARED_STATE_CHANGE
    assert _entries(finding) == [
        {
            "path": "target.preferences.delivery_note",
            "before": '""',
            "after": '"leave at door"',
            "attributed_cause": "none",
        }
    ]


@pytest.mark.unit
def test_the_report_block_shows_the_same_values_the_finding_recorded() -> None:
    """§23.1's block is a projection of the finding, never a second derivation."""
    # Arrange
    finding, changes = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
    )

    # Act
    block = undeclared_changes_from(finding, changed_paths=len(changes))
    document = block.canonical_document()

    # Assert
    assert document["paths"] == _entries(finding)
    assert document["undeclared"] == 1


@pytest.mark.unit
def test_an_added_path_reports_no_before_value_rather_than_an_empty_one() -> None:
    """`None` is "this side does not exist"; `""` would be a value that does."""
    # Arrange / Act
    finding, _ = _undeclared_finding(
        {"target": {"preferences": {}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
    )

    # Assert
    entry = _entries(finding)[0]
    assert entry["before"] is None
    assert entry["after"] == '"leave at door"'


@pytest.mark.unit
def test_paths_supplied_without_excerpts_report_no_values_rather_than_guessed_ones() -> None:
    """A caller with only paths gets a path list, not an invented pairing.

    §24 replay can restore a case that recorded no excerpts. Losing the values is
    the honest cost; filling them in from somewhere else would put a value in a
    report that no snapshot ever held.
    """
    # Arrange / Act
    finding = evaluate_policy(
        NoUndeclaredChangesPolicy(),
        PolicyEvidence(changed_paths=(ObservationPath.parse("target.preferences.note"),)),
    )

    # Assert
    assert finding.status is CheckStatus.FAILED
    assert _entries(finding) == [
        {
            "path": "target.preferences.note",
            "before": None,
            "after": None,
            "attributed_cause": "none",
        }
    ]


@pytest.mark.unit
def test_two_descriptions_of_one_diff_may_not_disagree() -> None:
    """A partition judging one list while the finding publishes another fails closed."""
    changes = diff_states({"target": {"a": 1}}, {"target": {"a": 2}})

    # Pydantic wraps a validator's `ContractError` in its own `ValidationError`,
    # the same way `LayeredResult`'s layer check surfaces.
    with pytest.raises(ValidationError, match="different diffs"):
        PolicyEvidence(changes=changes, changed_paths=(ObservationPath.parse("target.b"),))


# --- FR-159: attribution by adjacency ----------------------------------------


@pytest.mark.unit
def test_an_undeclared_change_is_attributed_to_the_last_action_that_moved_state() -> None:
    """FR-159's first branch: the action adjacent to the recorded state version."""
    # Arrange - two calls; only the second one's canonical state hashes moved.
    events = (
        _completed(1, "view_cart"),
        _mutating(2, "apply_discount"),
    )

    # Act
    finding, changes = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
        events=events,
    )

    # Assert - the mutating call is named, in the finding and in the report.
    assert finding.attributed_cause is not None
    assert finding.attributed_cause["kind"] == "tool_action"
    assert finding.attributed_cause["tool_name"] == "apply_discount"
    assert finding.attributed_cause["event_sequence"] == 2
    block = undeclared_changes_from(finding, changed_paths=len(changes))
    assert block.canonical_document()["paths"] == [
        {
            "path": "target.preferences.delivery_note",
            "before": '""',
            "after": '"leave at door"',
            "attributed_cause": "tool_action:apply_discount@2",
        }
    ]


@pytest.mark.unit
def test_an_executed_action_that_moved_nothing_is_never_blamed() -> None:
    """FR-159: `none` "is exactly what a change from an unrelated background process
    should produce".

    The counterfactual that matters. A run *did* execute a tool, so a rule that
    blamed the last action full stop would name it - and would name an innocent
    call in every run where a background process edited state. Only a recorded
    state-version movement makes an action adjacent to a change.
    """
    # Arrange - one completed call whose canonical state hashes did not move.
    events = (_completed(1, "view_cart"),)

    # Act
    finding, _ = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
        events=events,
    )

    # Assert
    assert finding.attributed_cause is not None
    assert finding.attributed_cause["kind"] == "none"
    assert _entries(finding)[0]["attributed_cause"] == "none"


@pytest.mark.unit
def test_a_completion_that_recorded_no_hashes_is_not_treated_as_a_mutation() -> None:
    """Absent evidence attributes to nothing; it never becomes an accusation."""
    # Arrange - FR-032 requires hashes on a mutation, and this call recorded none.
    events = (_event(1, OutcomeEventType.TOOL_INVOCATION_COMPLETED, tool_name="view_cart"),)

    # Act
    finding, _ = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
        events=events,
    )

    # Assert
    assert finding.attributed_cause is not None
    assert finding.attributed_cause["kind"] == "none"


@pytest.mark.unit
def test_a_recorded_human_decision_is_attributed_when_no_action_moved_state() -> None:
    """FR-159's second branch, and §9.10's "a human action recorded in the same run"."""
    # Arrange
    events = (
        _event(
            1,
            OutcomeEventType.CONFIRMATION_APPROVED,
            actor=EventActor.HUMAN,
            correlation_id="req_1",
        ),
    )

    # Act
    finding, changes = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
        events=events,
    )

    # Assert
    assert finding.attributed_cause is not None
    assert finding.attributed_cause["kind"] == "human_confirmation"
    block = undeclared_changes_from(finding, changed_paths=len(changes))
    entry = block.canonical_document()["paths"][0]
    assert entry["attributed_cause"] == "human_confirmation@1"


@pytest.mark.unit
def test_attribution_does_not_reorder_which_failure_a_report_names_as_primary() -> None:
    """Adjacency is a likelihood; §22's order belongs to established causes.

    §22 sorts failures by causal event sequence, so writing an adjacency guess
    into `causal_event_sequence` would let it outrank a classification that
    actually proved its cause.
    """
    finding, _ = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
        events=(_mutating(2, "apply_discount"),),
    )

    assert finding.attributed_cause is not None
    assert finding.attributed_cause["event_sequence"] == 2
    assert finding.causal_event_sequence is None


# --- §20.3 and §11.4: what a report is allowed to carry ----------------------


@pytest.mark.unit
def test_a_redacted_value_never_reaches_the_finding_or_the_report() -> None:
    """§20.3, asserted on the bytes rather than on the model.

    An excerpt is taken from the *stored* snapshot, which was redacted before it
    was persisted. Asserting that the model field "looks redacted" would pass
    against a report that also serialized the original somewhere else, so the
    whole canonical document is searched for the secret.
    """
    # Arrange - a subtree appears, and one of its fields is a default-redacted key.
    secret = "shopper@example.com"
    before: dict[str, JsonValue] = {"target": {"customer": None}}
    after: dict[str, JsonValue] = {"target": {"customer": {"name": "Ada", "email": secret}}}

    # Act
    finding, changes = _undeclared_finding(before, after)
    block = undeclared_changes_from(finding, changed_paths=len(changes))
    document = _report(policy_findings=[finding], undeclared_changes=block).canonical_document()
    serialized = canonicalize(document).decode("utf-8")

    # Assert - the marker is carried where the value was, and the value is gone.
    entry = _entries(finding)[0]
    assert entry["path"] == "target.customer"
    assert REDACTED in str(entry["after"])
    assert secret not in str(entry["after"])
    assert secret not in serialized
    assert REDACTED in serialized


@pytest.mark.unit
def test_a_contract_declared_redaction_pattern_is_honoured_too() -> None:
    """§20.3: contract patterns apply "in addition to defaults"."""
    # Arrange
    secret = "leave the key under the mat"
    policy = RedactionPolicy.from_paths(["target.**.delivery_note"])
    before = _redacted({"target": {"preferences": {"delivery_note": ""}}}, policy)
    after = _redacted({"target": {"preferences": {"delivery_note": secret}}}, policy)

    # Act - the redacted snapshots are identical, so nothing changed to report.
    changes = diff_states(before, after)
    finding = evaluate_policy(NoUndeclaredChangesPolicy(), PolicyEvidence(changes=changes))

    # Assert
    assert finding.status is CheckStatus.PASSED
    assert secret not in canonicalize(dict(finding.evidence)).decode("utf-8")


@pytest.mark.unit
def test_an_excerpt_stays_inside_the_finding_value_budget() -> None:
    """§11.4: a report never carries an unbounded payload copied out of a snapshot."""
    # Arrange - a value far larger than the budget.
    huge = "x" * (MAX_CHANGE_EXCERPT_CHARS * 5)

    # Act
    finding, changes = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": huge}}},
    )
    block = undeclared_changes_from(finding, changed_paths=len(changes))

    # Assert - truncated, marked as truncated, and never carried whole.
    excerpt = str(_entries(finding)[0]["after"])
    assert len(excerpt) <= MAX_CHANGE_EXCERPT_CHARS
    assert excerpt.endswith(TRUNCATION_MARKER)
    assert huge not in canonicalize(block.canonical_document()).decode("utf-8")


# --- a finding rebuilt from a stored row is untrusted input ------------------


def _stored(paths: tuple[str, ...], entries: JsonValue):
    """A finding as it comes back off a row, with evidence somebody could tamper."""
    return Finding(
        check_id="no_undeclared_changes",
        check_type=CheckType.POLICY,
        status=CheckStatus.FAILED,
        severity=AssertionSeverity.CRITICAL,
        classification=FailureClassification.UNDECLARED_STATE_CHANGE,
        paths=tuple(ObservationPath.parse(path) for path in paths),
        evidence={"undeclared_changes": entries},
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "entries",
    [
        pytest.param("not a list", id="not-a-list"),
        pytest.param([["target.a"]], id="entry-is-not-an-object"),
        pytest.param([{"path": "not a path", "attributed_cause": "none"}], id="unparseable-path"),
        pytest.param(
            [{"path": "target.a", "after": "x" * (MAX_CHANGE_EXCERPT_CHARS + 1)}],
            id="excerpt-over-budget",
        ),
    ],
)
def test_unusable_stored_evidence_falls_back_to_the_paths_it_can_trust(entries) -> None:
    """§17.1's `paths` column is the authority; the evidence list only enriches it.

    A row that cannot be validated must not silently drop a path the finding
    says changed, and an over-budget excerpt must not widen what the report
    carries just because it arrived from storage.
    """
    block = undeclared_changes_from(_stored(("target.a",), entries), changed_paths=1)

    assert block.canonical_document()["paths"] == [
        {"path": "target.a", "before": None, "after": None, "attributed_cause": "none"}
    ]


@pytest.mark.unit
def test_stored_evidence_naming_different_paths_is_refused() -> None:
    """A finding whose two halves disagree publishes the half §17.1 defines."""
    entries = [{"path": "target.b", "before": '"x"', "after": '"y"'}]

    block = undeclared_changes_from(_stored(("target.a",), entries), changed_paths=1)

    assert [entry.path for entry in block.paths] == [ObservationPath.parse("target.a")]
    assert block.paths[0].before is None


# --- §23.1: the block round-trips and hashes deterministically ---------------


@pytest.mark.unit
def test_the_undeclared_block_round_trips_through_serialization() -> None:
    """A stored report must rebuild into the model that wrote it."""
    # Arrange
    finding, changes = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
        events=(_mutating(2, "apply_discount"),),
    )
    block = undeclared_changes_from(finding, changed_paths=len(changes))

    # Act
    restored = UndeclaredChangesBlock.model_validate(block.model_dump())

    # Assert
    assert restored == block
    assert restored.canonical_document() == block.canonical_document()


@pytest.mark.unit
def test_the_same_undeclared_evidence_hashes_identically_every_time() -> None:
    """§17.2: a hash that varied could not anchor an evidence chain."""

    # Arrange
    def compose() -> OutcomeReport:
        finding, changes = _undeclared_finding(
            {"target": {"preferences": {"delivery_note": "", "nickname": "a"}}},
            {"target": {"preferences": {"delivery_note": "leave at door", "nickname": "b"}}},
            events=(_mutating(2, "apply_discount"),),
        )
        return _report(
            policy_findings=[finding],
            undeclared_changes=undeclared_changes_from(finding, changed_paths=len(changes)),
        )

    # Act
    first, second = compose(), compose()

    # Assert
    assert canonicalize(first.canonical_document()) == canonicalize(second.canonical_document())
    assert first.content_hash() == second.content_hash()


@pytest.mark.unit
def test_carrying_the_values_changes_the_report_hash() -> None:
    """The evidence is inside the hashed document, not beside it."""
    # Arrange
    finding, changes = _undeclared_finding(
        {"target": {"preferences": {"delivery_note": ""}}},
        {"target": {"preferences": {"delivery_note": "leave at door"}}},
    )
    with_values = undeclared_changes_from(finding, changed_paths=len(changes))
    without_values = undeclared_changes_from(
        finding.model_copy(update={"evidence": {}}), changed_paths=len(changes)
    )

    # Assert
    assert _report(undeclared_changes=with_values).content_hash() != (
        _report(undeclared_changes=without_values).content_hash()
    )


# --- §9.5: a warned surface delta is visible --------------------------------


def _delta(kind: SurfaceDeltaKind, *, tool: str = "apply_discount") -> SurfaceDelta:
    definition = ToolDefinition(name=tool, namespace=ToolNamespace.TARGET)
    return SurfaceDelta(
        tool_name=tool,
        namespace=ToolNamespace.TARGET,
        kind=kind,
        before=definition,
        after=definition,
    )


def _surface_finding(*deltas: SurfaceDelta):
    return evaluate_policy(
        StableToolSurfacePolicy(),
        PolicyEvidence(surface_baseline_recorded=True, observed_surface_deltas=deltas),
    )


@pytest.mark.unit
def test_a_description_only_delta_passes_the_policy_and_still_counts_a_warning() -> None:
    """§9.5 warns rather than fails - but §23.1 has to say that a warning happened."""
    # Arrange
    finding = _surface_finding(_delta(SurfaceDeltaKind.DESCRIPTION_CHANGE))

    # Act
    report = _report(policy_findings=[finding])

    # Assert - the policy held, and the run says so with a qualification.
    assert finding.status is CheckStatus.PASSED
    assert finding.check_id == "stable_tool_surface"
    assert finding.classification is None
    assert report.status is RunState.PASSED_WITH_WARNINGS
    assert report.counts.warnings == 1
    assert report.counts.critical_failures == 0


@pytest.mark.unit
def test_the_warning_names_the_tool_whose_description_drifted() -> None:
    """A count with no subject sends a reader back to the raw timeline."""
    finding = _surface_finding(_delta(SurfaceDeltaKind.DESCRIPTION_CHANGE, tool="apply_discount"))

    warnings = finding.evidence["warnings"]
    assert isinstance(warnings, list)
    assert "apply_discount" in str(warnings[0])
    assert "description_change" in str(warnings[0])


@pytest.mark.unit
def test_two_drifting_tools_count_as_two_warnings() -> None:
    """Collapsing them to one kind would report that a single tool moved."""
    finding = _surface_finding(
        _delta(SurfaceDeltaKind.DESCRIPTION_CHANGE, tool="apply_discount"),
        _delta(SurfaceDeltaKind.DESCRIPTION_CHANGE, tool="update_cart"),
    )

    assert _report(policy_findings=[finding]).counts.warnings == 2


@pytest.mark.unit
def test_a_run_with_no_warning_still_reports_a_plain_passed() -> None:
    """The counterfactual: `passed_with_warnings` must mean something happened."""
    # Arrange
    finding = _surface_finding()

    # Act
    report = _report(policy_findings=[finding])

    # Assert
    assert finding.status is CheckStatus.PASSED
    assert report.status is RunState.PASSED
    assert report.counts.warnings == 0


@pytest.mark.unit
def test_a_failing_surface_delta_still_fails_the_run() -> None:
    """Making a warning visible must not soften a real mutation."""
    # Arrange
    finding = _surface_finding(_delta(SurfaceDeltaKind.SCHEMA_CHANGE))

    # Act
    report = _report(policy_findings=[finding])

    # Assert
    assert finding.status is CheckStatus.FAILED
    assert finding.classification is FailureClassification.TOOL_SURFACE_MUTATION
    assert report.status is RunState.FAILED
    assert report.counts.critical_failures == 1


@pytest.mark.unit
def test_a_warning_never_moves_the_safety_policy_layer() -> None:
    """§23.1's closed set: `safety_policy` may report only passed, failed, or error."""
    finding = _surface_finding(_delta(SurfaceDeltaKind.DESCRIPTION_CHANGE))

    layers = _report(policy_findings=[finding]).layers
    assert layers.safety_policy.value == "passed"


@pytest.mark.unit
def test_a_warning_recorded_beside_a_critical_failure_does_not_hide_the_failure() -> None:
    """A run that both warned and failed is a failed run that also warned."""
    # Arrange - a schema change fails, a description change warns, in one policy.
    finding = _surface_finding(
        _delta(SurfaceDeltaKind.SCHEMA_CHANGE, tool="update_cart"),
        _delta(SurfaceDeltaKind.DESCRIPTION_CHANGE, tool="apply_discount"),
    )

    # Act
    report = _report(policy_findings=[finding])

    # Assert
    assert report.status is RunState.FAILED
    assert report.counts.critical_failures == 1
    assert report.counts.warnings == 1
