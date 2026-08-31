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
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_PACKAGE = REPO_ROOT / "packages" / "actionwitness_core"
TESTS_ROOT = REPO_ROOT / "tests"

#: A test file importing any of these needs a package the isolated environment
#: deliberately does not have, so it is not part of the core's own suite.
NON_CORE_ROOTS = frozenset(
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
EXCLUDED_LANES = frozenset({"architecture"})

#: Proof that the environment really is missing what it claims to be missing. A
#: green suite means nothing if the packages were installed after all.
MUST_NOT_IMPORT = ("actionwitness_service", "buggy_store", "fastapi", "httpx", "aiosqlite")


def _import_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


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
        and not (_import_roots(path) & NON_CORE_ROOTS)
    )


def _run(command: Iterable[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=False, **kwargs)


def run_isolation_check(verbose: bool = False) -> tuple[bool, str]:
    """Build the clean environment, prove what is absent, and run the suite."""
    files = core_only_test_files()
    if not files:
        return False, "no core-only test files were found; the selection is broken"

    log: list[str] = []
    with tempfile.TemporaryDirectory(prefix="actionwitness-core-only-") as workspace:
        venv = Path(workspace) / "venv"
        python = (
            venv
            / ("Scripts" if sys.platform == "win32" else "bin")
            / ("python.exe" if sys.platform == "win32" else "python")
        )

        created = _run(["uv", "venv", str(venv)])
        log.append(f"$ uv venv\n{created.stdout}{created.stderr}")
        if created.returncode != 0:
            return False, "\n".join(log)

        installed = _run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                str(CORE_PACKAGE),
                "pytest>=8.3",
                "pytest-asyncio>=0.24",
            ]
        )
        log.append(f"$ uv pip install\n{installed.stdout}{installed.stderr}")
        if installed.returncode != 0:
            return False, "\n".join(log)

        # The core must be importable...
        probe = _run(
            [
                str(python),
                "-c",
                "import actionwitness_core, actionwitness_core.ports, "
                "actionwitness_core.engine.assertions; print('core ok')",
            ]
        )
        log.append(f"$ import core\n{probe.stdout}{probe.stderr}")
        if probe.returncode != 0:
            return False, "\n".join(log)

        # ...and everything else must not be.
        script = (
            "import importlib.util, json;"
            f"names = {list(MUST_NOT_IMPORT)!r};"
            "print(json.dumps([n for n in names if importlib.util.find_spec(n) is not None]))"
        )
        absent = _run([str(python), "-c", script])
        log.append(f"$ absence probe\n{absent.stdout}{absent.stderr}")
        if absent.returncode != 0:
            return False, "\n".join(log)
        present = json.loads(absent.stdout.strip() or "[]")
        if present:
            log.append(f"packages that should be absent are installed: {present}")
            return False, "\n".join(log)

        suite = _run(
            [str(python), "-m", "pytest", "-q", *[str(path) for path in files]],
            cwd=str(REPO_ROOT),
        )
        log.append(f"$ pytest ({len(files)} files)\n{suite.stdout}{suite.stderr}")
        if suite.returncode != 0:
            return False, "\n".join(log)

    return True, "\n".join(log) if verbose else log[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="print every step's output")
    parser.add_argument(
        "--list", action="store_true", help="list the core-only test files and exit"
    )
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
