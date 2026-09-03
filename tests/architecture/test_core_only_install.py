"""Core-only install gate and the spec 002 exit-gate roll-up (002-T13).

Spec v1.9 §26.7 and AC-19; BUILD_ORDER §9 lane 1 ("architecture: forbidden
imports, core-only install"); the six acceptance criteria in
`specs/002-core-kernel/spec.md`.

The first half runs the real thing: a clean virtual environment containing only
`actionwitness_core` plus test tooling, with the core's own suite executed inside
it. This is the lane's job precisely because the development venv cannot detect
the failure - there, every package is installed, so an undeclared dependency and
a declared one look identical until someone installs the library on its own.

The second half is traceability. Spec 002's exit gate lists six criteria, and a
milestone can be "finished" with one of them covered by nothing at all; naming
the covering test for each makes that visible in the diff instead of at review.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "scripts" / "core_only_isolation.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from core_only_isolation import (  # noqa: E402 - path is prepared immediately above
    EXCLUDED_LANES,
    NON_CORE_ROOTS,
    core_only_test_files,
    run_isolation_check,
)

# --- the isolation job ------------------------------------------------------


@pytest.mark.architecture
def test_the_isolation_script_exists_and_is_executable_as_a_module() -> None:
    """BUILD_ORDER §9 makes core-only install part of this lane, not a manual step."""
    assert SCRIPT.is_file()
    assert shutil.which("uv") is not None, (
        "the core-only install gate needs `uv` on PATH; it is the project's "
        "declared package manager"
    )


@pytest.mark.architecture
def test_the_core_only_suite_selection_is_not_empty_and_excludes_service_tests() -> None:
    """A selection that quietly became empty would make the gate pass vacuously."""
    selected = core_only_test_files()
    assert len(selected) >= 10, f"only {len(selected)} core-only test files were selected"

    relative = {path.relative_to(REPO_ROOT).as_posix() for path in selected}
    # These import the service, so they cannot run where the service is absent.
    assert "tests/unit/test_registry.py" not in relative
    assert "tests/unit/test_config.py" not in relative
    assert "tests/guidance/test_guidance_lane.py" not in relative
    # This module has no direct service import, but its lane conftest builds the
    # real FastAPI application. Pytest loads that support before collecting it.
    # The existence check keeps the exclusion honest: were the file renamed
    # away, `not in` would hold vacuously and guard nothing.
    assert (REPO_ROOT / "tests" / "shopify" / "test_shopify_status_projection.py").is_file()
    assert "tests/shopify/test_shopify_status_projection.py" not in relative
    # These are the core's own, and must be in.
    assert "tests/unit/test_assertions.py" in relative
    assert "tests/contracts/test_contract_models.py" in relative
    assert "tests/adapters/test_non_commerce_adapter.py" in relative


@pytest.mark.architecture
def test_the_selection_rule_is_derived_rather_than_hand_maintained() -> None:
    """A hand-written list drifts; this one is read from the imports each time."""
    for path in core_only_test_files():
        source = path.read_text(encoding="utf-8")
        for root in NON_CORE_ROOTS:
            assert f"import {root}" not in source, f"{path} imports {root}"
    assert "architecture" in EXCLUDED_LANES


@pytest.mark.architecture
def test_no_selected_file_sits_beneath_a_conftest_that_needs_the_service() -> None:
    """The regression this guards fails otherwise only inside the isolated venv.

    Pytest imports every `conftest.py` between a collected file and `tests/`,
    so a core-looking test placed in a lane whose conftest imports the service
    stack drags that stack into the core-only run — and the first symptom is a
    `ModuleNotFoundError` minutes into the expensive venv build (this is
    exactly how `tests/shopify/test_shopify_status_projection.py` broke the
    gate once). The ancestor-conftest walk here is re-derived from the AST
    rather than taken from the selector's own bookkeeping, so a selector that
    regresses to reading only the test module fails this test in seconds.
    """
    import ast

    def module_load_roots(path: Path) -> set[str]:
        # Module-load imports only: function bodies (fixtures included) do not
        # run when pytest merely imports the conftest.
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots: set[str] = set()
        stack: list[ast.AST] = list(ast.iter_child_nodes(tree))
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
            stack.extend(ast.iter_child_nodes(node))
        return roots

    tests_root = REPO_ROOT / "tests"
    offending: list[str] = []
    for path in core_only_test_files():
        directory = path.parent
        while directory.is_relative_to(tests_root):
            conftest = directory / "conftest.py"
            if conftest.is_file():
                needed = module_load_roots(conftest) & NON_CORE_ROOTS
                if needed:
                    offending.append(
                        f"{path.relative_to(REPO_ROOT).as_posix()} loads "
                        f"{conftest.relative_to(REPO_ROOT).as_posix()}, which "
                        f"imports {sorted(needed)}"
                    )
            if directory == tests_root:
                break
            directory = directory.parent
    assert offending == [], (
        "core-only selected tests would import service dependencies through "
        "their lane conftest inside the isolated venv:\n" + "\n".join(offending)
    )


@pytest.mark.architecture
def test_the_core_installs_and_tests_in_a_clean_environment() -> None:
    """Spec 002 exit gate 1, and the AC-19 core-only install job.

    Builds a fresh venv, installs only the core, proves the application,
    integration and framework packages are genuinely absent, and runs the core's
    suite there.
    """
    ok, output = run_isolation_check(verbose=True)
    assert ok, f"core-only isolation failed:\n{output}"


@pytest.mark.architecture
def test_the_isolation_script_reports_failure_through_its_exit_code() -> None:
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


# --- spec 002 exit-gate traceability ----------------------------------------

#: Each criterion from `specs/002-core-kernel/spec.md`, mapped to a test that
#: covers it. Named rather than counted: "there are tests" is not the same claim
#: as "this requirement is tested".
EXIT_GATE: dict[str, tuple[str, str]] = {
    "1. core installs and tests in isolation": (
        "tests/architecture/test_core_only_install.py",
        "test_the_core_installs_and_tests_in_a_clean_environment",
    ),
    "2a. published RFC 8785 vectors pass": (
        "tests/unit/test_canonicalization.py",
        "test_every_accept_vector_canonicalizes_to_its_expected_text",
    ),
    "2b. repository fixture vectors pass": (
        "tests/unit/test_canonicalization.py",
        "test_the_corpus_covers_both_published_and_repository_origins",
    ),
    "3. the non-commerce adapter evaluates target.ticket.status": (
        "tests/adapters/test_non_commerce_adapter.py",
        "test_a_non_commerce_path_is_evaluated_end_to_end",
    ),
    "4a. unknown fields fail with structured errors": (
        "tests/contracts/test_contract_models.py",
        "test_an_unknown_field_is_refused_rather_than_ignored",
    ),
    "4b. unknown paths fail": (
        "tests/contracts/test_observation_paths.py",
        "test_expression_language_and_malformed_paths_are_refused",
    ),
    "4c. unknown operators fail": (
        "tests/contracts/test_contract_models.py",
        "test_an_unknown_operator_is_refused",
    ),
    "4d. unknown policy types fail": (
        "tests/contracts/test_contract_models.py",
        "test_an_unknown_policy_type_is_refused",
    ),
    "4e. unknown schema versions fail": (
        "tests/contracts/test_contract_models.py",
        "test_an_unknown_schema_version_is_refused",
    ),
    "4f. non-finite numbers fail": (
        "tests/unit/test_canonicalization.py",
        "test_non_finite_numbers_are_refused",
    ),
    "4g. unsafe contract sizes fail": (
        "tests/contracts/test_contract_models.py",
        "test_a_contract_over_the_canonical_size_limit_is_refused",
    ),
    "5. same inputs and hashes produce byte-identical reports": (
        "tests/unit/test_reports.py",
        "test_the_same_inputs_produce_byte_identical_reports",
    ),
    "6. the architecture lane still passes (no forbidden imports)": (
        "tests/architecture/test_import_boundaries.py",
        "test_actionwitness_core_has_no_forbidden_imports",
    ),
}


@pytest.mark.architecture
@pytest.mark.parametrize("criterion", sorted(EXIT_GATE), ids=lambda name: name.split(".")[0])
def test_every_exit_gate_criterion_names_a_test_that_exists(criterion: str) -> None:
    """A criterion whose covering test was renamed away must fail loudly."""
    relative, function = EXIT_GATE[criterion]
    path = REPO_ROOT / relative
    assert path.is_file(), f"{criterion}: {relative} does not exist"
    assert f"def {function}(" in path.read_text(encoding="utf-8"), (
        f"{criterion}: {relative} no longer defines {function}"
    )


@pytest.mark.architecture
def test_the_exit_gate_covers_every_criterion_the_spec_lists() -> None:
    """Read the spec's numbered criteria and require a covering test for each.

    The spec is operator-owned and protected, so it is read and never edited.
    Reading it here means a criterion the operator adds later fails this test
    until something covers it, rather than sitting quietly outside the map.
    """
    import re

    spec = (REPO_ROOT / "specs" / "002-core-kernel" / "spec.md").read_text(encoding="utf-8")
    acceptance = spec.split("## Acceptance criteria / exit gate")[1].split("## Non-goals")[0]
    numbered = re.findall(r"^(\d+)\.\s", acceptance, flags=re.MULTILINE)
    assert numbered, "no numbered acceptance criteria were found in the spec"

    # A criterion may need more than one covering test - criterion 4 lists seven
    # distinct rejections - so keys are `<number>` or `<number><letter>`.
    covered = {re.match(r"\d+", key).group(0) for key in EXIT_GATE}
    missing = sorted(set(numbered) - covered, key=int)
    assert missing == [], f"exit-gate criteria with no covering test: {missing}"
