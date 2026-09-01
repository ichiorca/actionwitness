"""Install ONLY the Buggy Store into a clean venv, run it, and run its suite.

Spec v1.9 §26.7 and AC-19; BUILD_ORDER §9 lane 1 ("standalone target install/run")
and §7/M2's first exit-gate item: "Buggy Store runs and tests with all assurance
packages absent."

The mirror image of `core_only_isolation.py`. That job proves the assurance core
needs no target; this one proves the target needs no assurance stack. Together
they are the two halves of BUILD_ORDER invariants 1 and 2, and neither can be
demonstrated in the development venv where everything is installed.

"Runs" is checked separately from "tests" because they are different claims.
A distribution can import cleanly and still fail the first time it is asked to
serve a request - a missing runtime dependency that only a request path reaches,
or a console script the packaging never generated. So this performs an actual
storefront journey inside the new environment: seed a cart, apply the discount,
read the total back.

`pytest`, `pytest-asyncio` and `httpx` are installed alongside the store. All
three are test tooling rather than package dependencies - `httpx` is how the
suite drives the ASGI app under ADR-0001's chosen transport, and the store itself
declares only FastAPI, Uvicorn, Pydantic and aiosqlite.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _isolation import REPO_ROOT, IsolationJob, import_roots, run_isolated

__all__ = [
    "ASSURANCE_ROOTS",
    "job",
    "run_isolation_check",
    "store_only_test_files",
]

STORE_PACKAGE: Final = REPO_ROOT / "examples" / "buggy_store"
TESTS_ROOT: Final = REPO_ROOT / "tests"

#: The assurance stack, which this environment must not contain. BUILD_ORDER
#: invariant 2: the Buggy Store "imports no assurance package and runs by itself".
ASSURANCE_ROOTS: Final[frozenset[str]] = frozenset(
    {"actionwitness_core", "actionwitness_service", "integrations"}
)

#: Lanes that verify the repository rather than the store; see the core job's
#: matching note. The architecture gates inspect a workspace this environment
#: deliberately does not have.
EXCLUDED_LANES: Final[frozenset[str]] = frozenset({"architecture"})

#: A real storefront journey, executed inside the clean environment.
#:
#: Deliberately not a smoke test of `import buggy_store`: §26.7 and the M2 exit
#: gate say the store *runs*, and the failure this catches - a dependency only a
#: request path needs - is invisible to an import check.
RUN_PROBE: Final = """
import asyncio, pathlib, tempfile

import httpx
from buggy_store.api import API_PREFIX, WORKSPACE_HEADER, create_app


async def journey() -> None:
    with tempfile.TemporaryDirectory() as workspace:
        app = create_app(database_path=pathlib.Path(workspace) / "store.sqlite3")
        async with (
            app.router.lifespan_context(app),
            httpx.ASGITransport(app=app) as transport,
            httpx.AsyncClient(
                transport=transport,
                base_url="http://buggy-store.test",
                headers={WORKSPACE_HEADER: "ws-1"},
            ) as client,
        ):
            health = await client.get("/healthz")
            assert health.json() == {"status": "ok"}, health.text

            added = await client.post(
                f"{API_PREFIX}/store/cart/mutations",
                json={
                    "product_id": "mug-ceramic-001",
                    "quantity": 1,
                    "request_id": "req-000000000001",
                },
            )
            assert added.status_code == 200, added.text

            discounted = await client.post(
                f"{API_PREFIX}/store/discount", json={"code": "SAVE20"}
            )
            assert discounted.json()["cart"]["total"] == "20.00", discounted.text

            observed = await client.get(f"{API_PREFIX}/store/state")
            total = observed.json()["target_state"]["cart"]["total"]
            assert total == "20.00", observed.text

    print("standalone storefront journey ok")


asyncio.run(journey())
"""


def store_only_test_files() -> list[Path]:
    """Every test file that exercises the store without the assurance stack.

    Derived from imports rather than listed by hand, so a new store test joins
    this suite automatically and a test that grows an `actionwitness_core`
    import leaves it automatically. A hand-written list drifts the moment
    someone forgets it exists.
    """
    selected: list[Path] = []
    for path in sorted(TESTS_ROOT.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        if set(path.relative_to(TESTS_ROOT).parts) & EXCLUDED_LANES:
            continue
        roots = import_roots(path)
        if "buggy_store" in roots and not (roots & ASSURANCE_ROOTS):
            selected.append(path)
    return selected


def job() -> IsolationJob:
    """The store-only job, with its test selection resolved now."""
    return IsolationJob(
        name="store-only",
        package=STORE_PACKAGE,
        requirements=("pytest>=8.3", "pytest-asyncio>=0.24", "httpx>=0.27"),
        must_import=("buggy_store", "buggy_store.api", "buggy_store.service"),
        must_not_import=tuple(sorted(ASSURANCE_ROOTS)),
        test_files=store_only_test_files(),
        run_probe=RUN_PROBE,
        console_scripts=("buggy-store",),
    )


def run_isolation_check(verbose: bool = False) -> tuple[bool, str]:
    """Build the clean environment, prove the store runs, and run its suite."""
    return run_isolated(job(), verbose=verbose)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="print every step's output")
    parser.add_argument("--list", action="store_true", help="list the selected test files")
    arguments = parser.parse_args()

    if arguments.list:
        for path in store_only_test_files():
            print(path.relative_to(REPO_ROOT).as_posix())
        return 0

    ok, output = run_isolation_check(verbose=arguments.verbose)
    print(output)
    if not ok:
        print("store-only isolation FAILED", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
