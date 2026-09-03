"""Architecture gates (spec v1.9 §26.7, LD-6/7/8) — enforced from day one.

These tests parse source files with `ast`; they need no dependencies installed
and must stay green for every commit:

1. `actionwitness_core` never imports an application, integration, demo, evaluator-vendor,
   frontend, or commerce-domain module (or FastAPI/persistence drivers).
2. The standalone Buggy Store example never imports the assurance stack — it must
   build and run with `actionwitness_core` absent.
"""

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

CORE_SRC = REPO_ROOT / "packages" / "actionwitness_core" / "src"
STORE_SRC = REPO_ROOT / "examples" / "buggy_store" / "src"
SELF_TARGET = REPO_ROOT / "integrations" / "self_target"
SELF_TARGET_SRC = SELF_TARGET / "src"

FORBIDDEN_FOR_CORE = {
    # application / composition layer
    "actionwitness_service",
    # integrations and demo/commerce modules
    "integrations",
    "buggy_store",
    "shopify",
    "shopify_bridge",
    # evaluator vendors
    "google_evals",
    "webmcp_evals",
    # web framework / server / persistence drivers (application concerns)
    "fastapi",
    "starlette",
    "uvicorn",
    "aiosqlite",
    "sqlite3",
    "httpx",
    "requests",
}

FORBIDDEN_FOR_STORE = {
    "actionwitness_core",
    "actionwitness_service",
    "integrations",
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


#: FR-171: the built-in `self` target "shall not receive privileged access
#: unavailable to a third-party adapter; anything it needs is a defect in the
#: public protocol and shall be fixed there."
#:
#: A third-party adapter has the core's protocols and a way to make HTTP
#: requests. So this one has `actionwitness_core` and `httpx`, and the two gates
#: below hold it to that from opposite directions — one on what it imports, one
#: on what it is permitted to import. The pair matters: the AST check alone
#: passes for a package that merely has not reached inside *yet*, and the
#: manifest check alone passes for a package importing something it forgot to
#: declare.
FORBIDDEN_FOR_SELF_TARGET = {
    "actionwitness_service",
    "aiosqlite",
    "sqlite3",
    "fastapi",
    "starlette",
    "buggy_store",
}

ALLOWED_SELF_TARGET_DEPENDENCIES = {"actionwitness-core", "httpx"}


def test_the_self_target_reaches_the_harness_only_over_its_public_api():
    assert SELF_TARGET_SRC.is_dir(), "expected integrations/self_target/src to exist"
    assert _violations(SELF_TARGET_SRC, FORBIDDEN_FOR_SELF_TARGET) == []


def test_the_self_target_may_not_declare_a_privileged_dependency():
    manifest = tomllib.loads((SELF_TARGET / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        requirement.split(">")[0].split("=")[0].split("[")[0].strip()
        for requirement in manifest["project"]["dependencies"]
    }
    assert declared == ALLOWED_SELF_TARGET_DEPENDENCIES, (
        "the self target may depend only on the public core protocols and an HTTP "
        f"client; it declares {sorted(declared)}"
    )
