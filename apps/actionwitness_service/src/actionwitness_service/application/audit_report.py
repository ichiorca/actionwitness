"""The site-owner report (§5, §12.17, FR-163; 015-T5).

FR-163: a report "addressed to a site owner rather than a harness engineer:
which agent tools work, which report success without a corresponding state
change, which fail outright, what an agent can change, and what the operator
should do next. It shall state its own limits plainly."

**The summary contains no harness vocabulary, and that is enforced rather than
intended.** A merchant does not have a contract, a policy, an assertion, or a
classification; they have a storefront and customers. `SUMMARY_FORBIDDEN_WORDS`
is checked by a test, because prose drifts back toward the vocabulary of
whoever writes it and this section is the one place that must not.

**Consequences first, mechanism second.** The order is the point: a reader who
stops after the first paragraph should already know whether an agent can take a
customer's money without their cart changing. The engineer-grade evidence — the
tool's exact words beside the observed cart — sits underneath, for whoever gets
handed the problem next.

**A passing audit is not a guarantee.** FR-163 requires the report to say so
plainly, and `LIMITS` says it in the report's own voice rather than in a
footnote. An audit that let a merchant believe otherwise would be worse than no
audit, because they would stop looking.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from actionwitness_service.application.audit_evidence import AuditFinding, ToolOutcome

__all__ = [
    "LIMITS",
    "SUMMARY_FORBIDDEN_WORDS",
    "compose_audit_report",
]

#: Words that belong to the harness, not to a shop owner.
#:
#: Checked against the summary section by `tests/integration/test_audit_report.py`.
#: Every one of them has a plain-language equivalent used instead, and a reader
#: who needs the harness word can find it in the evidence section below.
SUMMARY_FORBIDDEN_WORDS: Final[frozenset[str]] = frozenset(
    {
        "assertion",
        "classification",
        "contract",
        "critical",
        "finding",
        "harness",
        "observation_unavailable",
        "policy",
        "provenance",
        "run_id",
        "verdict",
        "workspace",
    }
)

#: FR-163's plain statement of limits, in the report's own voice.
LIMITS: Final[tuple[str, ...]] = (
    "This checked one journey through your store, not everything an agent could do.",
    "Tools listed as not checked were deliberately left alone — see below for why.",
    "A clean result here is evidence about what was tried, not a guarantee about "
    "your whole storefront.",
)

#: The sentence each outcome earns, written for the person who owns the shop.
_HEADLINE: Final[dict[ToolOutcome, str]] = {
    ToolOutcome.SILENTLY_FAILED: (
        "reported that it worked, but your store did not change. An agent — and "
        "your customer — would believe this succeeded."
    ),
    ToolOutcome.WORKED: "worked, and your store changed the way it said it did.",
    ToolOutcome.FAILED_OUTRIGHT: (
        "returned an error. An agent would see the failure and could tell your "
        "customer, which is the safer of the two ways to be broken."
    ),
    ToolOutcome.UNOBSERVED: (
        "could not be checked, because there was no independent way to read what "
        "it changed. This is not a pass."
    ),
    ToolOutcome.NOT_EXERCISED: (
        "is available to agents but was not tried. Nothing here says whether it works."
    ),
    ToolOutcome.ABSENT: "was not published by your store.",
}

#: What a merchant should do about each outcome, in the order urgency demands.
_ADVICE: Final[dict[ToolOutcome, str]] = {
    ToolOutcome.SILENTLY_FAILED: (
        "Fix this first. Until it is fixed, an agent acting for a customer will "
        "report success for something that did not happen."
    ),
    ToolOutcome.FAILED_OUTRIGHT: "Worth fixing, but a customer is told it failed.",
    ToolOutcome.UNOBSERVED: (
        "Check this by hand, or expose a way to read the state it changes so it "
        "can be checked next time."
    ),
    ToolOutcome.NOT_EXERCISED: (
        "Nothing to do. It was left alone on purpose, because trying it could "
        "have created a real order or touched a real customer's account."
    ),
    ToolOutcome.WORKED: "Nothing to do.",
    ToolOutcome.ABSENT: "Nothing to do.",
}

#: Urgency order. A merchant reading top-down should meet the thing that is
#: quietly costing them orders before the things that are merely absent.
_SEVERITY: Final[dict[ToolOutcome, int]] = {
    ToolOutcome.SILENTLY_FAILED: 0,
    ToolOutcome.FAILED_OUTRIGHT: 1,
    ToolOutcome.UNOBSERVED: 2,
    ToolOutcome.NOT_EXERCISED: 3,
    ToolOutcome.WORKED: 4,
    ToolOutcome.ABSENT: 5,
}


def compose_audit_report(
    *,
    authorized_origin: str,
    pack_id: str | None,
    pack_title: str | None,
    findings: Sequence[AuditFinding],
) -> dict[str, Any]:
    """FR-163's report: consequences first, evidence behind, limits stated.

    `pack_id` and `pack_title` are carried because FR-161 requires the report to
    "name which pack was applied" — a reader who cannot tell which journey was
    tried cannot tell what the result covers.
    """
    ordered = sorted(findings, key=lambda f: (_SEVERITY[f.outcome], f.tool_name))
    silent = [f for f in ordered if f.outcome is ToolOutcome.SILENTLY_FAILED]
    unchecked = [f for f in ordered if f.outcome is ToolOutcome.UNOBSERVED]
    untried = [f for f in ordered if f.outcome is ToolOutcome.NOT_EXERCISED]

    return {
        "schema_version": "1.0",
        "audited_site": authorized_origin,
        # FR-161: the report names the pack, so a reader knows what was tried.
        "checked_using": pack_title or "a journey chosen for this store",
        "checked_using_id": pack_id,
        "summary": {
            "headline": _headline(silent, unchecked),
            "what_this_means": _consequence(silent),
            "tools": [
                {
                    "tool": finding.tool_name,
                    "says": _HEADLINE[finding.outcome],
                    "what_to_do": _ADVICE[finding.outcome],
                }
                for finding in ordered
            ],
            "not_checked": [finding.tool_name for finding in untried + unchecked],
            "limits": list(LIMITS),
        },
        # Everything above is for the shop owner. Everything below is for
        # whoever they forward it to, and it is the same data rather than a
        # second derivation of it.
        "evidence": [finding.as_document() for finding in ordered],
    }


def _headline(silent: Sequence[AuditFinding], unchecked: Sequence[AuditFinding]) -> str:
    """The first sentence, which is the only one some readers will read."""
    if silent:
        names = ", ".join(finding.tool_name for finding in silent)
        return (
            f"{len(silent)} of your store's agent tools said they worked when they "
            f"had not: {names}."
        )
    if unchecked:
        return (
            "Nothing was found doing the wrong thing, but some tools could not be "
            "checked independently, so this is not a clean bill of health."
        )
    return "Every tool that was tried did what it said it did."


def _consequence(silent: Sequence[AuditFinding]) -> str:
    """What it means for the person who owns the shop.

    Concrete rather than hedged. A merchant deciding whether to stop what they
    are doing needs the consequence, and "a state mismatch was detected" is not
    one.
    """
    if not silent:
        return (
            "On this journey, an agent shopping on your store was told the truth "
            "about what it had done."
        )
    return (
        "An agent shopping on your store can be told an item was added when it was "
        "not. A customer using that agent would reach checkout expecting something "
        "their basket does not contain — and neither they nor the agent would see "
        "anything wrong until it was too late."
    )
