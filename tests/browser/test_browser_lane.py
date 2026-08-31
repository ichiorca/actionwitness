"""Browser lane (spec §26.4, §7.5).

This lane is deliberately **not** automated browser testing. Spec §26.4 makes
WebMCP browser checks a manual checklist against a pinned build, and §7.5 makes
provisioning a flagged browser a hard cut — its absence must never fail the
release-gating suite.

So the tests here guard that boundary rather than driving a browser: they assert
the lane stays free of automation dependencies, and (once T10 lands the checklist)
that the manual checklist an operator follows actually exists and is complete.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BROWSER_LANE = REPO_ROOT / "tests" / "browser"

#: Drivers that would turn this lane into a CI dependency on a flagged browser.
AUTOMATION_IMPORTS = ("playwright", "selenium", "puppeteer", "pyppeteer")


@pytest.mark.browser
def test_the_lane_declares_no_browser_automation_dependency() -> None:
    """§7.5: a flagged-browser build is never a release-gating CI dependency."""
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()} imports {driver}"
        for path in BROWSER_LANE.rglob("*.py")
        for driver in AUTOMATION_IMPORTS
        if f"import {driver}" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], (
        "the browser lane must stay manual; automated browser tests are Tier 3 and "
        f"conditional: {offenders}"
    )


@pytest.mark.browser
def test_the_lane_is_runnable_without_a_browser_present() -> None:
    """Reaching this assertion is the point: the lane collects and passes headless."""
    assert BROWSER_LANE.is_dir()


SPIKE_CHECKLIST = BROWSER_LANE / "webmcp-spike-checklist.md"

#: Every §25.1 adapter behavior the ADR-0002 run has to cover. A checklist that
#: silently loses a row would let the pin be made on incomplete evidence.
REQUIRED_CHECKS = (
    "unsupported environment",
    "unmount",
    "strictmode",
    "enabled",
    "gettools",
    "toolchange",
    "normalized success",
    "normalized thrown error",
    "registration failure",
)

#: The pinning facts spec §29.3 requires the README to carry afterwards.
REQUIRED_PINNING_FACTS = ("chrome", "webmcp-types", "origin-trial", "descriptions")


@pytest.mark.browser
def test_the_operator_spike_checklist_exists() -> None:
    """ADR-0002 cannot be closed from a terminal; the human needs instructions."""
    assert SPIKE_CHECKLIST.is_file(), (
        "expected the ADR-0002 operator checklist at "
        f"{SPIKE_CHECKLIST.relative_to(REPO_ROOT).as_posix()}"
    )


@pytest.mark.browser
def test_the_checklist_covers_every_required_adapter_behavior() -> None:
    text = SPIKE_CHECKLIST.read_text(encoding="utf-8").lower()
    missing = [check for check in REQUIRED_CHECKS if check not in text]
    assert missing == [], f"spike checklist does not cover: {missing}"


@pytest.mark.browser
def test_the_checklist_names_both_candidates_and_the_native_control() -> None:
    """A comparison with no control is not a comparison."""
    text = SPIKE_CHECKLIST.read_text(encoding="utf-8")
    for candidate in ("use-webmcp-tool", "usewebmcp", "native"):
        assert candidate in text, f"checklist never mentions {candidate}"


@pytest.mark.browser
def test_the_checklist_asks_for_the_execution_signal_result() -> None:
    """FR-037: a path that drops `signal` cannot carry proceed_to_checkout."""
    text = SPIKE_CHECKLIST.read_text(encoding="utf-8")
    assert "FR-037" in text
    assert "proceed_to_checkout" in text


@pytest.mark.browser
def test_the_checklist_collects_the_required_pinning_facts() -> None:
    text = SPIKE_CHECKLIST.read_text(encoding="utf-8").lower()
    missing = [fact for fact in REQUIRED_PINNING_FACTS if fact not in text]
    assert missing == [], f"checklist does not collect §29.3 pinning facts: {missing}"


@pytest.mark.browser
def test_the_checklist_warns_against_committing_a_lockfile_during_the_spike() -> None:
    """The lockfile is committed after the pin, so it records the tested tree."""
    text = SPIKE_CHECKLIST.read_text(encoding="utf-8").lower()
    assert "package-lock.json" in text
    assert "npm ci" in text, "the checklist must say why npm install is used instead"
