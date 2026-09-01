"""010-T2 — where a model credential may and may not travel (FR-099, AC-17).

FR-099 names four places a credential must never arrive: **the browser, a
WebMCP argument, a committed file, and an uploaded benchmark manifest**. AC-17
adds the positive form — retained "only in the evaluator process environment".
There is one test per prohibited place, because each is closed by a different
mechanism and any of them could be reopened on its own.

| Prohibited place | What closes it |
|---|---|
| the browser | closed request bodies; no route accepts credential material |
| a WebMCP argument | closed tool input schemas; no tool declares such a field |
| a committed file | the checked-in fixture and `.env` handling |
| an uploaded manifest | `screen_for_credential_material`, before any write |

The positive guarantee is tested too: the value never enters the harness
process, so `LiveEvaluatorSettings` holds a variable *name* and nothing that
renders a suite can reach the value behind it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.config import ServiceSettings
from buggy_store.api import create_app as create_store
from fastapi import FastAPI
from integrations.google_evals.live import (
    CredentialMaterialRejected,
    describe_live_run,
    redacted_summary,
    screen_for_credential_material,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BENCHMARKS = f"{API_PREFIX}/benchmarks"
CREDENTIAL = "EXAMPLE_MODEL_KEY"
SECRET = "sk-live-this-would-be-a-real-key"


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
                "LIVE_EVALUATOR_ENABLED": "true",
                "LIVE_EVALUATOR_PROVIDER": "google",
                "LIVE_EVALUATOR_MODEL": "example-model-1",
                "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
                CREDENTIAL: SECRET,
            },
            database_path=tmp_path / "harness.sqlite3",
            target_client=target_client,
        )
        async with harness.router.lifespan_context(harness):
            yield harness


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


def _report(config: dict | None = None) -> bytes:
    document = {
        "config": {
            "reporterSchema": "webmcp-evals/0.0.4",
            "evaluatorVersion": "0.0.4",
            **(config or {}),
        },
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


# --- prohibited place 1: the browser ----------------------------------------


async def test_no_route_accepts_credential_material_from_a_client(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-099: never "through the browser".

    Closed request bodies are what close this: an unknown field is a rejection
    rather than a silently retained one, so a client cannot introduce a
    credential even by guessing a field name.
    """
    # Arrange / Act
    refused = await visitor.post(
        BENCHMARKS,
        json={"source_kind": "recorded_fixture", "api_key": SECRET},
    )

    # Assert
    assert refused.status_code == 422
    assert SECRET not in refused.text


async def test_a_benchmark_response_never_carries_the_credential(
    visitor: httpx.AsyncClient,
) -> None:
    """The value is not in this process to return, and the reproducibility
    metadata a suite exposes is the variable *name* at most."""
    # Arrange
    created = await visitor.post(BENCHMARKS, json={"source_kind": "recorded_fixture"})
    benchmark_id = created.json()["benchmark_id"]

    # Act
    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
    health = await visitor.get("/healthz")

    # Assert
    assert SECRET not in read.text
    assert SECRET not in health.text


# --- prohibited place 2: a WebMCP argument -----------------------------------


def test_no_registered_tool_declares_a_credential_argument() -> None:
    """FR-099: never "through a WebMCP argument".

    Read out of the tool definitions rather than exercised, because the risk is
    a *schema* that accepts one — an agent could then be asked for a key it
    should never hold, and the request would look legitimate.
    """
    # Arrange
    tools = (
        REPO_ROOT
        / "apps"
        / "actionwitness_service"
        / "frontend"
        / "src"
        / "tools"
        / "harnessTools.ts"
    )

    # Act
    source = tools.read_text(encoding="utf-8").lower()

    # Assert
    for forbidden in ("apikey", "api_key", "credential", "secret", "token", "password"):
        assert forbidden not in source, f"a harness tool schema mentions {forbidden!r}"
    # Every published schema stays closed, so an undeclared argument cannot be
    # smuggled past the declared ones either.
    assert "additionalproperties: false" in source


# --- prohibited place 3: a committed file ------------------------------------


def test_no_checked_in_fixture_carries_credential_material() -> None:
    """FR-099: never "through a committed file".

    The fixture is the file most likely to be regenerated from a real run and
    committed without rereading, which is exactly why it is asserted rather
    than trusted.
    """
    # Arrange
    fixtures = REPO_ROOT / "integrations" / "google_evals" / "fixtures"

    # Act
    committed = list(fixtures.glob("*.json"))

    # Assert
    assert committed, "no evaluator fixture is checked in"
    for path in committed:
        text = path.read_text(encoding="utf-8").lower()
        for marker in ("apikey", "api_key", "secret", "credential", "bearer ", "sk-"):
            assert marker not in text, f"{path.name} carries {marker!r}"


def test_the_environment_file_is_not_committed() -> None:
    """`.env` holds the developer's real credential; the constitution and
    AGENTS.md both make it a protected path, and it must never be tracked."""
    # Arrange
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    # Act / Assert
    assert not (REPO_ROOT / ".env").exists() or "/.env" in ignore or ".env" in ignore


# --- prohibited place 4: an uploaded benchmark manifest ----------------------


async def test_an_uploaded_report_carrying_a_credential_is_refused(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-099: never "through an uploaded benchmark manifest".

    Refused rather than redacted. Redaction happens too (FR-090) and would have
    removed the value, but silently removing it would hide that the value
    existed and needs rotating — which is an incident, not a validation
    failure (constitution §7).
    """
    # Arrange
    created = await visitor.post(BENCHMARKS, json={"source_kind": "recorded_fixture"})
    benchmark_id = created.json()["benchmark_id"]

    # Act
    refused = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report({"apiKey": SECRET}),
        headers={"content-type": "application/json"},
    )

    # Assert
    assert refused.status_code == 422
    assert SECRET not in refused.text
    assert "rotate" in refused.text.lower()


async def test_a_report_using_the_configured_variable_name_is_refused(
    visitor: httpx.AsyncClient,
) -> None:
    """The specific check the harness can actually make.

    It knows the *name* of the variable the credential lives in, so a document
    using that name as a key is a credential being carried in — even under a
    spelling no generic list would contain.
    """
    # Arrange
    created = await visitor.post(BENCHMARKS, json={"source_kind": "recorded_fixture"})
    benchmark_id = created.json()["benchmark_id"]

    # Act
    refused = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report({CREDENTIAL: SECRET}),
        headers={"content-type": "application/json"},
    )

    # Assert
    assert refused.status_code == 422


async def test_a_refused_import_persists_nothing(visitor: httpx.AsyncClient) -> None:
    """Screened before any write, so the secret never reaches an artifact.

    A credential in a persisted, hashed artifact could not be removed without
    breaking the chain that references it.
    """
    # Arrange
    created = await visitor.post(BENCHMARKS, json={"source_kind": "recorded_fixture"})
    benchmark_id = created.json()["benchmark_id"]

    # Act
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report({"apiKey": SECRET}),
        headers={"content-type": "application/json"},
    )
    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")

    # Assert
    assert read.json()["counts"]["total_trials"] == 0
    assert SECRET not in read.text


async def test_an_ordinary_report_still_imports(visitor: httpx.AsyncClient) -> None:
    """The screen must not reject ordinary data.

    A check that refused everything would satisfy every test above and make the
    product unusable — and a reader would learn to bypass it.
    """
    # Arrange
    created = await visitor.post(BENCHMARKS, json={"source_kind": "recorded_fixture"})
    benchmark_id = created.json()["benchmark_id"]

    # Act
    imported = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/imports",
        content=_report({"modelName": "example-model-1", "commandMode": "browser"}),
        headers={"content-type": "application/json"},
    )

    # Assert
    assert imported.status_code == 201, imported.text


def test_the_screen_names_what_it_found_without_echoing_the_value() -> None:
    """An incident report has to be actionable and must not itself leak.

    Naming the key tells an operator where to look; repeating the value would
    put the secret into a log line and an error response.
    """
    # Arrange
    settings = ServiceSettings.from_env(
        {
            "HARNESS_ENV": "local",
            "LIVE_EVALUATOR_ENABLED": "true",
            "LIVE_EVALUATOR_PROVIDER": "google",
            "LIVE_EVALUATOR_MODEL": "example-model-1",
            "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
            CREDENTIAL: SECRET,
        }
    )

    # Act / Assert
    with pytest.raises(CredentialMaterialRejected) as rejected:
        screen_for_credential_material({"config": {"apiKey": SECRET}}, settings.live_evaluator)
    assert "apiKey" in str(rejected.value)
    assert SECRET not in str(rejected.value)


def test_the_screen_reaches_nested_documents() -> None:
    """A credential one level down is still a credential."""
    # Arrange
    document = {"results": {"results": [{"meta": {"secret": "x"}}]}}

    # Act / Assert
    with pytest.raises(CredentialMaterialRejected):
        screen_for_credential_material(document, None)


# --- the positive guarantee --------------------------------------------------


def test_the_credential_value_never_enters_the_harness_process() -> None:
    """AC-17: retained "only in the evaluator process environment".

    The settings object holds a variable *name*, and everything the harness
    renders about a live run is derived from that — so there is no path from a
    suite to the value, whatever a caller asks for.
    """
    # Arrange
    settings = ServiceSettings.from_env(
        {
            "HARNESS_ENV": "local",
            "LIVE_EVALUATOR_ENABLED": "true",
            "LIVE_EVALUATOR_PROVIDER": "google",
            "LIVE_EVALUATOR_MODEL": "example-model-1",
            "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
            CREDENTIAL: SECRET,
        }
    )

    # Act
    configuration = describe_live_run(settings.live_evaluator)
    rendered = " ".join(
        [
            repr(settings.live_evaluator),
            repr(configuration),
            str(redacted_summary(configuration)),
            json.dumps(configuration.manifest_fields()),
        ]
    )

    # Assert
    assert SECRET not in rendered
    assert CREDENTIAL in rendered  # the name is useful; the value is absent
