"""007-T12 — the CLI and FR-088's exit codes (§24.6).

The exit code is the whole interface a CI job sees, so the matrix is tested
explicitly rather than inferred from the runner's status.

The distinction that matters most is **1 versus 2**. A `1` says something about
the target: the code changed and the case noticed. A `2` says nothing about the
target at all — the file was unreadable, or the harness could not run. Merging
them would send somebody to read their own diff when the truth was that a JSON
file had a typo.

And a **reproduced failure exits 0**. §24.3: the case did what it was asked to
do. A CLI that exited 1 there would make this product's sharpest evidence
unusable in CI, which is the whole point of the milestone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.cli import EXIT_DIFFERED, EXIT_INVALID, EXIT_MATCHED, build_parser, main
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

# Only the lane marker: this module mixes sync parser tests with async ones,
# and a module-level `asyncio` mark would be applied to the sync ones too.
# `asyncio_mode = "auto"` already runs the async tests.
pytestmark = pytest.mark.integration

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
FAULT = "discount_reported_but_not_applied"


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    store = create_store(database_path=tmp_path / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ={
                "HARNESS_ENV": "local",
                "BUGGY_STORE_ENABLED": "true",
                "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            },
            database_path=tmp_path / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            # Kept on the app so the CLI test can point the command's own
            # adapter at this in-process store: the CLI builds its registry
            # from the environment exactly as CI would, and only the transport
            # is swapped.
            harness.state.store_app = store
            yield harness


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _case_file(stack: FastAPI, destination: Path) -> Path:
    """Generate a case from a real failed run and write it as CI would receive it."""
    async with client(stack) as visitor:
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
        await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
        await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": "pre_fix"})
        run_id = str((await visitor.post(RUNS)).json()["run_id"])
        for tool, arguments in (
            ("search_catalog", {"query": "mug"}),
            ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
            ("apply_discount", {"code": "SAVE20"}),
        ):
            await visitor.post(
                f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
            )
        await visitor.post(f"{RUNS}/{run_id}/verify")
        created = await visitor.post(f"{RUNS}/{run_id}/evals")
        case_id = created.json()["eval_case_id"]
        download = await visitor.get(f"{API_PREFIX}/evals/{case_id}/case.json")

    destination.write_text(download.text, encoding="utf-8")
    return destination


# --- the parser --------------------------------------------------------------


def test_the_parser_is_structured_rather_than_interpolated() -> None:
    """§24.6: "structured argument parsing, never shell interpolation".

    Every value reaches the runner as data, which is what stops a case path
    from becoming a command.
    """
    # Arrange / Act
    args = build_parser().parse_args(
        ["eval", "run", "case.json", "--environment", "reproduce_source", "--report-dir", "out"]
    )

    # Assert
    assert args.case == "case.json"
    assert args.environment == "reproduce_source"
    assert args.report_dir == "out"


def test_the_default_environment_is_current() -> None:
    """§24.4: "`current` is always the default" and "generated eval cases never
    silently force `reproduce_source`"."""
    # Arrange / Act
    args = build_parser().parse_args(["eval", "run", "case.json"])

    # Assert
    assert args.environment == "current"


def test_an_unknown_environment_is_refused_by_the_parser() -> None:
    """A typo must not silently become a different profile."""
    # Arrange / Act / Assert
    with pytest.raises(SystemExit):
        build_parser().parse_args(["eval", "run", "case.json", "--environment", "prod"])


# --- exit code 2: invalid definition ----------------------------------------


def test_a_missing_file_exits_two(tmp_path: Path) -> None:
    """Nothing was learned about the target, so this is not a failure of it."""
    # Arrange / Act / Assert
    assert main(["eval", "validate", str(tmp_path / "absent.json")]) == EXIT_INVALID


def test_unparseable_json_exits_two(tmp_path: Path) -> None:
    # Arrange
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    # Act / Assert
    assert main(["eval", "validate", str(broken)]) == EXIT_INVALID


def test_a_document_that_is_not_a_case_exits_two(tmp_path: Path) -> None:
    # Arrange
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    # Act / Assert
    assert main(["eval", "validate", str(wrong)]) == EXIT_INVALID


async def test_a_tampered_case_exits_two(stack: FastAPI, tmp_path: Path) -> None:
    """The hash is what a reader who was handed the file can check.

    An edited case is an invalid definition, not a failing target — exiting 1
    would blame code that was never run.
    """
    # Arrange
    path = await _case_file(stack, tmp_path / "case.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["name"] = "renamed-after-signing"
    path.write_text(json.dumps(document), encoding="utf-8")

    # Act / Assert
    assert main(["eval", "validate", str(path)]) == EXIT_INVALID


# --- exit code 0: validation and matched expectations ------------------------


async def test_validating_a_generated_case_exits_zero(
    stack: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC-08 through the CLI: a case generated by the API validates as handed
    over, with no repository, package, or credential in the way."""
    # Arrange
    path = await _case_file(stack, tmp_path / "case.json")

    # Act
    code = main(["eval", "validate", str(path)])

    # Assert
    assert code == EXIT_MATCHED
    assert "ok:" in capsys.readouterr().out


async def test_reproduce_source_recreates_the_failure_and_exits_zero(
    stack: FastAPI,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-15 and AC-12 through the CLI, and §24.3's headline sentence.

    The target failed and the command exits **0**, because reproducing the
    recorded failure is exactly what the case asked for.
    """
    # Arrange
    path = await _case_file(stack, tmp_path / "case.json")
    _point_the_cli_at(monkeypatch, stack)

    # Act
    code = await _cli(
        "eval",
        "run",
        str(path),
        "--environment",
        "reproduce_source",
        "--report-dir",
        str(tmp_path / "out"),
    )

    # Assert
    assert code == EXIT_MATCHED
    printed = capsys.readouterr().out
    assert "eval passed" in printed
    # And the line says plainly that the *target* failed, so nobody reads the
    # zero as "nothing went wrong".
    assert "target outcome failed" in printed


async def test_current_passes_and_exits_zero(
    stack: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-12's other half: `current` runs against the corrected implementation."""
    # Arrange
    path = await _case_file(stack, tmp_path / "case.json")
    _point_the_cli_at(monkeypatch, stack)

    # Act
    code = await _cli("eval", "run", str(path), "--report-dir", str(tmp_path / "out"))

    # Assert
    assert code == EXIT_MATCHED


async def test_the_run_writes_a_canonical_report(
    stack: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-088: every run produces a canonical JSON report.

    A CI job that saw only an exit code has nothing to show the person who asks
    why it went red.
    """
    # Arrange
    path = await _case_file(stack, tmp_path / "case.json")
    _point_the_cli_at(monkeypatch, stack)
    reports = tmp_path / "out"

    # Act
    await _cli("eval", "run", str(path), "--report-dir", str(reports))

    # Assert
    written = list(reports.glob("*.report.json"))
    assert len(written) == 1
    report = json.loads(written[0].read_text(encoding="utf-8"))
    assert report["environment"] == "current"
    assert report["eval_case_hash"].startswith("sha256:")
    # §24.4: the selected profile and both classification sets are present, so
    # a passing eval cannot hide what produced it.
    assert "actual_classifications" in report
    assert "expected_classifications" in report


# --- exit code 1: a valid run that differed ---------------------------------


async def test_an_unrelated_failure_exits_one(
    stack: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-15: "a different or additional critical failure exits `1`".

    Produced by asking `current` to satisfy the *source* expectation: the
    corrected implementation passes, which is not the recorded failure, so the
    case legitimately does not match.
    """
    # Arrange — a case whose `current` expectation demands the source failure.
    path = await _case_file(stack, tmp_path / "case.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["expected"]["current"] = {
        "overall_result": "failed",
        "required_classifications": ["false_success_or_state_mismatch"],
    }
    # Re-sign, so this is a *valid* case that simply expects something else —
    # otherwise the run would exit 2 for a bad hash and prove nothing.
    from actionwitness_core.security.canonical import content_hash

    document.pop("content_hash")
    document["content_hash"] = content_hash(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    _point_the_cli_at(monkeypatch, stack)

    # Act
    code = await _cli("eval", "run", str(path), "--report-dir", str(tmp_path / "out"))

    # Assert
    assert code == EXIT_DIFFERED


async def _cli(*argv: str) -> int:
    """Invoke the real entry point, off the test's event loop.

    `main` calls `asyncio.run`, which cannot nest inside a running loop — and
    that is the shipped behaviour, not an inconvenience: a CI job invokes the
    command from a cold process. Running it in a thread exercises the same
    entry point rather than reaching past it into the coroutine, which would
    let the command itself break while the test stayed green.
    """
    return await asyncio.to_thread(main, list(argv))


def _point_the_cli_at(monkeypatch: pytest.MonkeyPatch, stack: FastAPI) -> None:
    """Make the CLI's own replay reach this test's in-process store.

    The CLI builds its adapter registry from the environment, exactly as a CI
    job would. Only the HTTP transport is replaced, so what runs is the real
    command pipeline rather than a stand-in for it — a test that reimplemented
    the pipeline would pass while the shipped command was broken.
    """
    import httpx as httpx_module

    original = httpx_module.AsyncClient

    def _client(*_args: object, **_kwargs: object) -> httpx_module.AsyncClient:
        return original(
            transport=httpx_module.ASGITransport(app=stack.state.store_app),
            base_url="http://buggy-store.test",
        )

    monkeypatch.setenv("HARNESS_ENV", "local")
    monkeypatch.setenv("BUGGY_STORE_ENABLED", "true")
    monkeypatch.setattr(httpx_module, "AsyncClient", _client)
