"""Closed evidence vocabulary: where a value came from, and what a tool claimed.

Spec v1.9 §9.3 (the three required MVP evidence kinds), FR-032 (`reported_status`
closed values), FR-044 (trusted-state priority); constitution §4 ("Tool-reported
output and authoritative observations use distinct stored types and source
classifications" and "A successful tool response must never be persisted as
manufactured observed state").

`EvidenceSourceClassification` is the product's central distinction rendered as
data. Every stored value knows whether it was *claimed* by the thing under test
or *observed* independently of it, and only the second kind can settle an
assertion. Losing that label anywhere in the pipeline collapses the two channels
the harness exists to compare.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "AUTHORITATIVE_SOURCES",
    "ENUM_REGISTRATIONS",
    "EvidenceSourceClassification",
    "ToolReportedStatus",
]


class EvidenceSourceClassification(StrEnum):
    """Which channel produced a stored value (spec §9.3).

    The three members are exactly the required MVP evidence of §9.3. `ui_state`
    is deliberately absent: §9.3 admits it only as an optional supporting
    provider behind a versioned submission endpoint that does not exist, and a
    classification nothing can produce is a false promise of independence.
    """

    AUTHORITATIVE_OBSERVATION = "authoritative_observation"
    TOOL_REPORTED = "tool_reported"
    JOURNEY_EVENTS = "journey_events"


EVIDENCE_SOURCE_DESCRIPTIONS: Mapping[EvidenceSourceClassification, str] = {
    EvidenceSourceClassification.AUTHORITATIVE_OBSERVATION: (
        "Canonical target state captured by the selected observation provider. "
        "The only source that can settle an assertion (FR-044)."
    ),
    EvidenceSourceClassification.TOOL_REPORTED: (
        "A bounded summary of what a tool said about its own call. Evidence about "
        "the channel under test, never proof of its effect."
    ),
    EvidenceSourceClassification.JOURNEY_EVENTS: (
        "Recorded tool and confirmation activity. Consumed by policy evaluation and "
        "failure classification rather than resolved through an assertion path."
    ),
}

#: The sources an assertion verdict may rest on. Exactly one member today; it is a
#: set so that the rule reads as a rule rather than as an equality check that a
#: later provider could quietly widen.
AUTHORITATIVE_SOURCES: frozenset[EvidenceSourceClassification] = frozenset(
    {EvidenceSourceClassification.AUTHORITATIVE_OBSERVATION}
)


class ToolReportedStatus(StrEnum):
    """What a terminal tool invocation claimed about itself (FR-032).

    Required on `tool_invocation_completed` and omitted by the failed and
    cancelled event types, which carry their outcome in the event name. This is
    the self-report: `success` here is the input to false-success detection, not
    a verdict.
    """

    SUCCESS = "success"
    BLOCKED_BY_USER = "blocked_by_user"
    BLOCKED_BY_EXPIRY = "blocked_by_expiry"
    ALREADY_APPLIED = "already_applied"


TOOL_REPORTED_STATUS_DESCRIPTIONS: Mapping[ToolReportedStatus, str] = {
    ToolReportedStatus.SUCCESS: (
        "The tool reported that it did what it was asked. Independent observation "
        "decides whether that is true."
    ),
    ToolReportedStatus.BLOCKED_BY_USER: (
        "A human denied the protected mutation; no state changed and the consent policy passes."
    ),
    ToolReportedStatus.BLOCKED_BY_EXPIRY: (
        "The confirmation expired unresolved; no state changed and this is not a "
        "tool-execution failure."
    ),
    ToolReportedStatus.ALREADY_APPLIED: (
        "A retry under the original request ID returned the first persisted result "
        "rather than repeating the mutation."
    ),
}


ENUM_REGISTRATIONS: tuple[tuple[str, str, type[StrEnum], Mapping[StrEnum, str]], ...] = (
    (
        "evidence_source_classification",
        "spec §9.3 / FR-044",
        EvidenceSourceClassification,
        EVIDENCE_SOURCE_DESCRIPTIONS,
    ),
    ("tool_reported_status", "FR-032", ToolReportedStatus, TOOL_REPORTED_STATUS_DESCRIPTIONS),
)
