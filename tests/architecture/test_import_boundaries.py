"""Architecture gates (spec v1.8 §26.7, LD-6/7/8) — enforced from day one.

These tests parse source files with `ast`; they need no dependencies installed
and must stay green for every commit:

1. `actionwitness_core` never imports an application, integration, demo, evaluator-vendor,
   frontend, or commerce-domain module (or FastAPI/persistence drivers).
2. The standalone Buggy Store example never imports the assurance stack — it must
   build and run with `actionwitness_core` absent.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CORE_SRC = REPO_ROOT / "packages" / "actionwitness_core" / "src"
STORE_SRC = REPO_ROOT / "examples" / "buggy_store" / "src"

FORBIDDEN_FOR_CORE = {
    # application / composition layer
    "actionwitness_service",
    # integrations and demo/commerce modules
    "integrations", "buggy_store", "shopify", "shopify_bridge",
    # evaluator vendors
    "google_evals", "webmcp_evals",
    # web framework / server / persistence drivers (application concerns)
    "fastapi", "starlette", "uvicorn", "aiosqlite", "sqlite3", "httpx", "requests",
}

FORBIDDEN_FOR_STORE = {
    "actionwitness_core", "actionwitness_service", "integrations",
}


def _import_roots(pyfile: Path) -> set[str]:
    tree = ast.parse(pyfile.read_text(encoding="utf-8"), filename=str(pyfile))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _violations(src_root: Path, forbidden: set[str]) -> list[str]:
    problems = []
    for pyfile in sorted(src_root.rglob("*.py")):
        hits = _import_roots(pyfile) & forbidden
        if hits:
            problems.append(f"{pyfile.relative_to(REPO_ROOT)} imports {sorted(hits)}")
    return problems


def test_actionwitness_core_has_no_forbidden_imports():
    assert CORE_SRC.is_dir(), "expected packages/actionwitness_core/src to exist"
    assert _violations(CORE_SRC, FORBIDDEN_FOR_CORE) == []


def test_buggy_store_is_independent_of_the_assurance_stack():
    assert STORE_SRC.is_dir(), "expected examples/buggy_store/src to exist"
    assert _violations(STORE_SRC, FORBIDDEN_FOR_STORE) == []
