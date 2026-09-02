"""`docs/CODEMAPS/` stays true, or the build says so.

`AGENTS.md` tells every agent to read these maps *first, instead of scanning
files*. That instruction is what makes a stale map expensive: a reader who
verifies nothing because the map told them where things are will be wrong in
exactly the way the map is wrong, and will not find out until later.

Two kinds of decay are mechanically checkable and both are checked here:

* a map names a path that no longer exists — a file renamed, moved, or deleted
  without the map being updated;
* the routing table in `docs/CODEMAPS/README.md` and the set of map files
  disagree, so a map is unreachable or a link is dead.

What no test can check is whether a *description* is still accurate. That is
stated in the index as a rule for humans, and it is the reason these maps stay
coarse: a map at directory-and-symbol granularity survives ordinary refactoring,
while one listing every function would be false within a week and would train
readers to distrust it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CODEMAPS = REPO_ROOT / "docs" / "CODEMAPS"
INDEX = CODEMAPS / "README.md"

#: Backtick-quoted spans are the only place a map names a path. Prose that
#: mentions a directory without backticks is deliberately not checked — the
#: alternative is a matcher that fires on ordinary English containing a slash.
_CODE_SPAN = re.compile(r"`([^`\n]+)`")

#: A span is treated as a repository path only when it starts with one of these.
#: Anything else in backticks is a symbol, a command, a config key, or a value.
_REPO_ROOTS = (
    "apps/",
    "docs/",
    "examples/",
    "integrations/",
    "packages/",
    "scripts/",
    "shopify_bridge/",
    "tests/",
)

#: Spans that name a shape rather than a file. A glob has no single path to
#: check, an angle bracket marks a placeholder the reader is meant to fill, and
#: an ellipsis marks an abbreviated path — which is banned outright below,
#: because a path a reader cannot paste is a path nobody verifies.
_NOT_A_PATH = ("*", "<", ">", " ")

#: A map may declare the directory its table entries are relative to, so rows can
#: stay short and still be checked. Without this, a map either repeats a 45-
#: character prefix on every row or gives up on verifying most of its own paths —
#: and the second is how `integrations/buggyStore/tools.ts` came to look like a
#: repository path while meaning a frontend-relative one.
_AREA_ROOT = re.compile(r"\*\*Paths below are relative to\*\*\s+`([^`\n]+)`")


def _area_root(document: Path) -> str | None:
    found = _AREA_ROOT.search(document.read_text(encoding="utf-8"))
    return found.group(1).strip() if found else None


def _maps() -> list[Path]:
    return sorted(path for path in CODEMAPS.glob("*.md") if path.name != "README.md")


def _unresolved_paths(document: Path) -> list[str]:
    """Spans that claim to name something on disk and do not.

    Three exclusions, each earned by a false positive this test produced against
    the maps as first written:

    * a span starting with `/` is a **URL route** — `/healthz`, `/audits/packs` —
      not a file, and these maps are full of them;
    * a span with no extension in its last segment is a **directory or a symbol**
      mentioned in passing, such as a sibling layer named as `application/`;
    * a bare filename with no `/` is a **row label**, whose table heading already
      said where it lives.

    What survives is resolved against the repository root *and*, when the map
    declares one, its area root — either satisfying the check. Trying both is
    what lets `integrations/buggyStore/tools.ts` mean the frontend file it says
    it means, without the repository's own `integrations/` shadowing it.
    """
    text = document.read_text(encoding="utf-8")
    root = _area_root(document)
    unresolved: list[str] = []
    for span in _CODE_SPAN.findall(text):
        candidate = span.strip().rstrip(",.;:")
        if any(marker in candidate for marker in _NOT_A_PATH) or candidate.startswith("/"):
            continue
        if "/" not in candidate or "." not in candidate.rsplit("/", 1)[-1]:
            continue
        # Repository root, the map's declared area root, and the map's own
        # directory — the last one so a relative cross-link such as
        # `../ARCHITECTURE.md` is validated rather than reported as missing.
        options = [REPO_ROOT / candidate, document.parent / candidate]
        if root is not None:
            options.append(REPO_ROOT / root.rstrip("/") / candidate)
        if not any(option.exists() for option in options):
            unresolved.append(candidate)
    return unresolved


@pytest.mark.architecture
def test_the_codemaps_directory_exists_with_an_index() -> None:
    """`AGENTS.md` routes agents here, so its absence would be a dead pointer."""
    assert CODEMAPS.is_dir(), "docs/CODEMAPS/ is referenced by AGENTS.md and must exist"
    assert INDEX.is_file(), "docs/CODEMAPS/README.md is the entry point agents are sent to"
    assert _maps(), "an index with no maps orients nobody"


@pytest.mark.architecture
@pytest.mark.parametrize("document", [INDEX, *_maps()], ids=lambda path: path.name)
def test_every_path_a_map_names_exists(document: Path) -> None:
    """A map that points at a moved file is worse than no map.

    The reader trusted it *instead of* looking, so the error arrives with the
    reader's confidence attached.
    """
    missing = _unresolved_paths(document)

    assert missing == [], f"{document.name} names paths that do not exist: {missing}"


@pytest.mark.architecture
@pytest.mark.parametrize("document", _maps(), ids=lambda path: path.name)
def test_no_map_abbreviates_a_path(document: Path) -> None:
    """`apps/.../routes/shopify.py` is not a path, it is a gesture at one.

    An ellipsis defeats both readers: a person cannot paste it, and the check
    above cannot resolve it, so an abbreviated path is the one kind that can rot
    silently. Maps that would otherwise repeat a long prefix declare an area root
    instead — see `_AREA_ROOT`.
    """
    text = document.read_text(encoding="utf-8")
    abbreviated = [span for span in _CODE_SPAN.findall(text) if "..." in span and "/" in span]

    assert abbreviated == [], (
        f"{document.name} abbreviates paths: {abbreviated}; "
        "write them in full or declare an area root"
    )


@pytest.mark.architecture
def test_the_index_routes_to_every_map_and_only_to_maps() -> None:
    """No orphaned map, no dead link — checked in both directions.

    One direction alone is not enough: checking only that links resolve lets a
    new map sit unreachable, and checking only that maps are listed lets a
    deleted one keep its row.
    """
    index_text = INDEX.read_text(encoding="utf-8")
    linked = set(re.findall(r"\(([a-z-]+\.md)\)", index_text))
    present = {path.name for path in _maps()}

    assert linked - present == set(), f"the index links maps that do not exist: {linked - present}"
    assert present - linked == set(), (
        f"maps exist that the index routes nobody to: {present - linked}"
    )


@pytest.mark.architecture
def test_the_maps_stay_lean_enough_to_read_first() -> None:
    """Token-lean is the whole premise (`AGENTS.md`).

    A map that costs as much to read as the files it describes has stopped being
    a map. The bound is generous — it catches a map that has quietly become a
    prose document, not one that gained a few rows.
    """
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in [INDEX, *_maps()]
        if len(path.read_text(encoding="utf-8").splitlines()) > 200
    }

    assert oversized == {}, (
        f"these maps are long enough that a reader would skim rather than trust them: {oversized}; "
        "split by area or cut to directory-and-symbol granularity"
    )
