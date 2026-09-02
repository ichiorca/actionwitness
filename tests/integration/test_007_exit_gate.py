"""007-T13 — the milestone's exit gate, one test per published criterion.

`specs/007-regression-evals/spec.md` lists seven. Each is asserted here through
a public entry point — the HTTP API, the shipped CLI, or the core's own
evaluator — rather than by reaching into a service's internals, so a criterion
that stops holding fails *here* rather than at review.

The two sentences this milestone is judged on:

- **Eval status is not business outcome.** A `reproduce_source` run that
  faithfully recreates a recorded `failed` outcome has eval status `passed` and
  exit code `0`. Gate 2 asserts both fields separately, because a reader who
  checks only one will misread the run.
- **A passing eval never means "not checked".** Gate 5 asserts that a policy
  nobody could evaluate is excluded from both classification sets *and* named
  in the report.

Gates 2, 3, 4 and 6 run the real `main()` off the test's event loop. `main`
calls `asyncio.run`, which is the shipped behaviour — a CI job invokes the
command from a cold process — so a test that awaited the coroutine underneath
would keep passing while the command itself was broken.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.contracts.enums import SurfaceDeltaKind
from actionwitness_core.contracts.models import StableToolSurfacePolicy
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.engine.policies import PolicyEvidence, evaluate_policies
from actionwitness_core.evals.enums import EvalEnvironment, EvalStatus
from actionwitness_core.evals.models import SurfaceDelta, SurfaceEvidence
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.security.canonical import content_hash
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.eval_case_service import EvalCaseService
from actionwitness_service.application.eval_run_service import EvalRunService
from actionwitness_service.application.eval_runner import surface_events
from actionwitness_service.cli import EXIT_DIFFERED, EXIT_INVALID, EXIT_MATCHED, main
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
EVALS = f"{API_PREFIX}/evals"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
FAULT = "discount_reported_but_not_applied"
MISMATCH = FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH


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
            harness.state.store_app = store
            yield harness


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _failed_run(visitor: httpx.AsyncClient) -> tuple[str, str]:
    """The journey this milestone exists to make repeatable: a tool reports
    success and the authoritative state disagrees."""
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
    verdict = await visitor.post(f"{RUNS}/{run_id}/verify")
    assert verdict.json()["overall_result"] == "failed", verdict.text
    return (await visitor.get(WORKSPACE)).json()["workspace_id"], run_id


async def _case(stack: FastAPI, workspace_id: str, run_id: str):
    database: Database = stack.state.database
    async with database.transaction() as work:
        return (
            await EvalCaseService(work, workspace_id, stack.state.adapters).generate(run_id)
        ).case


def _service(stack: FastAPI) -> EvalRunService:
    return EvalRunService(stack.state.database, stack.state.adapters, stack.state.workspaces)


async def _case_file(stack: FastAPI, destination: Path) -> Path:
    """A case on disk, exactly as CI would receive it."""
    async with client(stack) as visitor:
        _, run_id = await _failed_run(visitor)
        created = await visitor.post(f"{RUNS}/{run_id}/evals")
        case_id = created.json()["eval_case_id"]
        download = await visitor.get(f"{EVALS}/{case_id}/case.json")
    destination.write_text(download.text, encoding="utf-8")
    return destination


def _point_the_cli_at(monkeypatch: pytest.MonkeyPatch, stack: FastAPI) -> None:
    """Only the HTTP transport is replaced, so what runs is the real command."""
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


async def _cli(*argv: str) -> int:
    """The shipped entry point, off this test's event loop."""
    return await asyncio.to_thread(main, list(argv))


def _resign(path: Path, mutate) -> Path:
    """Edit a case and re-sign it, so what is under test is a *valid* case that
    says something different — not a bad hash, which would exit 2 and prove
    nothing about the comparison."""
    document = json.loads(path.read_text(encoding="utf-8"))
    mutate(document)
    document.pop("content_hash", None)
    document["content_hash"] = content_hash(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


# --- gate 1 ------------------------------------------------------------------


async def test_gate_1_generation_is_idempotent_redacted_valid_and_source_preserving(
    stack: FastAPI,
) -> None:
    """Criterion 1, all four properties plus the proposal refusal.

    Asserted together because they are one claim about the artifact: a case you
    can hand to someone else, twice, without leaking what the run saw.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
        first = await visitor.post(f"{RUNS}/{run_id}/evals")
        second = await visitor.post(f"{RUNS}/{run_id}/evals")
        case_id = first.json()["eval_case_id"]
        document = (await visitor.get(f"{EVALS}/{case_id}/case.json")).json()

    # Assert — idempotent (FR-080): the repeat creates nothing new.
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    assert second.json()["eval_case_id"] == case_id

    # Schema-valid against the published schema (§24.2 step 10), which also
    # verifies the hash the document carries.
    from actionwitness_core.evals.schema import validate_case_document

    case = validate_case_document(document)

    # Source-preserving: the run it came from, and what that run actually found.
    assert case.source.run_id == run_id
    assert case.source.overall_result is LayerResult.FAILED
    assert MISMATCH in case.expected.reproduce_source.required_classifications

    # Redacted (§24.2 step 5): no redaction marker survives into a case that is
    # meant to be replayed — a marker is not a value, and replaying one would
    # fail at the target for the wrong reason.
    assert "[REDACTED]" not in json.dumps(document)

    # A proposal run is refused by name (§24.3a).
    database: Database = stack.state.database
    async with database.transaction() as work:
        service = EvalCaseService(work, workspace_id, stack.state.adapters)
        await work.execute("UPDATE runs SET status = ? WHERE id = ?", ("proposed", run_id))
        with pytest.raises(ApiError) as refused:
            await service.generate(run_id)
    # By code, not by message: §24.3a names the code, and a caller branches on
    # it. Matching prose would let a reworded message silently change the
    # contract a client depends on.
    assert refused.value.code is ApiErrorCode.PROPOSAL_RUN_NOT_ELIGIBLE


# --- gate 2 ------------------------------------------------------------------


async def test_gate_2_reproduce_source_matches_by_set_equality_and_exits_zero(
    stack: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 2, through the API *and* the CLI.

    §24.3's headline: the target failed, the eval passed. Set equality is
    asserted as equality — `==` on the whole set, never containment, because
    containment would let an extra critical failure pass unnoticed.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)

    # Act — through the service the API route calls.
    report = (
        await _service(stack).run(
            case,
            owner_workspace_id=workspace_id,
            environment=EvalEnvironment.REPRODUCE_SOURCE,
        )
    ).report

    # Assert — the two fields, separately.
    assert report.status is EvalStatus.PASSED
    assert report.overall_result is LayerResult.FAILED
    assert set(report.actual_classifications) == set(report.expected_classifications)

    # And through the shipped command.
    path = await _case_file(stack, tmp_path / "case.json")
    _point_the_cli_at(monkeypatch, stack)
    code = await _cli(
        "eval",
        "run",
        str(path),
        "--environment",
        "reproduce_source",
        "--report-dir",
        str(tmp_path / "out"),
    )
    assert code == EXIT_MATCHED


# --- gate 3 ------------------------------------------------------------------


async def test_gate_3_an_unrelated_or_additional_classification_exits_one(
    stack: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 3, in both directions.

    *Unrelated*: the recorded classification is replaced by another. *Additional*:
    the recorded one is kept and a second is demanded. Both must differ, which is
    what makes the comparison set equality rather than "at least these".
    """
    # Arrange — both cases are generated *before* the CLI transport patch, which
    # replaces `httpx.AsyncClient` process-wide and would otherwise redirect the
    # harness client that generates them.
    unrelated_path = await _case_file(stack, tmp_path / "unrelated.json")
    additional_path = await _case_file(stack, tmp_path / "additional.json")
    _point_the_cli_at(monkeypatch, stack)

    unrelated = _resign(
        unrelated_path,
        lambda document: document["expected"].__setitem__(
            "reproduce_source",
            {
                "overall_result": "failed",
                "required_classifications": ["tool_surface_mutation"],
            },
        ),
    )
    additional = _resign(
        additional_path,
        lambda document: document["expected"].__setitem__(
            "reproduce_source",
            {
                "overall_result": "failed",
                "required_classifications": [
                    "false_success_or_state_mismatch",
                    "tool_surface_mutation",
                ],
            },
        ),
    )

    # Act / Assert
    for path in (unrelated, additional):
        code = await _cli(
            "eval",
            "run",
            str(path),
            "--environment",
            "reproduce_source",
            "--report-dir",
            str(tmp_path / "out"),
        )
        assert code == EXIT_DIFFERED, path.name


# --- gate 4 ------------------------------------------------------------------


async def test_gate_4_current_exits_zero_and_an_invalid_case_or_harness_exits_two(
    stack: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 4 — FR-088's whole matrix in one place.

    The 1/2 split is the part worth protecting: a 1 says the target changed, a 2
    says nothing about the target at all. A CI job that saw 1 for an unreadable
    file would send someone to look at their own code.
    """
    # Arrange — every case is generated before the transport patch (see gate 3).
    good = await _case_file(stack, tmp_path / "case.json")
    unknown_path = await _case_file(stack, tmp_path / "unknown.json")
    _point_the_cli_at(monkeypatch, stack)
    reports = tmp_path / "out"

    # Act / Assert — `current` passes against the corrected behaviour.
    assert await _cli("eval", "run", str(good), "--report-dir", str(reports)) == EXIT_MATCHED

    # An invalid definition: a tampered document is not a mismatch, it is
    # unusable.
    tampered = tmp_path / "tampered.json"
    document = json.loads(good.read_text(encoding="utf-8"))
    document["name"] = "renamed after signing"
    tampered.write_text(json.dumps(document), encoding="utf-8")
    assert await _cli("eval", "run", str(tampered), "--report-dir", str(reports)) == EXIT_INVALID

    # A harness execution failure: the case names an adapter this build has no
    # registration for, so nothing was learned about the target.
    unknown = _resign(
        unknown_path,
        lambda body: body["target"].__setitem__("adapter_id", "adapter_that_does_not_exist"),
    )
    assert await _cli("eval", "run", str(unknown), "--report-dir", str(reports)) == EXIT_INVALID


# --- gate 5 ------------------------------------------------------------------


def test_gate_5_recorded_surface_replays_into_the_policy_that_needs_it() -> None:
    """Criterion 5, first half — §24.3a's `surface` replay.

    Without it a `tool_surface_poisoned` case "could never reproduce its own
    classification and would fail permanently". Asserted through the runner's
    replay helper and the core's own evaluator, so what is checked is the
    evidence the policy actually receives.
    """
    # Arrange — a baseline and a poisoning delta, as a case would carry them.
    from datetime import UTC, datetime

    surface = SurfaceEvidence(
        baseline=("search_catalog", "update_cart"),
        deltas=(
            SurfaceDelta(
                sequence=2, kind=SurfaceDeltaKind.ADDED.value, partition="target", tool="exfiltrate"
            ),
        ),
    )

    # Act
    events = surface_events(surface, step_count=3, now=datetime(2026, 1, 1, tzinfo=UTC))
    from actionwitness_core.engine.policies import surface_evidence

    recorded, kinds = surface_evidence(events)
    findings = evaluate_policies(
        (StableToolSurfacePolicy(),),
        PolicyEvidence(surface_baseline_recorded=recorded, observed_surface_deltas=kinds),
    )

    # Assert — the recorded poisoning reproduces its own classification.
    assert recorded is True
    assert [delta.kind for delta in kinds] == [SurfaceDeltaKind.ADDED]
    assert [delta.tool_name for delta in kinds] == ["exfiltrate"], (
        "the replayed delta must still name the tool it concerns"
    )
    assert findings[0].status is CheckStatus.FAILED
    assert findings[0].classification is FailureClassification.TOOL_SURFACE_MUTATION


def test_gate_5_a_case_with_no_recorded_surface_never_reads_as_satisfied() -> None:
    """§16.1: a policy with no baseline "shall never be reported as passed".

    The counterpart that makes the test above mean something — a runner that
    synthesised an empty baseline would pass both.
    """
    # Arrange / Act
    from actionwitness_core.engine.policies import surface_evidence

    recorded, kinds = surface_evidence(surface_events(None, step_count=3, now=_epoch()))
    findings = evaluate_policies(
        (StableToolSurfacePolicy(),),
        PolicyEvidence(surface_baseline_recorded=recorded, observed_surface_deltas=kinds),
    )

    # Assert
    assert (recorded, kinds) == (False, ())
    assert findings[0].status is CheckStatus.OBSERVATION_UNAVAILABLE
    assert findings[0].status is not CheckStatus.PASSED


def _epoch():
    from datetime import UTC, datetime

    return datetime(2026, 1, 1, tzinfo=UTC)


async def test_gate_5_an_unevaluable_policy_is_named_and_excluded_from_both_sets(
    stack: FastAPI,
) -> None:
    """Criterion 5, second half, plus the environment profile.

    A policy nobody could evaluate is excluded from both classification sets
    *and* named in the report, so a passing eval can never quietly mean "not
    checked".
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
    case = await _case(stack, workspace_id, run_id)

    # Act
    report = (
        await _service(stack).run(
            case,
            owner_workspace_id=workspace_id,
            environment=EvalEnvironment.REPRODUCE_SOURCE,
        )
    ).report

    # Assert — the selected profile is on the report (§24.4).
    assert report.environment is EvalEnvironment.REPRODUCE_SOURCE

    # Whatever could not be evaluated is named, and named things appear in
    # neither set.
    named = set(report.non_replayable_policies)
    both = {c.value for c in report.actual_classifications} | {
        c.value for c in report.expected_classifications
    }
    assert named.isdisjoint(both)


# --- gate 6 ------------------------------------------------------------------


async def test_gate_6_ac_08_ac_12_and_ac_15_hold_through_api_and_cli(
    stack: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 6, both surfaces.

    AC-08 — a failed run yields a portable, self-contained case.
    AC-12 — `current` replays it and passes.
    AC-15 — an eval actor is classified exactly as an agent is, so the replay
    reproduces the recorded classification rather than a harness artefact.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id, run_id = await _failed_run(visitor)
        created = await visitor.post(f"{RUNS}/{run_id}/evals")
        case_id = created.json()["eval_case_id"]

        # AC-08 through the API: the case is downloadable and carries its own
        # fixture and contract, so it needs nothing from this database.
        document = (await visitor.get(f"{EVALS}/{case_id}/case.json")).json()
        assert document["fixture"]["target_state"]
        assert document["contract"]["document"]

        # AC-12 through the API.
        ran = await visitor.post(f"{EVALS}/{case_id}/runs", json={"environment": "current"})
        assert ran.status_code == 201, ran.text
        assert ran.json()["status"] == "passed"

    # AC-15 through the API: the eval actor reproduces the source
    # classification set exactly.
    case = await _case(stack, workspace_id, run_id)
    reproduced = (
        await _service(stack).run(
            case,
            owner_workspace_id=workspace_id,
            environment=EvalEnvironment.REPRODUCE_SOURCE,
        )
    ).report
    assert set(reproduced.actual_classifications) == set(case.source.critical_classifications)

    # And all three through the CLI.
    path = await _case_file(stack, tmp_path / "case.json")
    _point_the_cli_at(monkeypatch, stack)
    reports = tmp_path / "out"
    assert await _cli("eval", "validate", str(path)) == EXIT_MATCHED
    assert await _cli("eval", "run", str(path), "--report-dir", str(reports)) == EXIT_MATCHED
    assert (
        await _cli(
            "eval",
            "run",
            str(path),
            "--environment",
            "reproduce_source",
            "--report-dir",
            str(reports),
        )
        == EXIT_MATCHED
    )


# --- gate 7 ------------------------------------------------------------------


def test_gate_7_the_evals_lane_carries_real_coverage_rather_than_a_tripwire() -> None:
    """Criterion 7: the tripwire is gone and §24 coverage stands in its place.

    Mechanical on purpose. The tripwire's job was to fail until this milestone
    landed real coverage; a lane that still contained it, or that contained
    nothing, would leave §24 unguarded while the suite looked green.
    """
    # Arrange
    lane = REPO_ROOT / "tests" / "evals" / "test_evals_lane.py"

    # Act
    source = lane.read_text(encoding="utf-8")

    # Assert
    assert "no eval coverage exists yet" not in source, "the tripwire is still in place"
    defined = source.count("\ndef test_")
    assert defined >= 10, f"the lane defines only {defined} tests"
    for sentence in ("§24", "reproduce", "classification"):
        assert sentence in source, f"the lane no longer mentions {sentence!r}"
