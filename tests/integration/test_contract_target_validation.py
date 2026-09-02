"""§10.2/§10.3's target-scoped validation, on the production path.

`OutcomeContract.validate_against_target` has always implemented the rules — the
contract's `target_id` must resolve to the selected adapter, every tool it names
must be one that adapter publishes, and a protected mutation must carry a
confirmation policy. Nothing in the service called it, so a contract naming a
tool that does not exist was accepted, stored, selected and armed, and the
mismatch finally surfaced during verification as a `missing_expected_tool`
finding — which reads as the agent having failed to call a tool, rather than as
the contract having named one the target never published.

These tests pin the two things that fix has to be simultaneously true about.

**A mismatch is refused, at the boundary, as a validation failure.** Not a
finding, not a 500, and not a message a caller has to read a stack trace out of.

**An absent integration is still not a validation failure.** §21.1 requires the
harness to run with the Buggy Store package absent from the environment
entirely. An adapter that is not there publishes no tools, and validating
against an empty surface would turn "this target is not installed" into "every
tool this contract names is invented". The absent case keeps its existing
answer, `TARGET_UNAVAILABLE`, and keeps it at the moment it always came.

The invalid contracts here are inserted directly as global rows rather than
submitted through `POST /contracts`, because that route cannot express one:
FR-021 lets a caller send four flat scalars and every term comes from a trusted
template. A stored contract is how an invalid one actually arrives — seeded by
an older release, or created while a different adapter surface was registered.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_core.contracts.models import ContractRecord, parse_contract
from actionwitness_core.security.canonical import content_hash
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.persistence.database import Database
from fastapi import FastAPI

from integrations.buggy_store import TARGET_ID

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENABLED = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"}
DISABLED = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}
CONTRACTS = f"{API_PREFIX}/contracts"
WORKSPACE = f"{API_PREFIX}/workspace"


def _app(tmp_path: Path, environ: dict[str, str]) -> FastAPI:
    return create_app(environ=environ, database_path=tmp_path / "harness.sqlite3")


@pytest.fixture
async def app(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = _app(tmp_path, ENABLED)
    async with application.router.lifespan_context(application):
        yield application


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


def _contract(**overrides: Any) -> dict[str, Any]:
    """A valid §10 contract for the Buggy Store, before any override.

    Named tools and policies are what each test varies; everything else is
    fixed so a refusal can only be about the thing under test.
    """
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "name": "crafted-contract",
        "target_id": TARGET_ID,
        "intent": "Exercise the target-scoped validation rules.",
        "assertions": [
            {
                "id": "cart-total-exists",
                "path": "target.cart.total",
                "operator": "exists",
                "severity": "critical",
            }
        ],
    }
    document.update(overrides)
    return document


async def _store_global_contract(app: FastAPI, document: Mapping[str, Any]) -> str:
    """Insert one contract that belongs to nobody, so any client may select it.

    Stored through `ContractRecord.of` for the same reason the service does: the
    row has to pass `read`'s integrity check before selection is even reached,
    and a row whose hash disagreed with its document would fail these tests for
    a reason that has nothing to do with target validation.
    """
    database: Database = app.state.database
    async with database.transaction() as work:
        record = ContractRecord.of(
            parse_contract(document), contract_id="ctr_crafted", created_at=work.instant()
        )
        await work.execute(
            """
            INSERT INTO contracts (
                id, workspace_id, source_template_id, content_hash,
                name, schema_version, document_json, created_at
            ) VALUES (?, NULL, NULL, ?, ?, ?, ?, ?)
            """,
            (
                record.contract_id,
                record.content_hash,
                str(record.document.get("name", "")),
                record.schema_version,
                json.dumps(dict(record.document), sort_keys=True),
                work.now(),
            ),
        )
    return record.contract_id


def _error(response: httpx.Response) -> dict[str, Any]:
    return dict(response.json()["error"])


async def _workspace_id(app: FastAPI) -> str:
    """A real, server-issued workspace, because `contracts.workspace_id` is a key.

    Invented here once, the tests below would insert against a foreign key that
    does not resolve and fail for a reason unrelated to what they assert.
    """
    async with client(app) as visitor:
        return str((await visitor.get(WORKSPACE)).json()["workspace_id"])


# --- §10.3: a tool the adapter does not publish ------------------------------


async def test_a_contract_naming_an_unpublished_tool_is_refused_at_selection(
    app: FastAPI,
) -> None:
    """§10.3: every entry must be "a known target-tool name published by the
    adapter".

    Before this ran on the production path the contract selected cleanly, armed,
    and produced a `missing_expected_tool` finding at verification — a sentence
    about the agent's behaviour, describing a defect in the contract.
    """
    # Arrange
    contract_id = await _store_global_contract(
        app,
        _contract(expected_tools={"ordered": False, "calls": ["update_cart", "teleport_cart"]}),
    )

    # Act
    async with client(app) as visitor:
        refused = await visitor.post(f"{CONTRACTS}/{contract_id}/select")
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert — the specification's own code for an invalid contract, and the
    # detail names the tool so the author can see which one does not exist.
    assert refused.status_code == 422, refused.text
    error = _error(refused)
    assert error["code"] == "CONTRACT_VALIDATION_FAILED"
    assert error["retryable"] is False
    assert any("teleport_cart" in detail["message"] for detail in error["details"])
    # Nothing was written: the workspace does not hold a contract it cannot run.
    assert workspace["selected_contract_id"] is None
    assert workspace["selected_target_id"] is None


async def test_the_refusal_carries_no_internal_detail(app: FastAPI) -> None:
    """§15.8: "internal exceptions and stack traces never reach a browser tool".

    The refusal is generated from the contract's own text and the adapter's
    published names, both of which the caller may see. What it must not carry is
    anything about how the harness is built.
    """
    # Arrange
    contract_id = await _store_global_contract(
        app, _contract(expected_tools={"ordered": False, "calls": ["teleport_cart"]})
    )

    # Act
    async with client(app) as visitor:
        refused = await visitor.post(f"{CONTRACTS}/{contract_id}/select")

    # Assert
    text = json.dumps(_error(refused))
    for leak in ("Traceback", "actionwitness_service", "integrations.", "sqlite", 'File \\"'):
        assert leak not in text, f"the refusal leaks {leak!r}"


# --- §10.2: a protected mutation without consent -----------------------------


async def test_a_contract_expecting_a_protected_tool_without_consent_is_refused(
    app: FastAPI,
) -> None:
    """§10.2: "reject destructive policy configurations that omit confirmation
    requirements".

    `proceed_to_checkout` creates an order. A contract that expects it and asks
    for no approval would report the missing confirmation as a policy its author
    never wrote — and would arm a journey whose protected mutation nobody agreed
    to gate.
    """
    # Arrange
    contract_id = await _store_global_contract(
        app,
        _contract(expected_tools={"ordered": False, "calls": ["proceed_to_checkout"]}),
    )

    # Act
    async with client(app) as visitor:
        refused = await visitor.post(f"{CONTRACTS}/{contract_id}/select")

    # Assert
    assert refused.status_code == 422, refused.text
    error = _error(refused)
    assert error["code"] == "CONTRACT_VALIDATION_FAILED"
    assert any("requires_confirmation" in detail["message"] for detail in error["details"])


async def test_the_same_protected_tool_with_consent_selects(app: FastAPI) -> None:
    """The counterfactual: the refusal is about the missing policy, not the tool.

    Without this, a rule that refused every mention of `proceed_to_checkout`
    would pass the test above while making the consent journey unexpressible.
    """
    # Arrange
    contract_id = await _store_global_contract(
        app,
        _contract(
            expected_tools={"ordered": False, "calls": ["proceed_to_checkout"]},
            policies=[
                {
                    "type": "requires_confirmation",
                    "tool": "proceed_to_checkout",
                    "timeout_seconds": 60,
                }
            ],
        ),
    )

    # Act
    async with client(app) as visitor:
        selected = await visitor.post(f"{CONTRACTS}/{contract_id}/select")

    # Assert
    assert selected.status_code == 200, selected.text
    assert selected.json()["selected_target_id"] == TARGET_ID


# --- the regression guard: a valid contract is untouched ---------------------


async def test_a_valid_template_still_instantiates_and_selects(app: FastAPI) -> None:
    """Every shipped template names published tools and gates its protected one.

    The check added to the production path must be invisible to them. Arming is
    covered by `test_contract_instantiation.py`, which owns the fixture that
    stands the demo store up behind the harness.
    """
    # Arrange
    async with client(app) as visitor:
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        canonical = next(
            item for item in templates if item["source_template_id"] == "one_mug_save20_no_checkout"
        )

        # Act
        created = await visitor.post(
            CONTRACTS, json={"template_id": "one_mug_save20_no_checkout", "quantity": 2}
        )
        selected = await visitor.post(f"{CONTRACTS}/{created.json()['contract_id']}/select")

        # Assert
        assert created.status_code == 201, created.text
        assert selected.status_code == 200, selected.text
        assert selected.json()["selected_target_id"] == TARGET_ID

        # And the seeded template itself, which arrives by the other path.
        seeded = await visitor.post(f"{CONTRACTS}/{canonical['contract_id']}/select")
        assert seeded.status_code == 200, seeded.text


# --- §21.1: the harness runs without the integration -------------------------


async def test_a_contract_whose_adapter_is_absent_is_unavailable_not_invalid(
    tmp_path: Path,
) -> None:
    """§21.1 / FR-024, unchanged by the new validation.

    Seeded with the target enabled, then restarted with it absent, so a stored
    contract names a target nothing registers. The answer must still be
    `TARGET_UNAVAILABLE` — an operator who switched an integration off needs to
    be told the target is missing, not that their contract is wrong.
    """
    # Arrange
    seeding = _app(tmp_path, ENABLED)
    async with seeding.router.lifespan_context(seeding):
        pass

    app = _app(tmp_path, DISABLED)
    async with app.router.lifespan_context(app):
        database: Database = app.state.database
        async with database.reading() as work:
            rows = await work.fetch_all(
                "SELECT id FROM contracts WHERE workspace_id IS NULL ORDER BY id"
            )

        # Act
        async with client(app) as visitor:
            refused = await visitor.post(f"{CONTRACTS}/{rows[0]['id']}/select")

    # Assert
    assert refused.status_code == 409, refused.text
    assert _error(refused)["code"] == "TARGET_UNAVAILABLE"


async def test_an_invalid_contract_is_also_unavailable_rather_than_invalid_without_the_adapter(
    tmp_path: Path,
) -> None:
    """The boundary case that decides where the check may run.

    This contract names a tool no adapter publishes, so it is genuinely invalid —
    but with the integration absent there is no published surface to judge it
    against, and calling it invalid would mean the harness had decided the tool
    does not exist from the fact that nothing is installed. The absent target is
    the first and only answer.
    """
    # Arrange
    app = _app(tmp_path, DISABLED)
    async with app.router.lifespan_context(app):
        contract_id = await _store_global_contract(
            app, _contract(expected_tools={"ordered": False, "calls": ["teleport_cart"]})
        )

        # Act
        async with client(app) as visitor:
            refused = await visitor.post(f"{CONTRACTS}/{contract_id}/select")

    # Assert
    assert refused.status_code == 409, refused.text
    assert _error(refused)["code"] == "TARGET_UNAVAILABLE"


async def test_instantiation_refuses_the_mismatch_before_it_is_ever_stored(
    app: FastAPI,
) -> None:
    """The earlier of the two moments, through the service's own entry point.

    `POST /contracts` cannot carry an invalid document — every term comes from a
    trusted template — so the route is exercised by the template tests above and
    the service API is exercised here. What matters is that the contract is
    refused *before* a row exists, so an invalid document never acquires an
    identity a later run could be armed against.
    """
    from actionwitness_core.kernel import ContractError
    from actionwitness_service.application.contract_service import ContractService

    # Arrange
    database: Database = app.state.database
    workspace_id = await _workspace_id(app)
    document = _contract(expected_tools={"ordered": False, "calls": ["teleport_cart"]})

    # Act
    async with database.transaction() as work:
        service = ContractService(work, workspace_id, app.state.adapters)
        with pytest.raises(ContractError) as refused:
            await service.instantiate(document, source_template_id="crafted")

    # Assert — refused, and nothing was written under that workspace.
    assert any("teleport_cart" in detail.message for detail in refused.value.details)
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT id FROM contracts WHERE workspace_id = ?", (workspace_id,)
        )
    assert rows == []


# --- one identity, whichever path stored it ----------------------------------


async def test_a_contract_stored_by_the_service_carries_the_cores_identity(
    app: FastAPI,
) -> None:
    """§17.2 hashes "its validated contract document", so there is one identity.

    The document here is semantically identical to the contract the core builds
    from it but is not written in canonical form — its assertion omits the
    `severity` that defaults to `critical`. Hashing the submission would give it
    a different identity from `OutcomeContract.content_hash()`, and §24.2 step 6
    re-derives the latter: a run armed against the divergent hash produces a
    correct verdict and then cannot generate its regression case, refused with
    "the source contract does not match its stored hash".
    """
    from actionwitness_service.application.contract_service import ContractService

    # Arrange — a document a person could plausibly submit, minus one default.
    document = _contract(
        assertions=[{"id": "cart-total-exists", "path": "target.cart.total", "operator": "exists"}]
    )
    core_identity = parse_contract(document).content_hash()
    # The premise: this really is a document whose raw hash differs, so the
    # assertion below is not vacuously true.
    assert content_hash(dict(document)) != core_identity

    # Act
    database: Database = app.state.database
    workspace_id = await _workspace_id(app)
    async with database.transaction() as work:
        created = await ContractService(work, workspace_id, app.state.adapters).instantiate(
            document, source_template_id="crafted"
        )

    # Assert
    assert created["content_hash"] == core_identity
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT content_hash, document_json FROM contracts WHERE id = ?",
            (created["contract_id"],),
        )
    # Stored document and stored hash describe each other, so `read` serves it
    # and `ContractRecord.verify` holds.
    assert row["content_hash"] == core_identity
    assert content_hash(json.loads(row["document_json"])) == core_identity


async def test_a_non_canonical_template_is_refused_at_seeding(app: FastAPI) -> None:
    """A template's text is published, so it may not be quietly normalised.

    Half of a seeded template's row id is the digest of its document. If the
    stored document were normalised while the shipped one was not, the
    integration's text and the harness's contract would be two different
    documents sharing an identity derived from only one of them. Loud at
    startup instead.
    """
    from dataclasses import dataclass

    from actionwitness_service.api.errors import ApiError
    from actionwitness_service.application.contract_service import seed_templates

    @dataclass(frozen=True)
    class _Template:
        template_id: str
        document: Mapping[str, Any]

    # Arrange — valid, but with the default `severity` left off one assertion.
    template = _Template(
        template_id="not_canonical",
        document=_contract(
            assertions=[
                {"id": "cart-total-exists", "path": "target.cart.total", "operator": "exists"}
            ]
        ),
    )

    # Act / Assert
    database: Database = app.state.database
    async with database.transaction() as work:
        with pytest.raises(ApiError) as refused:
            await seed_templates(work, [template])
    assert refused.value.code == "HARNESS_ERROR"
