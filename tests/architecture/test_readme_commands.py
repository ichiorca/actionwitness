"""Every command the README documents exists (spec §29.2; 009-T8/T10).

§29.2 makes the README a deliverable, and its command tables are the part a judge
actually executes. A documented command that no longer exists is not a
documentation bug — it is the first thing a new reader hits, and it fails in the
one minute they were promised.

These are static checks against the manifests, so they run in the Python lane with
no Node toolchain present. That the commands *pass* is the CI workflow's job
(`readme-clean-checkout`); that they *exist* is this file's.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"

HARNESS_FRONTEND = REPO_ROOT / "apps" / "actionwitness_service" / "frontend"
STORE_FRONTEND = REPO_ROOT / "examples" / "buggy_store" / "frontend"

#: `npm ci`, `npm test`, and `npm install` are npm's own; only `npm run <name>`
#: names a script a package.json has to declare.
NPM_RUN = re.compile(r"`npm run ([a-z:]+)`")
#: `uv run python scripts/<name>.py`
UV_SCRIPT = re.compile(r"`uv run python (scripts/[\w./-]+\.py)`")
#: `uv run pytest <path>`, ignoring bare `uv run pytest -q`.
PYTEST_PATH = re.compile(r"`uv run pytest (tests/[\w./-]+)")


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def _scripts(frontend: Path) -> set[str]:
    manifest = json.loads((frontend / "package.json").read_text(encoding="utf-8"))
    return set(manifest.get("scripts", {}))


@pytest.mark.architecture
def test_the_readme_exists_and_documents_commands() -> None:
    """The guard on every other test here: an empty scan proves nothing."""
    assert README.is_file()
    assert NPM_RUN.search(_readme()), "the README documents no npm scripts"
    assert UV_SCRIPT.search(_readme()), "the README documents no uv scripts"


@pytest.mark.architecture
def test_every_documented_npm_script_is_declared_by_a_frontend() -> None:
    """Both frontends are scanned together.

    The README lists them in separate sections, but a script named in either
    section must exist somewhere — and the sections have been copied from one
    another before, which is how `lint` came to be documented for a package that
    did not declare it.
    """
    declared = _scripts(HARNESS_FRONTEND) | _scripts(STORE_FRONTEND)
    documented = set(NPM_RUN.findall(_readme()))
    missing = sorted(documented - declared)
    assert missing == [], f"the README documents npm scripts nobody declares: {missing}"


@pytest.mark.architecture
@pytest.mark.parametrize("frontend", [HARNESS_FRONTEND, STORE_FRONTEND], ids=["harness", "store"])
def test_both_frontends_declare_the_same_gate_commands(frontend: Path) -> None:
    """§29.1 builds them independently, so each carries the full set of gates.

    Until 009 the storefront declared no `lint`, which the 006 command-surface
    gate recorded as a named gap rather than hiding. This is where it closes.
    """
    declared = _scripts(frontend)
    missing = sorted({"typecheck", "lint", "test", "build"} - declared)
    assert missing == [], f"{frontend.name} omits gate scripts: {missing}"


@pytest.mark.architecture
def test_every_documented_python_script_exists() -> None:
    documented = set(UV_SCRIPT.findall(_readme()))
    missing = sorted(name for name in documented if not (REPO_ROOT / name).is_file())
    assert missing == [], f"the README documents scripts that do not exist: {missing}"


@pytest.mark.architecture
def test_every_documented_pytest_path_exists() -> None:
    documented = set(PYTEST_PATH.findall(_readme()))
    missing = sorted(path for path in documented if not (REPO_ROOT / path).exists())
    assert missing == [], f"the README documents test paths that do not exist: {missing}"


@pytest.mark.architecture
def test_the_readme_names_no_unregistered_pytest_marker() -> None:
    """The README lists the lane markers; `--strict-markers` rejects a typo'd one.

    A reader following `-m guidance` should not be the one to discover the marker
    was renamed.
    """
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    registered = set(re.findall(r'^\s*"(\w+):', pyproject, re.MULTILINE))

    _, _, tail = _readme().partition("Registered markers:")
    listed, _, _ = tail.partition(".\n")
    documented = set(re.findall(r"`(\w+)`", listed))

    assert documented, "the README no longer lists the pytest markers"
    unknown = sorted(documented - registered)
    assert unknown == [], f"the README lists markers pyproject does not register: {unknown}"
