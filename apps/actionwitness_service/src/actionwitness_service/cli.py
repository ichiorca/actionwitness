"""`actionwitness` CLI (spec v1.9 §24.6, exit-code contract FR-088).

```text
uv run actionwitness eval validate path/to/case.json
uv run actionwitness eval run path/to/case.json --environment current --report-dir .evals
uv run actionwitness eval run path/to/case.json --environment reproduce_source --report-dir .evals
```

**The exit code is the interface.** FR-088:

- `0` — the actual outcome and the exact critical classification set matched the
  selected environment's expectation;
- `1` — a valid run completed and differed from that expectation;
- `2` — the eval definition or the harness execution was invalid.

The distinction between `1` and `2` is the one worth protecting. A `1` says
something about the *target*: the code changed and the case noticed. A `2` says
nothing about the target at all — the case was malformed, or the harness could
not run it. Collapsing them would tell a CI job "your code broke" when the truth
was "this file is unreadable", and the first thing anyone does with a red build
is look at the wrong place.

**A reproduced failure exits 0.** §24.3: eval status is expectation matching,
not business outcome. `reproduce_source` recreating a recorded failure is the
case doing its job, and a CLI that exited 1 there would make the product's
sharpest evidence unusable in CI.

Structured argument parsing, never shell interpolation (§24.6): every value
reaches the runner as data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["build_parser", "main"]

#: FR-088's three codes, named so a reader of a call site sees the meaning.
EXIT_MATCHED = 0
EXIT_DIFFERED = 1
EXIT_INVALID = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="actionwitness")
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("eval", help="Validate or run built-in regression eval cases")
    evsub = ev.add_subparsers(dest="eval_command", required=True)

    validate = evsub.add_parser("validate", help="Schema-validate an eval case (§24.6)")
    validate.add_argument("case", help="Path to case.json")

    run = evsub.add_parser("run", help="Deterministically replay an eval case (§24.3)")
    run.add_argument("case", help="Path to case.json")
    run.add_argument(
        "--environment",
        choices=["current", "reproduce_source"],
        default="current",
        help="Which implementation to replay against. §24.4 makes `current` the default.",
    )
    run.add_argument(
        "--report-dir",
        default=".evals",
        help="Where to write the canonical JSON report (FR-088).",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "eval" and args.eval_command == "validate":
        return _validate(Path(args.case))
    if args.command == "eval" and args.eval_command == "run":
        return asyncio.run(_run(Path(args.case), args.environment, Path(args.report_dir)))
    print("actionwitness: unknown command", file=sys.stderr)  # pragma: no cover - argparse guards
    return EXIT_INVALID


def _validate(case_path: Path) -> int:
    """`eval validate`. Exit 0 when the file is a case, 2 when it is not.

    Never exit 1 here: nothing was replayed, so there is no expectation to
    differ from, and reporting a malformed file as a mismatch would point a
    reader at their code instead of at the file.
    """
    loaded = _load(case_path)
    if isinstance(loaded, int):
        return loaded

    case, _document = loaded
    print(
        f"ok: {case.id} ({case.name}) schema {case.schema_version}, "
        f"hash {case.content_hash()}, {len(case.trajectory)} step(s)"
    )
    return EXIT_MATCHED


async def _run(case_path: Path, environment_name: str, report_dir: Path) -> int:
    """`eval run`. §24.3's pipeline, with FR-088's codes."""
    from actionwitness_core.evals.enums import EvalEnvironment, EvalStatus

    loaded = _load(case_path)
    if isinstance(loaded, int):
        return loaded
    case, _document = loaded
    environment = EvalEnvironment(environment_name)

    try:
        outcome = await _replay(case, environment)
    except Exception as failure:
        # The harness could not run. Exit 2 rather than 1: nothing was learned
        # about the target, and saying otherwise would send a reader to their
        # own code.
        print(f"error: the harness could not run this case: {failure}", file=sys.stderr)
        return EXIT_INVALID

    written = _write_report(report_dir, case.id, outcome.report)
    # Status and target outcome are printed as two labelled facts. A line
    # reporting one number would be read as the other, and "passed / failed" on
    # a reproduction is exactly the pairing §24.3 exists to keep legible.
    result = outcome.report.overall_result
    classifications = sorted(c.value for c in outcome.report.actual_classifications)
    print(
        f"eval {outcome.report.status.value}: {case.id} against {environment.value} — "
        f"target outcome {result.value if result else 'none'}, "
        f"classifications {classifications or '[]'}"
    )
    if outcome.report.non_replayable_policies:
        # §24.3a: named, never silently dropped, so a pass cannot mean "not
        # checked".
        print(
            "  not evaluated in this mode: "
            + ", ".join(sorted(outcome.report.non_replayable_policies))
        )
    if written is not None:
        print(f"  report: {written}")

    if outcome.report.status is EvalStatus.PASSED:
        return EXIT_MATCHED
    if outcome.report.status is EvalStatus.FAILED:
        return EXIT_DIFFERED
    return EXIT_INVALID


async def _replay(case: Any, environment: Any) -> Any:
    """Run one case against a throwaway harness database.

    The database is temporary because the CLI's job is to answer a question
    about the *target*, not to accumulate history: a case handed to CI must run
    without the harness's own storage, and FR-082 forbids requiring a private
    repository to do it. The eval run and its events are still recorded — into a
    database that lives for the length of the command — so the same code path
    runs here as in the service, rather than a second one that could disagree.
    """
    import os

    import httpx

    from actionwitness_service.application.adapter_registry import AdapterRegistry
    from actionwitness_service.application.eval_run_service import EvalRunService
    from actionwitness_service.application.workspaces import WorkspaceStore
    from actionwitness_service.config import ServiceSettings
    from actionwitness_service.persistence.database import Database

    settings = ServiceSettings.from_env(os.environ)
    with tempfile.TemporaryDirectory(prefix="actionwitness-eval-") as scratch:
        database = Database(Path(scratch) / "eval.sqlite3")
        await database.initialize()
        async with httpx.AsyncClient(
            base_url=settings.buggy_store.base_url if settings.buggy_store else ""
        ) as client:
            registry = AdapterRegistry(settings, client=client)
            workspaces = WorkspaceStore(database)
            async with database.transaction() as work:
                owner = await _owner_workspace(work, workspaces)
                # The case arrived as a *file*, so this database has never seen
                # it — and the eval run references its case (§17.1). Recording
                # it here is what lets the CLI use the service's own pipeline
                # rather than a second one that skipped the reference and could
                # drift from it.
                await _record_case(work, owner, case)
            return await EvalRunService(database, registry, workspaces).run(
                case, owner_workspace_id=owner, environment=environment
            )


async def _record_case(work: Any, workspace_id: str, case: Any) -> None:
    """Store the case this command was handed, in its own ephemeral database.

    Written from the case's own canonical bytes so what the run references is
    exactly what the file contained — re-deriving it here would let the
    recorded case and the replayed one differ, which is the one thing a
    content-addressed artifact must never allow.
    """
    from actionwitness_core.evals.models import CASE_SCHEMA_VERSION
    from actionwitness_core.security.canonical import canonical_text

    await work.execute(
        """
        INSERT INTO evaluation_cases (
            id, workspace_id, source_run_id, contract_content_hash,
            generator_schema_version, schema_version, name, content_hash,
            case_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            case.id,
            workspace_id,
            case.source.run_id,
            case.contract.content_hash,
            CASE_SCHEMA_VERSION,
            case.schema_version,
            case.name,
            case.content_hash(),
            canonical_text(case.as_stored_document()),
            work.now(),
        ),
    )


async def _owner_workspace(work: Any, workspaces: Any) -> str:
    """An interactive workspace to own the eval run.

    FR-083 makes an eval workspace owned by the workspace that requested it. A
    CLI run has no browser session, so one is created here — the ownership is
    what lets the eval workspace be cleaned with its owner rather than aged out
    on its own.
    """
    from actionwitness_service.application.workspaces import new_workspace_id

    workspace_id = new_workspace_id()
    await work.execute(
        "INSERT INTO workspaces (id, kind, created_at, last_seen_at) VALUES (?, ?, ?, ?)",
        (workspace_id, "interactive", work.now(), work.now()),
    )
    return workspace_id


def _load(case_path: Path) -> tuple[Any, dict] | int:
    """Read and validate a case file, or return FR-088's exit code 2.

    Every failure here — missing file, unreadable JSON, schema mismatch, a hash
    that no longer describes its document — is an *invalid definition*. None of
    them says anything about the target.
    """
    from actionwitness_core.evals.schema import validate_case_document
    from actionwitness_core.kernel import CoreError

    try:
        text = case_path.read_text(encoding="utf-8")
    except OSError as unreadable:
        print(f"error: cannot read {case_path}: {unreadable.strerror}", file=sys.stderr)
        return EXIT_INVALID

    try:
        document = json.loads(text)
    except json.JSONDecodeError as malformed:
        print(f"error: {case_path} is not valid JSON: {malformed}", file=sys.stderr)
        return EXIT_INVALID

    if not isinstance(document, dict):
        print(f"error: {case_path} does not contain an eval case object", file=sys.stderr)
        return EXIT_INVALID

    try:
        return validate_case_document(document), document
    except CoreError as invalid:
        print(f"error: {invalid}", file=sys.stderr)
        return EXIT_INVALID


def _write_report(report_dir: Path, case_id: str, report: Any) -> Path | None:
    """Write FR-088's canonical report, or say why it could not be written.

    A failure to write is not a failure of the run: the verdict was reached and
    printed, and turning an unwritable directory into a non-zero exit would make
    CI red for a reason that has nothing to do with the code under test.
    """
    from actionwitness_core.security.canonical import canonical_text

    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        destination = report_dir / f"{case_id}.report.json"
        destination.write_text(canonical_text(report.as_stored_document()), encoding="utf-8")
    except OSError as unwritable:
        print(f"warning: could not write the report: {unwritable.strerror}", file=sys.stderr)
        return None
    return destination


if __name__ == "__main__":
    sys.exit(main())
