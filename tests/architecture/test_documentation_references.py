"""Documentation-reference gates (spec v1.9 §29.2; 001-preflight-baseline AC-2).

Every in-repo citation must name the current specification version. A stale
`v1.8` reference is not cosmetic: section numbers moved between 1.8 and 1.9, so a
stale citation points a reader at the wrong requirement.

These tests walk the filesystem rather than shelling out to git, so they run in a
clean checkout with no dependencies installed.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Roots that hold tracked, citation-bearing project content.
SCANNED_ROOTS = (
    "apps",
    "examples",
    "integrations",
    "packages",
    "shopify_bridge",
    "tests",
)
SCANNED_ROOT_FILES = ("README.md", "pyproject.toml", "AGENTS.md", "CLAUDE.md")

SCANNED_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".md",
    ".toml",
    ".json",
    ".html",
    ".yaml",
    ".yml",
}

EXCLUDED_DIR_NAMES = {
    ".git",
    ".idea",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}

CURRENT_SPEC_VERSION = "1.9"
SPEC_PATH = "docs/actionwitness-functional-spec.md"

# Matches "v1.8", "spec v1.8", "version 1.8" and friends, but not "1.80" or a
# semantic version such as "0.1.8" that belongs to a package.
STALE_SPEC_VERSION = re.compile(
    r"(?<![\w.])(?:v|version\s+)1\.8(?![\w.])", re.IGNORECASE
)


#: This module necessarily spells the superseded version out, so it excludes itself.
SELF = Path(__file__).resolve()


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for name in SCANNED_ROOT_FILES:
        candidate = REPO_ROOT / name
        if candidate.is_file():
            files.append(candidate)
    for root_name in SCANNED_ROOTS:
        root = REPO_ROOT / root_name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix not in SCANNED_SUFFIXES or not path.is_file():
                continue
            if EXCLUDED_DIR_NAMES & set(path.relative_to(REPO_ROOT).parts):
                continue
            if path.resolve() == SELF:
                continue
            files.append(path)
    return sorted(files)


@pytest.mark.architecture
def test_no_source_file_cites_a_superseded_spec_version() -> None:
    scanned = _scanned_files()
    assert scanned, "expected the documentation-reference scan to find files"

    stale = [
        f"{path.relative_to(REPO_ROOT).as_posix()}:{lineno}: {line.strip()}"
        for path in scanned
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if STALE_SPEC_VERSION.search(line)
    ]
    assert stale == [], (
        f"citations must name spec v{CURRENT_SPEC_VERSION}; found superseded references:\n"
        + "\n".join(stale)
    )


@pytest.mark.architecture
def test_readme_names_the_specification_at_its_real_path() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert SPEC_PATH in readme, f"README must name the specification at {SPEC_PATH}"
    assert f"version {CURRENT_SPEC_VERSION}" in readme, (
        f"README must state the normative spec version ({CURRENT_SPEC_VERSION})"
    )


@pytest.mark.architecture
def test_readme_documents_the_full_command_surface() -> None:
    """BUILD_ORDER §7/M0 requires format, type-check, unit, frontend-test and build."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    required = (
        "uv run pytest -q",
        "uv run pytest tests/architecture -q",
        "uv run ruff format --check .",
        "uv run ruff check .",
        "npm ci",
        "npm run typecheck",
        "npm test",
        "npm run build",
    )
    missing = [command for command in required if command not in readme]
    assert missing == [], f"README omits required commands: {missing}"
