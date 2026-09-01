"""010-T8 — recording exported parameters exactly (FR-093, AC-17).

AC-17: the application "records actual exported evaluator/model parameters
without inventing missing values". FR-093: "missing unsupported metadata shall
be `null`, never inferred".

**`null` and `{}` are different facts, and both are reachable.** `{}` says the
evaluator exported no parameters; `null` says this report did not carry the
field, so the harness does not know. Collapsing them would turn "we do not know"
into "there were none" — a claim about the run that nobody made, and one a
reader would reasonably act on when comparing two benchmarks.

**Exactly means exactly.** A parameter the harness does not understand survives
into the manifest unchanged rather than being dropped, renamed, or coerced. The
manifest is a reproducibility record: somebody re-running this benchmark needs
the values that were actually in force, including the ones this build has never
heard of.

**Nothing is filled in from configuration.** The deployment knows a provider and
a model, and it would be easy to write them into a manifest whose report omitted
them. That would describe the run somebody intended rather than the one that
happened, which is precisely what AC-17's "without inventing" forbids.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI
from integrations.google_evals.normalize import normalize
from integrations.google_evals.reader import read_report

pytestmark = pytest.mark.integration

BENCHMARKS = f"{API_PREFIX}/benchmarks"
CREDENTIAL = "EXAMPLE_MODEL_KEY"

LIVE_ENVIRON = {
    "LIVE_EVALUATOR_ENABLED": "true",
    "LIVE_EVALUATOR_PROVIDER": "google",
    "LIVE_EVALUATOR_MODEL": "example-model-1",
    "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
    CREDENTIAL: "not-a-real-key",
}


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
                **LIVE_ENVIRON,
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


def _report(config: dict) -> bytes:
    document = {
        "config": {"reporterSchema": "webmcp-evals/0.0.4", "evaluatorVersion": "0.0.4", **config},
        "results": {
            "results": [
                {"test": {"name": "one"}, "outcome": "pass", "runIndex": 0, "response": ""}
            ],
            "testCount": 1,
            "passCount": 1,
            "failCount": 0,
            "errorCount": 0,
        },
    }
    return json.dumps(document).encode("utf-8")


def _manifest_fields(config: dict) -> dict:
    from actionwitness_core.benchmarks.enums import CorrelationMode

    imported = read_report(_report(config))
    return dict(
        normalize(imported, correlation_mode=CorrelationMode.EXECUTED_BROWSER).manifest_fields
    )


async def _imported_manifest(stack: FastAPI, config: dict) -> dict:
    async with client(stack) as visitor:
        created = await visitor.post(BENCHMARKS, json={"source_kind": "live_model_run"})
        benchmark_id = created.json()["benchmark_id"]
        imported = await visitor.post(
            f"{BENCHMARKS}/{benchmark_id}/imports",
            content=_report(config),
            headers={"content-type": "application/json"},
        )
        assert imported.status_code == 201, imported.text
        read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
    return read.json()["manifest"]


# --- the null / empty distinction --------------------------------------------


def test_absent_parameters_are_null() -> None:
    """FR-093: missing metadata is `null`, never inferred."""
    # Arrange / Act
    fields = _manifest_fields({})

    # Assert
    assert fields["model_parameters"] is None


def test_explicitly_empty_parameters_stay_empty() -> None:
    """`{}` is the evaluator saying it exported none — a different fact from
    the report not carrying the field, and one a reader comparing two
    benchmarks would act on."""
    # Arrange / Act
    fields = _manifest_fields({"modelParameters": {}})

    # Assert
    assert fields["model_parameters"] == {}
    assert fields["model_parameters"] is not None


def test_a_malformed_parameters_value_is_null_rather_than_coerced() -> None:
    """A string where an object belongs is not parameters. Recorded as
    unknown rather than parsed into a shape the report did not have."""
    # Arrange / Act
    fields = _manifest_fields({"modelParameters": "temperature=0"})

    # Assert
    assert fields["model_parameters"] is None


# --- exactly means exactly ---------------------------------------------------


def test_exported_parameters_are_recorded_verbatim() -> None:
    """AC-17: "actual exported evaluator/model parameters"."""
    # Arrange
    exported = {"temperature": 0, "top_p": 0.95, "max_output_tokens": 1024, "seed": 7}

    # Act
    fields = _manifest_fields({"modelParameters": exported})

    # Assert
    assert fields["model_parameters"] == exported


def test_a_parameter_this_build_has_never_heard_of_survives() -> None:
    """The manifest is a reproducibility record.

    Somebody re-running this benchmark needs the values that were in force,
    including the ones this build does not recognise — dropping them would make
    the record describe a simpler run than the one that happened.
    """
    # Arrange
    exported = {"temperature": 0, "someFutureKnob": {"depth": 3, "mode": "wide"}}

    # Act
    fields = _manifest_fields({"modelParameters": exported})

    # Assert
    assert fields["model_parameters"]["someFutureKnob"] == {"depth": 3, "mode": "wide"}


def test_numeric_parameters_keep_their_type() -> None:
    """`0` and `0.0` are different settings to a model, and a manifest that
    rounded one into the other would misdescribe the run."""
    # Arrange
    exported = {"temperature": 0, "top_p": 1.0, "penalty": -0.5}

    # Act
    fields = _manifest_fields({"modelParameters": exported})

    # Assert
    assert isinstance(fields["model_parameters"]["temperature"], int)
    assert isinstance(fields["model_parameters"]["top_p"], float)
    assert fields["model_parameters"]["penalty"] == -0.5


# --- nothing is filled in from configuration ---------------------------------


async def test_the_deployment_does_not_supply_a_missing_model_name(
    stack: FastAPI,
) -> None:
    """AC-17: "without inventing missing values".

    This deployment is configured with a provider and a model. A report that
    omits them must still record `null`, because the manifest describes the run
    that happened rather than the environment that imported it.
    """
    # Arrange / Act
    manifest = await _imported_manifest(stack, {})

    # Assert
    assert manifest["model_name"] is None
    assert manifest["model_provider"] is None
    assert manifest["model_parameters"] is None


async def test_the_report_supplies_the_values_when_it_has_them(stack: FastAPI) -> None:
    """The counterpart: what the report *did* export is recorded."""
    # Arrange / Act
    manifest = await _imported_manifest(
        stack,
        {
            "modelProvider": "example-provider",
            "modelName": "example-model-7",
            "modelParameters": {"temperature": 0},
            "targetBuildCommit": "abc1234",
        },
    )

    # Assert
    assert manifest["model_provider"] == "example-provider"
    assert manifest["model_name"] == "example-model-7"
    assert manifest["model_parameters"] == {"temperature": 0}
    assert manifest["target_build_commit"] == "abc1234"


async def test_a_reported_model_is_not_overwritten_by_the_configured_one(
    stack: FastAPI,
) -> None:
    """The report is the authority on what ran.

    A deployment configured for `example-model-1` importing a report from
    `example-model-7` must record the seven — otherwise the manifest would
    attribute one model's results to another.
    """
    # Arrange / Act
    manifest = await _imported_manifest(stack, {"modelName": "example-model-7"})

    # Assert
    assert manifest["model_name"] == "example-model-7"


async def test_the_parameters_reach_the_finalized_artifact(stack: FastAPI) -> None:
    """FR-093 puts them in the reproducibility manifest, and FR-094 makes the
    finalized artifact the immutable copy of it."""
    # Arrange
    async with client(stack) as visitor:
        # An `executed_browser` suite, because §16.4 lets that one finalize
        # straight from `ready` — this test is about the manifest reaching the
        # artifact, and a replay would only add moving parts to the arrangement.
        created = await visitor.post(
            BENCHMARKS,
            json={"source_kind": "live_model_run", "correlation_mode": "executed_browser"},
        )
        benchmark_id = created.json()["benchmark_id"]
        await visitor.post(
            f"{BENCHMARKS}/{benchmark_id}/imports",
            content=_report({"modelParameters": {"temperature": 0, "seed": 7}}),
            headers={"content-type": "application/json"},
        )
        await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})
        await visitor.post(f"{BENCHMARKS}/{benchmark_id}/finalize")

        # Act
        report = await visitor.get(f"{BENCHMARKS}/{benchmark_id}/report")

    # Assert
    document = json.loads(report.text)
    assert document["manifest"]["model_parameters"] == {"temperature": 0, "seed": 7}
