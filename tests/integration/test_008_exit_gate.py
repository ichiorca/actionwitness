"""008-T12 — the milestone's exit gate, one test per published criterion.

`specs/008-evaluator-import/spec.md` lists five. Each is asserted here through a
public entry point — the HTTP API, the configuration the deployment resolves, or
the core's own arithmetic — so a criterion that stops holding fails *here*
rather than at review.

The two sentences this milestone is judged on:

- **The fixture path needs no credential.** AC-16 requires import and
  correlation "without Node, an LLM API key, Shopify, or a live model call". A
  benchmark that quietly needed one would make the Tier 2 story
  unreproducible for anybody but its author, and CI would go red on a machine
  that had none.
- **A green benchmark never means "not checked".** Every fail-closed input in
  criterion 2 is a way a careless importer would produce numbers anyway —
  guessing a binding, pooling two populations, counting an error as a failure.

Criterion 5 is deliberately narrow about what it claims. AC-16 passing does not
make the Tier 2 gate green: §7.2 also requires AC-25 and AC-26, which belong to
later milestones. The test says so rather than implying otherwise.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.benchmarks.matrix import metrics_for, tally
from actionwitness_core.benchmarks.models import MatrixCounts
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.config import ModuleStatus, ServiceSettings
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = REPO_ROOT / "integrations" / "google_evals" / "fixtures" / "tier2_three_scenarios.json"

BENCHMARKS = f"{API_PREFIX}/benchmarks"
CONTRACTS = f"{API_PREFIX}/contracts"
CANONICAL = "one_mug_save20_no_checkout"
FAULT = "discount_reported_but_not_applied"

FAULTY = "SAVE20 on one mug against the faulty build"
CORRECTED = "SAVE20 on one mug against the corrected build"
OMITTED = "SAVE20 on one mug, discount step omitted"

SCENARIOS = [
    {"scenario_id": FAULTY, "scenario_mode": "pre_fix", "failure_profile": FAULT},
    {"scenario_id": CORRECTED, "scenario_mode": "post_fix"},
    {"scenario_id": OMITTED, "scenario_mode": "post_fix"},
]

#: Everything AC-16 says the path must run without. Set to nothing at all in the
#: environment the gate builds, so "no credential" is a property of the run
#: rather than of the machine that happens to execute it.
FORBIDDEN_CREDENTIALS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "SHOPIFY_ADMIN_TOKEN",
    "SHOPIFY_CLIENT_SECRET",
)


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """The harness with the *minimum* configuration AC-16 allows.

    No model credential, no Shopify, no live evaluator — only the target and the
    artifact root. If the fixture path needed anything else, it would fail here.
    """
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
            yield harness


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _pipeline(visitor: httpx.AsyncClient) -> dict:
    """Select → create → import → seal → replay → finalize → read."""
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")

    created = await visitor.post(
        BENCHMARKS, json={"source_kind": "recorded_fixture", "scenarios": SCENARIOS}
    )
    benchmark_id = created.json()["benchmark_id"]
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=FIXTURE.read_bytes(),
        headers={"content-type": "application/json"},
    )
    await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/replay")
    await visitor.post(f"{BENCHMARKS}/{benchmark_id}/finalize")
    return (await visitor.get(f"{BENCHMARKS}/{benchmark_id}")).json()


async def _suite(visitor: httpx.AsyncClient, **body: object) -> str:
    created = await visitor.post(BENCHMARKS, json={"source_kind": "recorded_fixture", **body})
    return created.json()["benchmark_id"]


def _report(*trials: dict) -> bytes:
    document = {
        "config": {"reporterSchema": "webmcp-evals/0.0.4", "evaluatorVersion": "0.0.4"},
        "results": {
            "results": list(trials),
            "testCount": len(trials),
            "passCount": 0,
            "failCount": 0,
            "errorCount": 0,
        },
    }
    return json.dumps(document).encode("utf-8")


def _trial(name: str, outcome: str = "pass", run_index: int = 0) -> dict:
    return {"test": {"name": name}, "outcome": outcome, "runIndex": run_index, "response": ""}


# --- gate 1 ------------------------------------------------------------------


def test_gate_1_the_import_path_is_configured_without_any_credential() -> None:
    """Criterion 1, as a property of the resolved configuration.

    An empty environment must still enable evaluator import and must *not*
    enable the live model path — the two are separate modules precisely so a
    recorded fixture and a live run can never be silently interchangeable.
    """
    # Arrange
    settings = ServiceSettings.from_env({"HARNESS_ENV": "local"})

    # Assert
    assert settings.is_enabled("evaluator_import")
    assert settings.evaluator_import is not None
    assert settings.module("live_evaluator").status is ModuleStatus.DISABLED
    assert settings.live_evaluator is None
    assert settings.module("shopify").status is ModuleStatus.DISABLED


async def test_gate_1_the_complete_fixture_path_runs_with_no_credential(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 1, end to end.

    Every credential AC-16 names is removed from the process environment first,
    so a path that reached for one would fail rather than quietly succeed on a
    developer machine that had it set.
    """
    # Arrange
    for name in FORBIDDEN_CREDENTIALS:
        monkeypatch.delenv(name, raising=False)

    # Act
    async with client(stack) as visitor:
        body = await _pipeline(visitor)

    # Assert
    assert body["status"] == "completed"
    assert body["counts"]["total_trials"] == 9
    assert body["result_artifact_id"]


async def test_gate_1_no_node_or_subprocess_is_used(stack: FastAPI) -> None:
    """§25.3: "the Tier 2 importer and checked-in fixtures are Python-only at
    runtime".

    FR-098 forbids arbitrary command execution outright, and FR-097 puts any
    future CLI adapter behind an allowlisted argument vector. The import and
    normalization modules therefore reach for no process at all — asserted by
    reading their source, because a `subprocess` import added later would
    otherwise pass every behavioural test.
    """
    # Arrange
    package = REPO_ROOT / "integrations" / "google_evals" / "src" / "integrations"

    # Act
    sources = list((package / "google_evals").glob("*.py"))

    # Assert
    assert sources
    for source in sources:
        text = source.read_text(encoding="utf-8")
        for forbidden in ("import subprocess", "os.system", "popen", "npx "):
            assert forbidden not in text, f"{source.name} reaches for {forbidden!r}"


# --- gate 2 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (b"not json at all", "malformed"),
        (b'{"config": {}, "results": {"results": []}, "extra": 1}', "unsupported shape"),
        (
            json.dumps(
                {
                    "config": {"reporterSchema": "webmcp-evals/9.9.9"},
                    "results": {"results": []},
                }
            ).encode("utf-8"),
            "unpinned schema",
        ),
    ],
)
async def test_gate_2_unreadable_reports_fail_closed(
    stack: FastAPI, payload: bytes, why: str
) -> None:
    """Criterion 2: malformed and unsupported inputs are refused.

    422 rather than a partial import: a report the importer cannot read must
    leave no trials behind, or the matrix would be computed over whatever
    happened to parse.
    """
    # Arrange
    async with client(stack) as visitor:
        benchmark_id = await _suite(visitor)

        # Act
        refused = await visitor.post(
            f"{BENCHMARKS}/{benchmark_id}/imports",
            content=payload,
            headers={"content-type": "application/json"},
        )
        read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")

    # Assert
    assert refused.status_code == 422, why
    assert read.json()["counts"]["total_trials"] == 0


async def test_gate_2_an_oversized_report_fails_closed(stack: FastAPI) -> None:
    """FR-090's 1 MiB cap, enforced before the document is parsed."""
    # Arrange
    async with client(stack) as visitor:
        benchmark_id = await _suite(visitor)

        # Act — unparseable as well as oversized, so the error names which
        # check ran first.
        refused = await visitor.post(
            f"{BENCHMARKS}/{benchmark_id}/imports",
            content=b"{" + b"x" * 1_100_000,
            headers={"content-type": "application/json"},
        )

    # Assert
    assert refused.status_code == 422
    assert "limit" in refused.text


async def test_gate_2_an_ambiguous_binding_fails_closed(stack: FastAPI) -> None:
    """FR-091: a trial with no stable address binds only by explicit choice.

    The importer never guesses one from list position — which is what the
    positional id `#0` would otherwise invite.
    """
    # Arrange
    async with client(stack) as visitor:
        benchmark_id = await _suite(visitor)
        await visitor.post(
            f"{BENCHMARKS}/{benchmark_id}/imports",
            content=_report(_trial("one"), _trial("one")),
            headers={"content-type": "application/json"},
        )

        # Act
        refused = await visitor.put(
            f"{BENCHMARKS}/{benchmark_id}/bindings",
            json={"bindings": [{"external_trial_id": "#0", "evaluation_run_id": "evr-1"}]},
        )

    # Assert
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "TRIAL_BINDING_AMBIGUOUS"


async def test_gate_2_a_duplicate_binding_fails_closed(stack: FastAPI) -> None:
    """§17.1: "a source run cannot be counted twice in one benchmark"."""
    # Arrange
    async with client(stack) as visitor:
        benchmark_id = await _suite(visitor, correlation_mode="imported_trajectory_replay")
        await visitor.post(
            f"{BENCHMARKS}/{benchmark_id}/imports",
            content=_report(_trial("one", run_index=0), _trial("one", run_index=1)),
            headers={"content-type": "application/json"},
        )
        first = await visitor.put(
            f"{BENCHMARKS}/{benchmark_id}/bindings",
            json={"bindings": [{"external_trial_id": "one#0", "evaluation_run_id": "evr-1"}]},
        )
        assert first.status_code == 200, first.text

        # Act — the same trial again.
        refused = await visitor.put(
            f"{BENCHMARKS}/{benchmark_id}/bindings",
            json={"bindings": [{"external_trial_id": "one#0", "evaluation_run_id": "evr-2"}]},
        )

    # Assert
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "TRIAL_ALREADY_BOUND"


async def test_gate_2_a_cross_workspace_benchmark_fails_closed(stack: FastAPI) -> None:
    """§12.4: a known identifier from another workspace grants nothing, and is
    indistinguishable from one that never existed."""
    # Arrange
    async with client(stack) as owner:
        benchmark_id = await _suite(owner)

    # Act
    async with client(stack) as intruder:
        read = await intruder.get(f"{BENCHMARKS}/{benchmark_id}")
        imported = await intruder.post(
            f"{BENCHMARKS}/{benchmark_id}/imports",
            content=_report(_trial("one")),
            headers={"content-type": "application/json"},
        )
        absent = await intruder.get(f"{BENCHMARKS}/bench_never_existed")

    # Assert
    assert read.status_code == absent.status_code == 404
    assert imported.status_code == 404


# --- gate 3 ------------------------------------------------------------------


async def test_gate_3_the_counting_identities_hold_on_the_fixture(stack: FastAPI) -> None:
    """Criterion 3, on the real nine-trial population.

    These three are what a reader of a published benchmark relies on. If the
    cells did not sum to the denominator, every rate would be over a population
    that does not exist.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        counts = (await _pipeline(visitor))["counts"]

    # Assert
    cells = (
        counts["call_level_pass_outcome_pass"]
        + counts["call_level_pass_outcome_fail"]
        + counts["call_level_fail_outcome_pass"]
        + counts["call_level_fail_outcome_fail"]
    )
    assert cells == counts["eligible_trials"]
    assert counts["eligible_trials"] + counts["excluded_trials"] == counts["total_trials"]
    assert counts["error_trials"] <= counts["excluded_trials"]


def test_gate_3_errors_are_a_disclosed_subset_never_a_third_population() -> None:
    """FR-092, at the arithmetic itself.

    An errored trial appears in `excluded_trials` *and* in `error_trials`.
    Adding the two would count it twice and inflate the total.
    """
    # Arrange
    counts = MatrixCounts(call_level_pass_outcome_pass=1, excluded_trials=3, error_trials=2)

    # Act / Assert
    assert counts.total_trials == 4
    assert counts.error_trials < counts.excluded_trials
    assert (
        counts.total_trials != counts.eligible_trials + counts.excluded_trials + counts.error_trials
    )


def test_gate_3_an_empty_population_yields_null_rates_not_zeroes() -> None:
    """FR-092's zero-denominator rule.

    `0.0000` reads as "we measured and found none". Over an empty population
    that is a claim nobody made, and the one a reader would act on.
    """
    # Arrange
    counts = tally(())

    # Act
    metrics = metrics_for(counts)

    # Assert
    assert counts.total_trials == 0
    for rate in (
        metrics.call_level_pass_rate,
        metrics.outcome_pass_rate,
        metrics.end_to_end_success_rate,
        metrics.silent_outcome_failure_rate,
    ):
        assert rate.value is None
        assert rate.display is None


# --- gate 4 ------------------------------------------------------------------


async def test_gate_4_populations_are_never_pooled(stack: FastAPI) -> None:
    """Criterion 4 and AC-16: "does not pool correlation modes, source kinds,
    scenario modes, or failure-profile populations".

    Each is checked where it could actually be violated: the suite carries one
    source kind and one mode, the scenarios stay separate, and only the scenario
    that declared a failure profile forms a profile population.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        body = await _pipeline(visitor)

    # Assert — one source kind and one correlation mode for the whole suite.
    assert body["source_kind"] == "recorded_fixture"
    assert body["correlation_mode"] == "imported_trajectory_replay"

    # Scenario populations are separate and each carries its own denominator.
    labels = [group["label"] for group in body["by_scenario"]]
    assert sorted(labels) == sorted([FAULTY, CORRECTED, OMITTED])
    assert sum(g["counts"]["total_trials"] for g in body["by_scenario"]) == 9

    # A trial with no failure profile joins no profile population rather than
    # being gathered under an invented "none".
    profiles = [group["label"] for group in body["by_failure_profile"]]
    assert profiles == [FAULT]
    faulty = next(g for g in body["by_failure_profile"] if g["label"] == FAULT)
    assert faulty["counts"]["total_trials"] == 3


async def test_gate_4_a_suite_refuses_a_trial_from_the_other_mode(stack: FastAPI) -> None:
    """The mode separation, at the point it could be broken.

    §9.9: the two correlation modes "shall never be aggregated into one rate".
    A suite holding both could not honestly report either.
    """
    # Arrange
    async with client(stack) as visitor:
        browser_suite = await _suite(visitor, correlation_mode="executed_browser")
        replay_suite = await _suite(visitor, correlation_mode="imported_trajectory_replay")

        # Act — a replay is only meaningful for the replay suite.
        refused = await visitor.post(f"{BENCHMARKS}/{browser_suite}/replay")
        allowed = await visitor.get(f"{BENCHMARKS}/{replay_suite}")

    # Assert
    assert refused.status_code == 409
    assert allowed.json()["correlation_mode"] == "imported_trajectory_replay"


# --- gate 5 ------------------------------------------------------------------


async def test_gate_5_ac_16_passes_from_the_checked_in_fixture(stack: FastAPI) -> None:
    """Criterion 5's first half: AC-16, clause by clause.

    Every clause is asserted against one run of the shipped path rather than
    against a stub, because AC-16 is a statement about the product a developer
    would actually use.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        body = await _pipeline(visitor)
        benchmark_id = body["benchmark_id"]
        report = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/report")
        trial = await visitor.get(
            f"{BENCHMARKS}/{benchmark_id}/trials/{FAULTY.replace(' ', '%20')}%230"
        )

    document = json.loads(report.text)

    # Assert — the explicit source kind, never represented as a live execution.
    assert body["source_kind"] == "recorded_fixture"

    # …the preserved immutable source hash and the validated adapter version.
    assert document["manifest"]["source_artifact_hashes"]
    assert document["manifest"]["reporter_schema"] == "webmcp-evals/0.0.4"
    assert document["manifest"]["normalized_adapter_version"] == "1"

    # …the exact two-by-two counts, including at least one silent defect.
    assert body["counts"]["call_level_pass_outcome_fail"] == 3
    assert body["metrics"]["incremental_outcome_failure_trials"] == 3

    # …errors excluded from the rate denominators.
    assert body["counts"]["error_trials"] == 1
    assert (
        body["metrics"]["call_level_pass_rate"]["denominator"] == body["counts"]["eligible_trials"]
    )

    # …coverage, breakdowns, and reachable per-trial evidence.
    assert body["counts"]["excluded_trials"] >= 1
    assert len(body["by_scenario"]) == 3
    assert trial.status_code == 200
    assert trial.json()["call_level_result"] == "passed"
    assert trial.json()["outcome_result"] == "failed"


def test_gate_5_the_tier_two_gate_needs_more_than_this_milestone() -> None:
    """Criterion 5's second half, stated honestly.

    §7.2's Tier 2 release gate requires AC-08, AC-12, AC-15, AC-16, **AC-25 and
    AC-26**. M6 delivered the first three and M7 delivers AC-16; AC-25
    (tool-surface witnessing) and AC-26 (the `self` target) belong to later
    milestones.

    This test exists so "AC-16 is green" is never read as "Tier 2 is done". It
    fails the day somebody adds those milestones' exit gates without revisiting
    this claim — which is exactly when the sentence should be rewritten.
    """
    # Arrange
    gate_007 = REPO_ROOT / "tests" / "integration" / "test_007_exit_gate.py"
    gate_008 = REPO_ROOT / "tests" / "integration" / "test_008_exit_gate.py"

    # Act
    shipped = {
        path.name for path in (REPO_ROOT / "tests" / "integration").glob("test_0*_exit_gate.py")
    }

    # Assert — M6 and M7 have exit gates; the remaining Tier 2 criteria do not
    # yet, and the tier is therefore not green.
    assert gate_007.is_file()
    assert gate_008.is_file()
    assert "test_009_exit_gate.py" not in shipped, (
        "a later milestone landed an exit gate — recheck whether §7.2's Tier 2 "
        "gate (AC-25, AC-26) is now satisfiable and update this claim"
    )
