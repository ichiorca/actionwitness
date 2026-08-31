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
