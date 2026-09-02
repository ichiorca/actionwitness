"""What an audited surface claimed, and what was independently observed (015-T4).

Two kinds of evidence arrive from the operator's browser and they are kept
apart, all the way through, because the entire feature is the comparison between
them:

* a **tool report** is what the site's own agent tool said it did;
* an **observation** is a `cart.js` session read, normalized by the adapter.

The constitution requires "tool-reported output and authoritative observations
use distinct stored types and source classifications", and §12.17 exists because
the interesting sites are the ones where the two disagree. So there is no
function here that turns one into the other, and `AuditFinding` is derived from
holding both rather than from trusting either.

**Nothing here fetches anything.** FR-160a puts every read in the operator's own
browser session. This module receives, validates, and compares.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "AuditFinding",
    "ToolOutcome",
    "audit_findings",
]


class ToolOutcome(StrEnum):
    """What one audited tool turned out to do.

    `silently_failed` is the whole reason this product exists: the tool answered
    with a success, and the independent read says nothing moved.

    `unobserved` is separate from every other value on purpose. It means the
    harness could not read the state this tool claims to change, so it is not a
    verdict about the tool at all — and collapsing it into `worked` would turn
    "we could not check" into "it is fine", which is the failure §16.1 spends a
    whole section preventing.
    """

    WORKED = "worked"
    SILENTLY_FAILED = "silently_failed"
    FAILED_OUTRIGHT = "failed_outright"
    UNOBSERVED = "unobserved"
    NOT_EXERCISED = "not_exercised"
    ABSENT = "absent"


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One tool, and what the audit can honestly say about it."""

    tool_name: str
    outcome: ToolOutcome
    #: Present only when the tool was exercised. Kept verbatim so a reader can
    #: see the claim beside the observation rather than a summary of it.
    reported: str | None = None
    observed_before: Mapping[str, Any] | None = None
    observed_after: Mapping[str, Any] | None = None

    def as_document(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "outcome": self.outcome.value,
            "reported": self.reported,
            "observed_before": None if self.observed_before is None else dict(self.observed_before),
            "observed_after": None if self.observed_after is None else dict(self.observed_after),
        }


def audit_findings(
    *,
    enumerated: Sequence[str],
    expected: Sequence[str],
    reports: Mapping[str, Mapping[str, Any]],
    observed_before: Mapping[str, Any] | None,
    observed_after: Mapping[str, Any] | None,
    never_invoked: Sequence[str],
) -> tuple[AuditFinding, ...]:
    """Classify every tool the pack cares about (§12.17, FR-163).

    The order of the checks is the meaning:

    1. **Never invoked** wins over everything. A tool the pack refuses to
       exercise gets no verdict, whatever else is true, because a verdict would
       imply somebody tried.
    2. **Absent** next: a tool the surface never published cannot have an
       outcome, and reporting one as failing would accuse a merchant of breaking
       something they never had.
    3. **Not exercised**: present, allowed, and this journey did not call it.
       Distinct from working.
    4. **Failed outright** before any state comparison: a tool that returned an
       error made no claim to contradict.
    5. **Unobserved** before `worked`: without both reads there is nothing to
       compare, and §16.1 forbids turning "could not check" into a pass.
    6. Only then does a reported success get weighed against the observation.

    Ordered by tool name so two audits of the same surface produce the same
    report.
    """
    forbidden = set(never_invoked)
    present = set(enumerated)
    exercised = set(expected)

    findings: list[AuditFinding] = []
    for tool in sorted(set(enumerated) | set(expected) | forbidden):
        if tool in forbidden:
            findings.append(
                AuditFinding(
                    tool_name=tool,
                    outcome=(ToolOutcome.NOT_EXERCISED if tool in present else ToolOutcome.ABSENT),
                )
            )
            continue
        if tool not in present:
            findings.append(AuditFinding(tool_name=tool, outcome=ToolOutcome.ABSENT))
            continue
        if tool not in exercised:
            findings.append(AuditFinding(tool_name=tool, outcome=ToolOutcome.NOT_EXERCISED))
            continue

        report = reports.get(tool)
        if report is None:
            findings.append(AuditFinding(tool_name=tool, outcome=ToolOutcome.NOT_EXERCISED))
            continue

        summary = _summary(report)
        if not _claimed_success(report):
            findings.append(
                AuditFinding(tool_name=tool, outcome=ToolOutcome.FAILED_OUTRIGHT, reported=summary)
            )
            continue

        if observed_before is None or observed_after is None:
            findings.append(
                AuditFinding(tool_name=tool, outcome=ToolOutcome.UNOBSERVED, reported=summary)
            )
            continue

        changed = observed_before != observed_after
        # A read tool that changed nothing is working correctly; a write tool
        # that changed nothing while reporting success is the defect. The pack
        # says which is which, and this function is told rather than guessing:
        # inferring "write" from a name would make `update_cart` and
        # `update_preferences` mean the same thing on a surface nobody wrote.
        expects_change = bool(report.get("expects_state_change"))
        if expects_change and not changed:
            findings.append(
                AuditFinding(
                    tool_name=tool,
                    outcome=ToolOutcome.SILENTLY_FAILED,
                    reported=summary,
                    observed_before=observed_before,
                    observed_after=observed_after,
                )
            )
            continue

        findings.append(
            AuditFinding(
                tool_name=tool,
                outcome=ToolOutcome.WORKED,
                reported=summary,
                observed_before=observed_before,
                observed_after=observed_after,
            )
        )
    return tuple(findings)


def _claimed_success(report: Mapping[str, Any]) -> bool:
    """Whether the tool said it worked.

    An explicit error flag is believed; anything else is treated as a claim of
    success. That asymmetry is deliberate: a tool that failed and did not say so
    is indistinguishable from one that succeeded, and the state comparison below
    is what catches it either way.
    """
    return not bool(report.get("is_error"))


def _summary(report: Mapping[str, Any]) -> str:
    """The tool's own words, bounded.

    Kept verbatim rather than paraphrased — the report puts the claim beside the
    observation so a reader can judge the disagreement themselves, and a summary
    of a claim is no longer the claim.
    """
    from actionwitness_core.security.limits import MAX_FINDING_VALUE_CHARS, bounded_summary

    text = report.get("summary")
    return bounded_summary(text if isinstance(text, str) else "", MAX_FINDING_VALUE_CHARS).text
