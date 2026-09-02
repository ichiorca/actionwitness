"""015-T4 — classifying what an audited surface actually does (§12.17, FR-163).

The classification exists to make one distinction survivable in a report a
merchant reads: a tool that *reported success and changed nothing* is a
different thing from a tool that failed, from a tool nobody called, and from a
tool the harness could not check.

Collapsing any of those into "worked" is how an audit becomes reassurance. So
each has its own outcome and its own test, and the ordering of the checks is
tested too — a tool that is both forbidden and absent must report as absent
rather than as a verdict nobody earned.
"""

from __future__ import annotations

from typing import Any

import pytest
from actionwitness_service.application.audit_evidence import (
    AuditFinding,
    audit_findings,
)

pytestmark = pytest.mark.unit

BEFORE: dict[str, Any] = {"cart": {"item_count": 0, "total": "0.00"}}
AFTER: dict[str, Any] = {"cart": {"item_count": 1, "total": "25.99"}}

ENUMERATED = ["get_cart", "search_catalog", "update_cart", "proceed_to_checkout"]
NEVER = ["proceed_to_checkout", "manage_orders"]


def outcomes(findings: tuple[AuditFinding, ...]) -> dict[str, str]:
    return {finding.tool_name: finding.outcome.value for finding in findings}


def report(**over: Any) -> dict[str, Any]:
    return {"summary": '{"status":"success"}', "expects_state_change": True, **over}


def classify(**over: Any) -> tuple[AuditFinding, ...]:
    kwargs: dict[str, Any] = {
        "enumerated": ENUMERATED,
        "expected": ["update_cart"],
        "reports": {"update_cart": report()},
        "observed_before": BEFORE,
        "observed_after": AFTER,
        "never_invoked": NEVER,
    }
    kwargs.update(over)
    return audit_findings(**kwargs)


# --- the finding this whole product exists for -------------------------------


def test_a_tool_that_reports_success_and_changes_nothing_is_named() -> None:
    """The Allbirds-shaped failure, and the reason independent observation is
    not optional.

    The tool's answer is identical to a working one — a call-level evaluator
    sees nothing wrong — and the cart read is what disagrees.
    """
    findings = classify(observed_after=BEFORE)

    assert outcomes(findings)["update_cart"] == "silently_failed"


def test_the_silent_failure_carries_the_claim_beside_the_observation() -> None:
    """A merchant has to be able to see the disagreement, not be told about it.

    The tool's own words are kept verbatim: a summary of a claim is no longer
    the claim, and the report puts the two side by side so a reader judges it
    themselves.
    """
    (finding,) = [f for f in classify(observed_after=BEFORE) if f.tool_name == "update_cart"]

    assert finding.reported == '{"status":"success"}'
    assert finding.observed_before == BEFORE
    assert finding.observed_after == BEFORE


def test_a_tool_that_actually_worked_is_not_accused() -> None:
    """The guard on the test above.

    Without this, a classifier that called everything `silently_failed` would
    pass the headline test and be wrong about every honest storefront.
    """
    assert outcomes(classify())["update_cart"] == "worked"


# --- the distinctions that must not collapse ---------------------------------


def test_a_tool_that_failed_outright_is_not_a_silent_failure() -> None:
    """It made no claim to contradict.

    Reporting it as a silent failure would tell a merchant their tool lies when
    in fact it told them the truth: it did not work.
    """
    findings = classify(
        reports={"update_cart": report(is_error=True, summary="upstream 502")},
        observed_after=BEFORE,
    )

    assert outcomes(findings)["update_cart"] == "failed_outright"


def test_a_tool_the_harness_could_not_check_is_not_reported_as_working() -> None:
    """§16.1's rule, in a merchant-facing shape.

    Without both reads there is nothing to compare, and turning "we could not
    check" into "it is fine" is exactly the reassurance this feature refuses to
    produce.
    """
    assert outcomes(classify(observed_after=None))["update_cart"] == "unobserved"
    assert outcomes(classify(observed_before=None))["update_cart"] == "unobserved"


def test_a_read_tool_that_changed_nothing_is_working_correctly() -> None:
    """The comparison is told what to expect rather than inferring it.

    Guessing "write" from a name would make `update_cart` and
    `update_preferences` mean the same thing on a surface nobody wrote.
    """
    findings = classify(
        expected=["get_cart"],
        reports={"get_cart": report(expects_state_change=False)},
        observed_after=BEFORE,
    )

    assert outcomes(findings)["get_cart"] == "worked"


def test_a_present_tool_nobody_called_is_not_a_verdict() -> None:
    assert outcomes(classify())["search_catalog"] == "not_exercised"


def test_a_tool_the_surface_never_published_is_absent() -> None:
    """Reporting it as failing would accuse a merchant of breaking something
    they never had — the shape the pack's own matching also refuses.

    The pack expects `search_catalog`; this surface does not publish it. A tool
    that is neither published nor expected is not in the report at all, which is
    why the expectation is what puts it there.
    """
    findings = classify(
        enumerated=["get_cart", "update_cart"],
        expected=["update_cart", "search_catalog"],
    )

    assert outcomes(findings)["search_catalog"] == "absent"


# --- FR-162's two tools -------------------------------------------------------


def test_a_never_invoked_tool_present_on_the_surface_is_reported_unexercised() -> None:
    """Enumerated, never called.

    A site owner needs to know an agent can reach checkout from their store; a
    verdict about it would imply somebody tried.
    """
    assert outcomes(classify())["proceed_to_checkout"] == "not_exercised"


def test_a_never_invoked_tool_gets_no_verdict_even_with_a_report_present() -> None:
    """The ordering is the guarantee.

    If a report for a forbidden tool ever reached this function, something
    upstream dispatched it — and the honest response is still to refuse to grade
    it, not to quietly publish the result of a call that should not exist.
    """
    findings = classify(
        expected=["proceed_to_checkout"],
        reports={"proceed_to_checkout": report()},
    )

    assert outcomes(findings)["proceed_to_checkout"] == "not_exercised"
    (finding,) = [f for f in findings if f.tool_name == "proceed_to_checkout"]
    assert finding.reported is None, "a forbidden tool's result is never published"


def test_a_never_invoked_tool_absent_from_the_surface_is_absent() -> None:
    assert outcomes(classify())["manage_orders"] == "absent"


# --- determinism --------------------------------------------------------------


def test_findings_are_ordered_by_tool_name() -> None:
    """Two audits of the same surface must produce the same report."""
    findings = classify()

    assert [f.tool_name for f in findings] == sorted(f.tool_name for f in findings)
    assert classify() == classify()
