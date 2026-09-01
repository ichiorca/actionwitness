"""Shared machinery for the clean-environment install jobs.

Spec v1.9 §26.7 and AC-19; BUILD_ORDER §9 lane 1, which asks the architecture
lane for both "core-only install" and "standalone target install/run".

The two jobs prove opposite halves of the same boundary — the core installs with
no application or integration present, and the Buggy Store installs with no
assurance package present — and the procedure is identical either way: build an
empty environment, install one distribution, prove what is *absent* really is,
optionally prove the thing runs, and then run its own tests inside it.

The absence probe is the part worth naming. A green suite inside the new
environment proves nothing on its own: if the packages under embargo happened to
be installed as well, every test would pass and the isolation claim would be
false. So each job states what must not be importable, and the job fails when any
of it is.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["REPO_ROOT", "IsolationJob", "import_roots", "run_isolated"]

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True, slots=True)
class IsolationJob:
    """One clean-environment proof."""

    #: Human-readable name, used in failure output.
    name: str
    #: The distribution directory to install, and nothing else from this repo.
    package: Path
    #: Test tooling. Not package dependencies: the claim under test is that no
    #: application, integration, demo, or vendor package is present, and a test
    #: runner is how the claim gets checked rather than part of it.
    requirements: tuple[str, ...]
    #: Modules that must import inside the new environment.
    must_import: tuple[str, ...]
    #: Modules that must NOT resolve there. Without this a suite could be green
    #: because everything was installed after all.
    must_not_import: tuple[str, ...]
    #: The distribution's own tests, run from the repository root.
    test_files: Sequence[Path]
    #: Optional source proving the distribution *runs*, not merely imports.
    #: BUILD_ORDER §9 asks the target job for "install/run", not "install".
    run_probe: str | None = None
    #: Console scripts the install must have created.
    console_scripts: tuple[str, ...] = field(default_factory=tuple)


def import_roots(path: Path) -> set[str]:
    """Top-level module names a Python file imports, by AST rather than by regex."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _run(command: Iterable[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=False, **kwargs)


def _interpreter(venv: Path) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _script(venv: Path, name: str) -> Path:
    if sys.platform == "win32":
        return venv / "Scripts" / f"{name}.exe"
    return venv / "bin" / name


def run_isolated(job: IsolationJob, verbose: bool = False) -> tuple[bool, str]:
    """Execute one isolation job. Returns (passed, output)."""
    if not job.test_files:
        return False, f"{job.name}: no test files were selected; the selection is broken"

    log: list[str] = []

    def record(step: str, result: subprocess.CompletedProcess[str]) -> bool:
        log.append(f"$ {step}\n{result.stdout}{result.stderr}")
        return result.returncode == 0

    with tempfile.TemporaryDirectory(prefix=f"actionwitness-{job.name}-") as workspace:
        venv = Path(workspace) / "venv"
        python = _interpreter(venv)
        # Probes run from a neutral directory, never the repository root.
        # `python -c` puts the working directory on `sys.path`, and this
        # repository has bare `integrations/` and `packages/` directories at its
        # top level - so a probe run from the root would "find" a namespace
        # package that is a folder on disk rather than anything installed, and
        # report a leak that does not exist. The question being asked is what the
        # *environment* contains.
        neutral = str(Path(workspace))

        if not record("uv venv", _run(["uv", "venv", str(venv)])):
            return False, "\n".join(log)

        install = _run(
            ["uv", "pip", "install", "--python", str(python), str(job.package), *job.requirements]
        )
        if not record("uv pip install", install):
            return False, "\n".join(log)

        importable = _run(
            [str(python), "-c", "; ".join(f"import {name}" for name in job.must_import)],
            cwd=neutral,
        )
        if not record(f"import {', '.join(job.must_import)}", importable):
            return False, "\n".join(log)

        probe_source = (
            "import importlib.util, json;"
            f"names = {list(job.must_not_import)!r};"
            "print(json.dumps([n for n in names if importlib.util.find_spec(n) is not None]))"
        )
        absent = _run([str(python), "-c", probe_source], cwd=neutral)
        if not record("absence probe", absent):
            return False, "\n".join(log)
        present = json.loads(absent.stdout.strip() or "[]")
        if present:
            log.append(f"packages that must be absent are installed: {present}")
            return False, "\n".join(log)

        for name in job.console_scripts:
            path = _script(venv, name)
            log.append(f"$ console script {name}\n{'found' if path.exists() else 'MISSING'}")
            if not path.exists():
                return False, "\n".join(log)

        if job.run_probe is not None and not record(
            "run probe", _run([str(python), "-c", job.run_probe], cwd=neutral)
        ):
            return False, "\n".join(log)

        suite = _run(
            [str(python), "-m", "pytest", "-q", *[str(path) for path in job.test_files]],
            cwd=str(REPO_ROOT),
        )
        if not record(f"pytest ({len(job.test_files)} files)", suite):
            return False, "\n".join(log)

    return True, "\n".join(log) if verbose else log[-1]
