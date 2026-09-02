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
STALE_SPEC_VERSION = re.compile(r"(?<![\w.])(?:v|version\s+)1\.8(?![\w.])", re.IGNORECASE)


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
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
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


@pytest.mark.architecture
def test_the_unfiled_upstream_issue_is_drafted_and_pointed_at() -> None:
    """§25.3's first-consumer contribution: drafted here, filed by the operator.

    "Not yet filed" was a note pointing at nothing for as long as it stood, which
    is the least useful shape an open item can take — a reader could not tell
    whether the work was pending, lost, or decided against.

    So the draft is in the repository and the two places that used to say
    "not filed" now say so, and say where the text is kept.

    The draft itself left the repository with the decision records
    (`.gitignore`), so what this can still hold is the part a reader depends on:
    that the item is declared open rather than silently dropped, and that the
    version it was verified against survives. Asserting the file existed would
    have been asserting the operator's working tree, which is not something a
    clean checkout — or CI — can see.

    Filing itself stays the operator's call: it publishes text on somebody
    else's project, and neither this test nor any other should make that look
    automatic.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "drafted, not filed" in readme, (
        "the README no longer declares the upstream issue as an open item"
    )
    # The pin the draft was written against. Without it the note dates itself to
    # nothing, and a reader cannot tell whether it still describes the shipped
    # evaluator.
    assert "fe33c1b" in readme, (
        "the README no longer names the evaluator version the draft was verified against"
    )


#: Directories that left the repository at the operator's direction
#: (`.gitignore`). They still exist in an operator's working tree, which is
#: exactly why a citation of one reads as fine locally and resolves to nothing
#: in a clone.
ABSENT_DOC_DIRS = ("docs/adr/", "docs/upstream/")

#: Files allowed to name those paths, each for a stated reason.
#:
#: `webmcp-spike-checklist.md` is a procedure for the operator to run against
#: their own tree, and it tells them which file to record the results in — the
#: one reader it addresses is the one reader who has it.
#:
#: `rfc8785_vectors.json` carries the path as provenance metadata on a fixture,
#: recording where the vectors came from. Rewriting it would erase the
#: attribution to make a link check pass.
#:
#: `specs/**` is out of scope by construction: `_scanned_files` does not walk it,
#: and it should not — those are records of work as it happened, and editing a
#: completed task list to say something it did not say at the time would falsify
#: the history the runway exists to keep.
ABSENT_DOC_CITATION_ALLOWED = {
    "tests/browser/webmcp-spike-checklist.md",
    "tests/fixtures/canonicalization/rfc8785_vectors.json",
}


@pytest.mark.architecture
def test_no_shipped_document_cites_a_path_the_repository_does_not_carry() -> None:
    """A clone must not be pointed at a file it was never given.

    This replaces the ADR-corpus gates. Those checked that `docs/adr/` held a
    template, an index, and records whose statuses agreed — a reasonable thing to
    assert while the corpus was tracked, and unrunnable once it was not: they
    passed on a machine that still had the files and failed in CI, which is the
    worst of both readings.

    The obligation that survives the corpus leaving is the one a reader can be
    hurt by. `ADR-0004` names a decision and costs nothing if the record is
    elsewhere; ``docs/adr/0004-rfc-8785-canonicalization.md`` promises a file,
    and a clone does not have it. So this gate holds the promise, not the
    reference — and it checks content the repository actually carries, which is
    the property that lets it run anywhere.
    """
    offenders = [
        f"{path.relative_to(REPO_ROOT).as_posix()} cites {cited}"
        for path in _scanned_files()
        if path.relative_to(REPO_ROOT).as_posix() not in ABSENT_DOC_CITATION_ALLOWED
        for cited in ABSENT_DOC_DIRS
        if cited in path.read_text(encoding="utf-8", errors="ignore")
    ]

    assert offenders == [], (
        "these ship in the repository and name a path it does not carry; cite the "
        f"decision by number instead: {offenders}"
    )


@pytest.mark.architecture
def test_the_readme_says_where_the_decision_records_are() -> None:
    """The numbers are meaningless if nobody says what they refer to.

    136 tracked files name an `ADR-000N`. With the records out of the
    repository, a reader who meets one has no way to learn that the omission is
    deliberate rather than a broken link somebody forgot — so the README says it
    once, in the place the docket used to be.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "outside this repository" in readme, (
        "the README no longer explains that the ADR records are kept out of the repository"
    )
