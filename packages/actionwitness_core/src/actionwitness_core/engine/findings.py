"""Findings, severity aggregation, and deterministic primary-failure ordering.

Spec v1.9 §17.1 (`findings` columns), §22 (the primary-failure rule), FR-052
(severity), FR-053 (overall result); constitution §5 (an observation failure
"produces an explicit non-pass result; it never degrades to success").

Two rules here are load-bearing far beyond their size.

**Ordering is total.** §22 selects one `primary_failure` by "highest severity
first, then lowest causal event sequence, then lexical `check_id`", with a
finding that has no causal event sorting after every finding that has one. If any
two findings could tie, the displayed primary failure would depend on iteration
order, and two replays of one recorded run would disagree about what went wrong.
The `check_id` tie-break is what closes that: no two findings in a run share one.

**An unresolved check is not a pass.** A critical check whose evidence never
arrived contributes a failure classified `observation_unavailable` rather than
being skipped, because skipping it produces a green run that verified nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated

from pydantic import Field, StringConstraints

from actionwitness_core.contracts.enums import AssertionSeverity
from actionwitness_core.contracts.paths import ObservationPathField
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.kernel import CoreModel, JsonValue
from actionwitness_core.reports.enums import LayerResult

__all__ = [
    "SEVERITY_RANK",
    "Finding",
    "aggregate",
    "order_failures",
    "primary_failure",
]

#: Higher sorts first when choosing a primary failure (§22).
SEVERITY_RANK: Mapping[AssertionSeverity, int] = {
    AssertionSeverity.CRITICAL: 3,
    AssertionSeverity.WARNING: 2,
    AssertionSeverity.INFO: 1,
}

#: Statuses that count against a run. `not_evaluated` is excluded - nothing was
#: judged - while `observation_unavailable` is included, because a check whose
#: evidence is missing is unresolved, and unresolved is not passed.
_FAILING_STATUSES: frozenset[CheckStatus] = frozenset(
    {CheckStatus.FAILED, CheckStatus.OBSERVATION_UNAVAILABLE}
)


class Finding(CoreModel):
    """One evaluated check and its evidence (§17.1 `findings`).

    `path` and `paths` are deliberately separate: §17.1 says a finding concerning
    exactly one path sets `path` and leaves the list null, while
    `undeclared_state_change` "emits one finding per run, listing every
    undeclared path", so the critical classification set stays stable however
    many paths changed.
    """

    check_id: Annotated[str, StringConstraints(min_length=1)]
    check_type: CheckType
    status: CheckStatus
    severity: AssertionSeverity
    classification: FailureClassification | None = None
    path: ObservationPathField | None = None
    paths: tuple[ObservationPathField, ...] = ()
    applied_exemptions: tuple[ObservationPathField, ...] = ()
    attributed_cause: Mapping[str, JsonValue] | None = None
    expected: JsonValue = None
    actual: JsonValue = None
    #: The sequence number of the event this finding is attributed to, when one
    #: exists. `None` means "attributed to no action", which §22 sorts last.
    causal_event_sequence: Annotated[int, Field(ge=0)] | None = None
    evidence: Mapping[str, JsonValue] = Field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status in _FAILING_STATUSES

    def sort_key(self) -> tuple[int, int, int, str]:
        """§22's deterministic ordering, as a total order.

        The second element is the "no causal event sorts last" flag; without it a
        `None` sequence would have to be compared with an integer, and any
        substitute value would collide with a real sequence number.
        """
        return (
            -SEVERITY_RANK[self.severity],
            1 if self.causal_event_sequence is None else 0,
            self.causal_event_sequence if self.causal_event_sequence is not None else 0,
            self.check_id,
        )


def order_failures(findings: Sequence[Finding]) -> tuple[Finding, ...]:
    """Every failing finding, in §22's deterministic order."""
    return tuple(sorted((f for f in findings if f.failed), key=Finding.sort_key))


def primary_failure(findings: Sequence[Finding]) -> Finding | None:
    """The one finding a report displays as `primary_failure` (§22).

    "This display field does not replace the complete finding or classification
    set used by eval expectations" - callers comparing runs must compare the
    whole set, not this.
    """
    ordered = order_failures(findings)
    return ordered[0] if ordered else None


def critical_classifications(findings: Sequence[Finding]) -> tuple[FailureClassification, ...]:
    """The sorted, de-duplicated critical classification set (§24.1, AC-15).

    Sorted and de-duplicated because eval expectations compare this set exactly:
    a set that varied with evaluation order would make a faithful replay look
    like a different failure.
    """
    return tuple(
        sorted(
            {
                finding.classification
                for finding in findings
                if finding.failed
                and finding.severity is AssertionSeverity.CRITICAL
                and finding.classification is not None
            }
        )
    )


def aggregate(findings: Sequence[Finding]) -> LayerResult:
    """Reduce findings to one layer result (FR-053).

    "A run shall fail when any critical assertion, policy, or required
    observed-trajectory check fails. Warning-only assertion mismatches shall
    yield `passed_with_warnings`; info-only mismatches shall leave the result
    `passed` while remaining visible in the report."
    """
    failures = [finding for finding in findings if finding.failed]
    if any(finding.severity is AssertionSeverity.CRITICAL for finding in failures):
        return LayerResult.FAILED
    if any(finding.severity is AssertionSeverity.WARNING for finding in failures):
        return LayerResult.PASSED_WITH_WARNINGS
    return LayerResult.PASSED
