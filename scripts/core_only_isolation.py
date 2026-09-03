"""Install ONLY `actionwitness_core` into a clean venv and run its suite there.

Spec v1.9 §26.7 and AC-19; BUILD_ORDER §9 lane 1 ("architecture: forbidden
imports, core-only install"); spec 002 exit gate item 1, "core installs and tests
in isolation with no application or integration package available".

The AST gate in `tests/architecture/test_import_boundaries.py` proves the core
*declares* no forbidden import. That is necessary and not sufficient: it cannot
catch a dependency the core acquires transitively, a module that only imports
something inside a function body, or a `pyproject.toml` that forgets to declare
what the code actually needs. Those failures all look identical in the
development venv, where every package is installed, and appear for the first time
in front of whoever runs `pip install actionwitness-core`.

So this builds the thing being claimed: an empty environment, the core wheel,
nothing else, and the core's own tests run against it.

`pytest` and `pytest-asyncio` are installed alongside it. They are test tooling
rather than package dependencies - the claim under test is that no application,
integration, demo, or vendor package is present, and `pytest-asyncio` is required
because the workspace's pytest configuration declares `asyncio_mode` and
`--strict-config` would otherwise reject the unknown key.

Run directly, or through `tests/architecture/test_core_only_install.py`:

    uv run python scripts/core_only_isolation.py
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import REPO_ROOT, IsolationJob, import_roots, run_isolated

__all__ = [
    "EXCLUDED_LANES",
    "NON_CORE_ROOTS",
    "core_only_test_files",
    "job",
    "run_isolation_check",
]

CORE_PACKAGE: Final = REPO_ROOT / "packages" / "actionwitness_core"
TESTS_ROOT: Final = REPO_ROOT / "tests"

#: A test file importing any of these needs a package the isolated environment
#: deliberately does not have, so it is not part of the core's own suite.
NON_CORE_ROOTS: Final[frozenset[str]] = frozenset(
    {
        "actionwitness_service",
        "integrations",
        "buggy_store",
        "shopify",
        "shopify_bridge",
        "google_evals",
        "fastapi",
        "starlette",
        "uvicorn",
        "httpx",
        "aiosqlite",
    }
)

#: Lanes that verify the *repository* rather than the core: forbidden-import
#: scans, documentation citations, the frontend command surface, and the
#: lane-coverage gate that re-collects the whole workspace suite. None of them is
#: a test of the installed library, and the workspace they inspect is exactly
#: what the isolated environment does not have.
EXCLUDED_LANES: Final[frozenset[str]] = frozenset({"architecture"})

#: Proof that the environment really is missing what it claims to be missing. A
#: green suite means nothing if the packages were installed after all.
MUST_NOT_IMPORT: Final = ("actionwitness_service", "buggy_store", "fastapi", "httpx", "aiosqlite")


class _ModuleLoadImports(ast.NodeVisitor):
    """Collect imports executed while Python initializes a support module."""

    def __init__(self) -> None:
        self.roots: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        self.roots.update(alias.name.split(".")[0] for alias in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level == 0 and node.module:
            self.roots.add(node.module.split(".")[0])

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Function bodies, including optional fixtures, are not run at import.
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None


def _module_load_import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    visitor = _ModuleLoadImports()
    visitor.visit(tree)
    return visitor.roots


def core_only_test_files() -> list[Path]:
    """Every test file that can run with only the core installed.

    Derived by reading the imports rather than by keeping a hand-written list,
    so a new core test joins the isolated suite automatically and a test that
    grows a service import leaves it automatically. A list maintained by hand
    would drift the moment someone forgot it existed.
    """
    return sorted(
        path
        for path in TESTS_ROOT.rglob("test_*.py")
        if "__pycache__" not in path.parts
        and not (set(path.relative_to(TESTS_ROOT).parts) & EXCLUDED_LANES)
        and not (_pytest_import_roots(path) & NON_CORE_ROOTS)
    )


def _pytest_import_roots(path: Path) -> set[str]:
    """Imports pytest will load for ``path``, including ancestor conftests.

    Selecting only from the test module is insufficient: pytest imports every
    ``conftest.py`` between that module and ``tests/`` before collection. A
    core-looking test inside an integration lane must therefore leave the
    isolated suite when its lane fixtures require service dependencies.
    """
    roots = import_roots(path)
    directory = path.parent
    while directory.is_relative_to(TESTS_ROOT):
        conftest = directory / "conftest.py"
        if conftest.is_file():
            roots.update(_module_load_import_roots(conftest))
        if directory == TESTS_ROOT:
            break
        directory = directory.parent
    return roots


def job() -> IsolationJob:
    """The core-only job, with its test selection resolved now.

    Built per call rather than at import time so a test added since this module
    was first imported is still selected - the architecture lane imports this
    once and runs it later.
    """
    return IsolationJob(
        name="core-only",
        package=CORE_PACKAGE,
        requirements=("pytest>=8.3", "pytest-asyncio>=0.24"),
        must_import=(
            "actionwitness_core",
            "actionwitness_core.ports",
            "actionwitness_core.engine.assertions",
        ),
        must_not_import=MUST_NOT_IMPORT,
        test_files=core_only_test_files(),
    )


def run_isolation_check(verbose: bool = False) -> tuple[bool, str]:
    """Build the clean environment, prove what is absent, and run the suite."""
    return run_isolated(job(), verbose=verbose)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="print every step's output")
    parser.add_argument("--list", action="store_true", help="list the core-only test files")
    arguments = parser.parse_args()

    if arguments.list:
        for path in core_only_test_files():
            print(path.relative_to(REPO_ROOT).as_posix())
        return 0

    ok, output = run_isolation_check(verbose=arguments.verbose)
    print(output)
    if not ok:
        print("core-only isolation FAILED", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
