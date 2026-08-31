"""Evals lane (spec v1.9 §26.7; 001-preflight-baseline AC-5).

The eval mechanism itself is M6 work (spec §24). What is assertable now is the
scaffold's honesty: the core evals package must import in isolation, and it must
still be *only* a scaffold. The second assertion is a deliberate tripwire — the
moment M6 lands behavior here, it fails, forcing this placeholder to be replaced
by real §24 coverage in the same change rather than after it.
"""

import inspect

import actionwitness_core.evals as core_evals
import pytest


@pytest.mark.evals
def test_core_evals_package_imports_cleanly() -> None:
    """§18 mandates the package; an import error would poison every M6 test."""
    assert core_evals.__doc__ is not None
    assert "§24" in core_evals.__doc__


@pytest.mark.evals
def test_scaffold_has_not_grown_untested_behavior() -> None:
    """Fails the moment real eval behavior lands, so it cannot land untested.

    Replace this placeholder with §24 coverage (case generation, fixture
    replay, classification fidelity) in the same change that makes it fail.
    """
    public = [
        name
        for name, value in vars(core_evals).items()
        if not name.startswith("_") and (inspect.isfunction(value) or inspect.isclass(value))
    ]
    assert public == [], (
        "actionwitness_core.evals grew public behavior; replace "
        "tests/evals/test_evals_lane.py with real spec §24 coverage: "
        f"{public}"
    )
