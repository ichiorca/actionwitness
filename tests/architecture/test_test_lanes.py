"""Test-lane gates (spec v1.9 §26; 001-preflight-baseline AC-5).

AC-5 asks that the §26 test directories and fixture builders exist. The failure
this guards against is quiet: a lane that is declared but empty looks identical to
a lane that is passing, and nobody notices until the milestone that needed it.

So every registered marker must select at least one test, and every lane directory
must exist. It also enforces the constitution's "no unexplained skips": a suite
that is green because it skipped is not green.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TESTS_ROOT = REPO_ROOT / "tests"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Every lane named in spec §26 / tests/README.md.
EXPECTED_LANES = (
    "adapters",
    "architecture",
    "benchmarks",
    "browser",
    "contracts",
    "evals",
    "guidance",
    "integration",
    "shopify",
    "unit",
)


def _registered_markers() -> set[str]:
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    markers = config["tool"]["pytest"]["ini_options"]["markers"]
    return {entry.split(":", 1)[0].strip() for entry in markers}


def _collect(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.architecture
def test_every_spec_lane_has_a_directory() -> None:
    missing = [lane for lane in EXPECTED_LANES if not (TESTS_ROOT / lane).is_dir()]
    assert missing == [], f"spec §26 lanes without a directory: {missing}"


@pytest.mark.architecture
def test_every_spec_lane_is_a_registered_marker() -> None:
    registered = _registered_markers()
    missing = [lane for lane in EXPECTED_LANES if lane not in registered]
    assert missing == [], f"lanes without a registered pytest marker: {missing}"


@pytest.mark.architecture
def test_no_marker_is_registered_without_a_lane() -> None:
    """A marker nobody can select is dead configuration."""
    unexpected = sorted(_registered_markers() - set(EXPECTED_LANES))
    assert unexpected == [], f"markers registered for no §26 lane: {unexpected}"


@pytest.mark.architecture
@pytest.mark.parametrize("lane", EXPECTED_LANES)
def test_every_lane_selects_at_least_one_test(lane: str) -> None:
    """An empty lane passes vacuously; that is the failure mode worth catching."""
    result = _collect("-m", lane)
    assert result.returncode == 0, f"collection failed for -m {lane}:\n{result.stdout}"
    assert "no tests ran" not in result.stdout.lower()
    assert "/1 tests collected" in result.stdout or "tests collected" in result.stdout, (
        f"-m {lane} collected nothing:\n{result.stdout}"
    )


@pytest.mark.architecture
def test_the_suite_contains_no_skips_or_expected_failures() -> None:
    """The constitution forbids unexplained skips and quarantined failures."""
    self_path = Path(__file__).resolve()
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}"
        for path in TESTS_ROOT.rglob("test_*.py")
        if path.resolve() != self_path  # this module necessarily names the markers
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if ("pytest.mark.skip" in line or "pytest.mark.xfail" in line)
        and not line.lstrip().startswith("#")
    ]
    assert offenders == [], f"skipped or quarantined tests: {offenders}"
