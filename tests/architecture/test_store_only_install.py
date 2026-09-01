"""Standalone Buggy Store install/run gate (spec v1.9 §26.7, AC-19; 003-T9).

BUILD_ORDER §9 lane 1 asks this lane for "standalone target install/run", and
§7/M2's first exit-gate item is "Buggy Store runs and tests with all assurance
packages absent". This is the mirror of the core-only job: that one proves the
assurance core needs no target, this one proves the target needs no assurance
stack.

Neither can be demonstrated in the development venv, where every workspace
member is installed and a forgotten dependency is indistinguishable from a
declared one. The failure this catches appears for the first time in front of
whoever runs `pip install buggy-store` — or, in this project's case, in front of
whoever tries to show that the demo target really is separable from the harness.

**Run is checked separately from import.** A distribution can import cleanly and
still fail the first time it serves a request. So the job performs an actual
storefront journey inside the new environment and checks that the console script
the packaging promises was really generated.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "store_only_isolation.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from store_only_isolation import (  # noqa: E402 - path is prepared immediately above
    ASSURANCE_ROOTS,
    job,
    run_isolation_check,
    store_only_test_files,
)


@pytest.mark.architecture
def test_the_isolation_script_exists_and_uv_is_available() -> None:
    assert SCRIPT.is_file()
    assert shutil.which("uv") is not None, (
        "the standalone-store gate needs `uv` on PATH; it is the project's declared package manager"
    )


@pytest.mark.architecture
def test_the_store_only_selection_is_not_empty_and_excludes_harness_tests() -> None:
    """A selection that quietly became empty would make the gate pass vacuously."""
    selected = {path.relative_to(REPO_ROOT).as_posix() for path in store_only_test_files()}
    assert len(selected) >= 5, f"only {len(selected)} store-only test files were selected"

    # The store's own behaviour, which must run with the harness absent.
    assert "tests/unit/test_store_catalog_and_state.py" in selected
    assert "tests/integration/test_store_api.py" in selected
    assert "tests/integration/test_store_failure_injection.py" in selected

    # These need the assurance stack, so they cannot run where it is absent.
    assert "tests/adapters/test_buggy_store_adapter.py" not in selected
    assert "tests/integration/test_buggy_store_end_to_end.py" not in selected
    assert "tests/contracts/test_buggy_store_templates.py" not in selected


@pytest.mark.architecture
def test_the_selection_rule_is_derived_rather_than_hand_maintained() -> None:
    """A hand-written list drifts; this one is read from each file's imports."""
    for path in store_only_test_files():
        source = path.read_text(encoding="utf-8")
        for root in ASSURANCE_ROOTS:
            assert f"import {root}" not in source, f"{path.name} imports {root}"


@pytest.mark.architecture
def test_the_job_proves_the_assurance_stack_is_absent() -> None:
    """A green suite means nothing if the embargoed packages were installed too."""
    assert set(job().must_not_import) == ASSURANCE_ROOTS


@pytest.mark.architecture
def test_the_job_proves_the_store_runs_and_not_merely_imports() -> None:
    """§26.7 and the M2 exit gate say "runs and tests", which are two claims."""
    configured = job()
    assert configured.run_probe is not None
    assert "/healthz" in configured.run_probe
    assert "store/discount" in configured.run_probe
    assert configured.console_scripts == ("buggy-store",)


@pytest.mark.architecture
def test_the_store_installs_runs_and_tests_in_a_clean_environment() -> None:
    """M2 exit gate, item 1.

    Builds a fresh venv, installs only `buggy-store`, proves the assurance
    packages are genuinely absent, performs a real storefront journey, and runs
    the store's suite there.
    """
    ok, output = run_isolation_check(verbose=True)
    assert ok, f"standalone store isolation failed:\n{output}"


@pytest.mark.architecture
def test_the_isolation_script_reports_through_its_exit_code() -> None:
    """A gate whose failure exits 0 is not a gate. `--list` is the cheap probe."""
    listed = subprocess.run(
        [sys.executable, str(SCRIPT), "--list"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert listed.returncode == 0
    assert listed.stdout.strip().splitlines()


@pytest.mark.architecture
def test_the_store_declares_its_own_async_driver() -> None:
    """BUILD_ORDER §7/M2: "add its own async SQLite dependency".

    Inheriting it from the workspace would pass every test here and fail the
    moment someone installed the distribution on its own.
    """
    import tomllib

    manifest = tomllib.loads(
        (REPO_ROOT / "examples" / "buggy_store" / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = " ".join(manifest["project"]["dependencies"])
    assert "aiosqlite" in dependencies
    for forbidden in ("actionwitness", "integrations"):
        assert forbidden not in dependencies, f"the store depends on {forbidden}"
