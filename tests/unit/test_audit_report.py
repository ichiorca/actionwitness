"""015-T5 — the site-owner report (§5, FR-163).

FR-163 asks for a report "addressed to a site owner rather than a harness
engineer". That is testable, and these tests treat it as a requirement rather
than as a style note:

* the summary contains none of the harness's vocabulary, checked word by word,
  because prose drifts back toward the vocabulary of whoever writes it;
* the first sentence states a consequence, since it is the only sentence some
  readers will read;
* the limits are in the report rather than assumed, because a merchant who
  believes a clean result is a guarantee will stop looking.

The urgency ordering is tested too. A merchant reading top-down should meet the
tool that is quietly costing them orders before the ones that are merely absent.
"""

from __future__ import annotations

import json

import pytest
from actionwitness_service.application.audit_evidence import AuditFinding, ToolOutcome
from actionwitness_service.application.audit_report import (
    LIMITS,
    SUMMARY_FORBIDDEN_WORDS,
    compose_audit_report,
)

pytestmark = pytest.mark.unit

ORIGIN = "https://shop.example"
BEFORE = {"cart": {"item_count": 0}}
AFTER = {"cart": {"item_count": 0}}


def finding(name: str, outcome: ToolOutcome, **over: object) -> AuditFinding:
    return AuditFinding(tool_name=name, outcome=outcome, **over)  # type: ignore[arg-type]


def report(*findings: AuditFinding) -> dict:
    return compose_audit_report(
        authorized_origin=ORIGIN,
        pack_id="shopify_cart",
        pack_title="Shopify storefront — cart pass",
        findings=findings,
    )


SILENT = finding(
    "update_cart",
    ToolOutcome.SILENTLY_FAILED,
    reported='{"status":"success"}',
    observed_before=BEFORE,
    observed_after=AFTER,
)


# --- written for the shop owner (criterion 4) --------------------------------


def test_the_summary_uses_no_harness_vocabulary() -> None:
    """A merchant has a storefront and customers, not contracts and policies.

    Checked word by word rather than reviewed by eye, because this is the one
    section that must not drift back toward the vocabulary of whoever edits it.
    """
    summary = json.dumps(report(SILENT)["summary"]).lower()

    present = sorted(word for word in SUMMARY_FORBIDDEN_WORDS if word in summary)
    assert present == [], f"the summary speaks harness: {present}"


def test_the_headline_states_a_consequence_rather_than_a_mechanism() -> None:
    """It is the only sentence some readers will read."""
    headline = report(SILENT)["summary"]["headline"]

    assert "update_cart" in headline
    assert "said they worked when they had not" in headline


def test_the_consequence_is_concrete_enough_to_act_on() -> None:
    """ "A state mismatch was detected" is not a consequence.

    A merchant deciding whether to stop what they are doing needs to know what
    happens to a customer.
    """
    meaning = report(SILENT)["summary"]["what_this_means"]

    assert "customer" in meaning
    assert "checkout" in meaning


def test_the_advice_puts_the_silent_failure_first() -> None:
    """Fix-this-first has to be findable without reading everything."""
    entries = report(SILENT)["summary"]["tools"]

    assert entries[0]["tool"] == "update_cart"
    assert entries[0]["what_to_do"].startswith("Fix this first")


# --- ordering by urgency ------------------------------------------------------


def test_tools_are_ordered_by_what_a_merchant_should_worry_about() -> None:
    """Absent and working tools are the least urgent thing on the page."""
    entries = report(
        finding("browse_store", ToolOutcome.ABSENT),
        finding("get_cart", ToolOutcome.WORKED),
        finding("proceed_to_checkout", ToolOutcome.NOT_EXERCISED),
        finding("search_catalog", ToolOutcome.UNOBSERVED),
        finding("cancel_cart", ToolOutcome.FAILED_OUTRIGHT),
        SILENT,
    )["summary"]["tools"]

    assert [entry["tool"] for entry in entries] == [
        "update_cart",
        "cancel_cart",
        "search_catalog",
        "proceed_to_checkout",
        "get_cart",
        "browse_store",
    ]


# --- honesty about what it did not do ----------------------------------------


def test_the_report_states_its_own_limits() -> None:
    """FR-163: "a passing audit is evidence about the tested journey rather than
    a guarantee about the surface"."""
    summary = report(finding("get_cart", ToolOutcome.WORKED))["summary"]

    assert summary["limits"] == list(LIMITS)
    assert any("not a guarantee" in line for line in summary["limits"])


def test_a_clean_result_does_not_claim_more_than_it_checked() -> None:
    """The sentence a merchant would over-read if it were any warmer."""
    headline = report(finding("get_cart", ToolOutcome.WORKED))["summary"]["headline"]

    assert headline == "Every tool that was tried did what it said it did."


def test_an_unchecked_tool_stops_the_report_calling_itself_clean() -> None:
    """§16.1, in a merchant-facing shape: "could not check" is not "fine"."""
    summary = report(finding("update_cart", ToolOutcome.UNOBSERVED))["summary"]

    assert "not a clean bill of health" in summary["headline"]
    assert "update_cart" in summary["not_checked"]


def test_untried_tools_are_listed_as_untried() -> None:
    """A merchant needs to know checkout was reachable and deliberately left."""
    summary = report(finding("proceed_to_checkout", ToolOutcome.NOT_EXERCISED))["summary"]

    assert summary["not_checked"] == ["proceed_to_checkout"]
    assert "could have created a real order" in summary["tools"][0]["what_to_do"]


# --- the engineer's half -------------------------------------------------------


def test_the_evidence_section_keeps_the_claim_and_the_observation() -> None:
    """The summary is for the owner; this is for whoever they forward it to.

    Same data rather than a second derivation of it — a report whose two halves
    could disagree would be worse than one half.
    """
    document = report(SILENT)
    (evidence,) = document["evidence"]

    assert evidence["reported"] == '{"status":"success"}'
    assert evidence["observed_before"] == BEFORE
    assert evidence["observed_after"] == AFTER
    assert [item["tool"] for item in document["summary"]["tools"]] == [
        entry["tool_name"] for entry in document["evidence"]
    ]


def test_the_report_names_the_journey_it_ran() -> None:
    """FR-161: "the report shall name which pack was applied".

    A reader who cannot tell which journey was tried cannot tell what the result
    covers.
    """
    document = report(SILENT)

    assert document["checked_using"] == "Shopify storefront — cart pass"
    assert document["checked_using_id"] == "shopify_cart"
    assert document["audited_site"] == ORIGIN
