"""010-T12 — the milestone's exit gate, one test per published criterion.

`specs/010-live-model-benchmark/spec.md` lists six. Five are testable here; the
sixth depends on a live run against a real credential, which is an operator gate
(T11), and the test for it says so rather than asserting something weaker and
calling it green.

The sentence this milestone is judged on: **a live-sourced report travels the
AC-16 pipeline unchanged.** If it had needed a second import path or a special
case in correlation, the Tier 2 claim was never as general as it looked. The
criterion-3 test imports the same bytes under both source kinds and compares
everything except the label.

Criterion 6's stop is real: BUILD_ORDER §7/M9 says "if it does not, do not start
Shopify work". `test_gate_6_shopify_work_has_not_started` fails the day 011
lands while AC-17 is still unproven, which is the moment somebody would need
telling.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.benchmarks.enums import SourceKind
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI
from integrations.google_evals.live import describe_live_run, source_kind_for

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURE = REPO_ROOT / "integrations" / "google_evals" / "fixtures" / "tier2_three_scenarios.json"

BENCHMARKS = f"{API_PREFIX}/benchmarks"
CONTRACTS = f"{API_PREFIX}/contracts"
CANONICAL = "one_mug_save20_no_checkout"
FAULT = "discount_reported_but_not_applied"
CREDENTIAL = "EXAMPLE_MODEL_KEY"
SECRET = "sk-live-this-would-be-a-real-key"  # not-a-real-credential

FAULTY = "SAVE20 on one mug against the faulty build"
CORRECTED = "SAVE20 on one mug against the corrected build"
OMITTED = "SAVE20 on one mug, discount step omitted"

SCENARIOS = [
    {"scenario_id": FAULTY, "scenario_mode": "pre_fix", "failure_profile": FAULT},
    {"scenario_id": CORRECTED, "scenario_mode": "post_fix"},
    {"scenario_id": OMITTED, "scenario_mode": "post_fix"},
]

LIVE = {
    "LIVE_EVALUATOR_ENABLED": "true",
    "LIVE_EVALUATOR_PROVIDER": "google",
    "LIVE_EVALUATOR_MODEL": "example-model-1",
    "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
    CREDENTIAL: SECRET,
}


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _deployment(root: Path, extra: dict[str, str]) -> AsyncIterator[FastAPI]:
    root.mkdir(parents=True, exist_ok=True)
    store = create_store(database_path=root / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ={
                "HARNESS_ENV": "local",
                "BUGGY_STORE_ENABLED": "true",
                "HARNESS_ARTIFACT_ROOT": str(root / "artifacts"),
                **extra,
            },
            database_path=root / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            yield harness


@pytest.fixture
async def live_stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async for app in _deployment(tmp_path / "live", LIVE):
        yield app


@pytest.fixture
async def offline_stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    async for app in _deployment(tmp_path / "offline", {}):
        yield app


async def _pipeline(visitor: httpx.AsyncClient, source_kind: str) -> dict:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    created = await visitor.post(
        BENCHMARKS, json={"source_kind": source_kind, "scenarios": SCENARIOS}
    )
    assert created.status_code == 201, created.text
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


# --- gate 1 ------------------------------------------------------------------


async def test_gate_1_a_live_suite_is_labelled_by_the_application(
    live_stack: FastAPI,
) -> None:
    """Criterion 1: the suite is labelled `live_model_run`, and the label is the
    application's to give."""
    # Arrange / Act
    async with client(live_stack) as visitor:
        body = await _pipeline(visitor, "live_model_run")

    # Assert
    assert body["source_kind"] == "live_model_run"
    assert body["manifest"]["source_kind"] == "live_model_run"


async def test_gate_1_a_fixture_is_never_represented_as_a_live_execution(
    offline_stack: FastAPI,
) -> None:
    """Criterion 1's other half, and §25.3's blunt version of it.

    Refused rather than downgraded: a caller who asked for live and silently
    received a fixture would present its numbers as a model result.
    """
    # Arrange / Act
    async with client(offline_stack) as visitor:
        refused = await visitor.post(BENCHMARKS, json={"source_kind": "live_model_run"})
        body = await _pipeline(visitor, "recorded_fixture")

    # Assert
    assert refused.status_code == 409
    assert body["source_kind"] == "recorded_fixture"
    assert source_kind_for(None) is SourceKind.RECORDED_FIXTURE


# --- gate 2 ------------------------------------------------------------------


async def test_gate_2_exported_parameters_are_recorded_without_invention(
    live_stack: FastAPI,
) -> None:
    """Criterion 2 and AC-17: "records actual exported evaluator/model
    parameters without inventing missing values".

    The fixture exports parameters, so they are recorded; the deployment's own
    configured model is *not* substituted for anything the report omitted.
    """
    # Arrange / Act
    async with client(live_stack) as visitor:
        body = await _pipeline(visitor, "live_model_run")

    # Assert — what the report carried.
    manifest = body["manifest"]
    assert manifest["model_parameters"] == {"temperature": 0}
    assert manifest["model_name"] == "recorded-fixture"

    # …and the configured model is not the one recorded.
    configured = describe_live_run(live_stack.state.settings.live_evaluator)
    assert configured.model == "example-model-1"
    assert manifest["model_name"] != configured.model


# --- gate 3 ------------------------------------------------------------------


async def test_gate_3_a_live_report_travels_the_ac_16_pipeline_unchanged(
    live_stack: FastAPI, offline_stack: FastAPI
) -> None:
    """Criterion 3, and the milestone's real question.

    The same bytes under both source kinds. Everything describing the trials
    must match — a difference would mean the Tier 2 pipeline had a
    fixture-shaped special case, and its generality was never real.
    """
    # Arrange / Act
    async with client(offline_stack) as offline:
        recorded = await _pipeline(offline, "recorded_fixture")
    async with client(live_stack) as online:
        live = await _pipeline(online, "live_model_run")

    # Assert
    assert live["counts"] == recorded["counts"]
    assert live["metrics"] == recorded["metrics"]
    assert live["trials"] == recorded["trials"]
    assert live["by_scenario"] == recorded["by_scenario"]
    assert live["source_kind"] != recorded["source_kind"]


async def test_gate_3_every_eligible_trial_binds_and_the_matrix_holds(
    live_stack: FastAPI,
) -> None:
    """Criterion 3: "binds each eligible trial exactly, produces the dual-layer
    matrix and silent-outcome-defect evidence"."""
    # Arrange / Act
    async with client(live_stack) as visitor:
        body = await _pipeline(visitor, "live_model_run")

    # Assert — the counting identities, and the signal itself.
    counts = body["counts"]
    cells = (
        counts["call_level_pass_outcome_pass"]
        + counts["call_level_pass_outcome_fail"]
        + counts["call_level_fail_outcome_pass"]
        + counts["call_level_fail_outcome_fail"]
    )
    assert cells == counts["eligible_trials"]
    assert counts["eligible_trials"] + counts["excluded_trials"] == counts["total_trials"]
    assert counts["call_level_pass_outcome_fail"] == 3
    assert body["metrics"]["incremental_outcome_failure_trials"] == 3

    # Every eligible trial references the run that produced its outcome.
    eligible = [t for t in body["trials"] if t["eligibility"] == "eligible"]
    assert len(eligible) == counts["eligible_trials"]


# --- gate 4 ------------------------------------------------------------------


async def test_gate_4_the_credential_stays_in_the_evaluator_environment(
    live_stack: FastAPI,
) -> None:
    """Criterion 4 and AC-17: retained "only in the evaluator process
    environment".

    This deployment has one set. Nothing a client can reach may contain it —
    not the suite, not the finalized artifact, not the health endpoint.
    """
    # Arrange / Act
    async with client(live_stack) as visitor:
        body = await _pipeline(visitor, "live_model_run")
        report = await visitor.get(f"{BENCHMARKS}/{body['benchmark_id']}/report")
        health = await visitor.get("/healthz")

    # Assert
    assert SECRET not in json.dumps(body)
    assert SECRET not in report.text
    assert SECRET not in health.text
    # The variable *name* may appear — it tells an operator where to look.
    assert describe_live_run(live_stack.state.settings.live_evaluator).credential_var == CREDENTIAL


async def test_gate_4_a_credential_cannot_arrive_through_an_upload(
    live_stack: FastAPI,
) -> None:
    """FR-099's fourth prohibited channel, refused before anything is written."""
    # Arrange
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    document["config"]["apiKey"] = SECRET

    # Act
    async with client(live_stack) as visitor:
        created = await visitor.post(BENCHMARKS, json={"source_kind": "live_model_run"})
        benchmark_id = created.json()["benchmark_id"]
        refused = await visitor.post(
            f"{BENCHMARKS}/{benchmark_id}/imports",
            content=json.dumps(document).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")

    # Assert
    assert refused.status_code == 422
    assert SECRET not in refused.text
    assert read.json()["counts"]["total_trials"] == 0


# --- gate 5 ------------------------------------------------------------------


async def test_gate_5_the_ci_fixture_path_passes_with_no_credential(
    offline_stack: FastAPI,
) -> None:
    """Criterion 5 and AC-17's second paragraph: "the same import/correlation
    path must still pass in CI from the checked-in redacted fixture, which
    remains clearly labeled `recorded_fixture`"."""
    # Arrange / Act
    async with client(offline_stack) as visitor:
        body = await _pipeline(visitor, "recorded_fixture")
        report = await visitor.get(f"{BENCHMARKS}/{body['benchmark_id']}/report")

    # Assert
    assert body["status"] == "completed"
    assert body["source_kind"] == "recorded_fixture"
    assert json.loads(report.text)["manifest"]["source_kind"] == "recorded_fixture"
    assert "live_model_run" not in report.text


# --- gate 6 ------------------------------------------------------------------


def test_gate_6_ac_17_needs_a_live_run_this_suite_cannot_perform() -> None:
    """Criterion 6, stated honestly rather than asserted green.

    AC-17 requires "a configured supported model backend" executing "at least
    three completed **live** trials". Every test above runs against the
    checked-in fixture, which is exactly what AC-17's second paragraph asks CI
    to do — and exactly what it says is *not* evidence that a live call
    occurred.

    So AC-17 is **not** proven by this file. What is proven is the path a live
    report travels, the labelling, the parameter capture, and the credential
    boundary. The remaining step is 010-T11, an operator gate: a real
    credential, a real generation, and a human approving the variants.

    This test exists so the gap is recorded where a reader of the gate will
    find it, rather than inferred from the absence of a test.
    """
    # Arrange
    tasks = (REPO_ROOT / "specs" / "010-live-model-benchmark" / "tasks.md").read_text(
        encoding="utf-8"
    )

    # Act / Assert — T11 is still open, and still marked an operator gate.
    assert "- [ ] T11 — (operator gate)" in tasks, (
        "T11 has moved: if a live run has now happened, AC-17's status needs "
        "restating here rather than inheriting this test's caveat"
    )


def test_gate_6_shopify_work_has_not_started() -> None:
    """BUILD_ORDER §7/M9: "If it does not [pass], do not start Shopify work."

    **Ticked tasks, not the spec's existence.** This test first asserted that no
    `011*` directory existed at all, which was a proxy rather than the
    requirement — and the wrong one. Authoring a spec is planning; ticking a
    task is work. This repository has authored the next milestone's WHAT ahead
    of the current gate before (009 was pre-drafted during 007, and 008 during
    007's exit gate), and BUILD_ORDER's own entry conditions are written to be
    read before the work begins.

    So the gate now enforces what §7/M9 actually forbids: 011 may be planned
    while AC-17 is unproven, and none of its tasks may be marked done. It fails
    the moment one is, which is the moment somebody needs telling.
    """
    # Arrange
    shopify_specs = sorted(
        path for path in (REPO_ROOT / "specs").iterdir() if path.name.startswith("011")
    )

    # Act
    ticked: list[str] = []
    for spec in shopify_specs:
        tasks = spec / "tasks.md"
        if not tasks.is_file():
            continue
        ticked += [
            line.strip()
            for line in tasks.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("- [x]")
        ]

    # Assert
    assert not ticked, (
        "Shopify tasks are marked done while AC-17 is unproven: "
        + "; ".join(ticked)
        + ". BUILD_ORDER §7/M9 gates M10 on a passing live benchmark, and "
        "§7/M10's entry condition also requires the development-store "
        "configuration to be locked."
    )


# --- the authored live suite -------------------------------------------------

SUITE = REPO_ROOT / "integrations" / "google_evals" / "scenarios" / "save20_suite.json"

#: The one instruction that distinguishes the omitted case: its prompt forbids
#: the discount. A rubric that still requires `apply_discount` would score an
#: obedient model as failing at the call level, and the scenario could then
#: never land in the evaluator-passed / outcome-failed cell it exists to
#: demonstrate.
FORBIDS_DISCOUNT = "Do not apply any discount code"
REQUESTS_DISCOUNT = "apply the discount code SAVE20"


def _suite_cases() -> list[dict]:
    return json.loads(SUITE.read_text(encoding="utf-8"))


def test_the_authored_suite_names_are_exactly_the_ids_this_gate_binds() -> None:
    """FR-091 binds trials by scenario id and forbids guessing: the case names
    in the authored suite and the scenario ids declared at suite creation must
    match byte for byte, or every live trial imports as unbound."""
    # Arrange / Act
    names = [case["name"] for case in _suite_cases()]

    # Assert
    assert names == [FAULTY, CORRECTED, OMITTED]


def test_each_suite_expectation_is_consistent_with_its_own_prompt() -> None:
    """A rubric that requires a call its own prompt forbids scores obedience as
    failure — the copy-paste this catches survived because nothing read the
    suite until a live run would have."""
    for case in _suite_cases():
        # Arrange
        prompt = " ".join(message["content"] for message in case["messages"])
        expected = [call["functionName"] for call in case["expectedCall"]]

        # Act — each prompt states its discount intent exactly one way.
        forbids = FORBIDS_DISCOUNT in prompt
        requests = REQUESTS_DISCOUNT in prompt
        assert forbids != requests, (
            f"{case['name']!r}: the prompt must either request or forbid the "
            "discount, unambiguously"
        )

        # Assert — the rubric agrees with the instruction it grades against.
        if forbids:
            assert "apply_discount" not in expected, (
                f"{case['name']!r} forbids the discount but still expects "
                "apply_discount; an obedient model would fail at the call level"
            )
        else:
            assert "apply_discount" in expected, (
                f"{case['name']!r} asks for the discount but does not expect apply_discount"
            )
