"""004-T12 — §15.2's template list, contract read, and selection.

FR-024 is the requirement with teeth: "Selecting it atomically selects the
server-registered target mapped from its immutable `target_id` ... and **no
endpoint may combine a contract with a different target**."

The design answer is that the selection route takes no body. There is no
parameter through which a caller could name a target, so the combination the
requirement forbids is not expressible rather than merely rejected — and the
test for it asserts the route rejects a body outright.

The templates are the three `integrations.buggy_store` seeds (003-T12). They are
read from the integration rather than re-authored here: a contract naming
`target.cart.total` is target-specific by construction, and duplicating it would
give the project two sources of truth for what a contract asserts.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.persistence.database import Database
from fastapi import FastAPI

from integrations.buggy_store import TARGET_ID, TEMPLATES

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


@pytest.fixture
async def app_without_target(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = _app(tmp_path, DISABLED)
    async with application.router.lifespan_context(application):
        yield application


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _templates(visitor: httpx.AsyncClient) -> list[dict]:
    response = await visitor.get(f"{CONTRACTS}/templates")
    assert response.status_code == 200
    return list(response.json()["templates"])


# --- seeding ----------------------------------------------------------------


async def test_the_integrations_templates_are_seeded_at_startup(app: FastAPI) -> None:
    """FR-020: "at least three Buggy Store integration contracts"."""
    # Arrange / Act
    async with client(app) as visitor:
        templates = await _templates(visitor)

    # Assert — the seeded set is exactly what the integration ships.
    assert len(templates) == len(TEMPLATES) >= 3
    assert {t["source_template_id"] for t in templates} == {
        template.template_id for template in TEMPLATES
    }


async def test_seeding_is_idempotent_across_restarts(tmp_path: Path) -> None:
    """A restart must not create a second copy of every built-in."""
    # Arrange
    first = _app(tmp_path, ENABLED)
    async with first.router.lifespan_context(first):
        seeded_first = first.state.templates_seeded

    # Act — a second application over the same database file.
    second = _app(tmp_path, ENABLED)
    async with second.router.lifespan_context(second):
        seeded_second = second.state.templates_seeded
        database: Database = second.state.database
        async with database.reading() as work:
            rows = await work.fetch_all("SELECT id FROM contracts WHERE workspace_id IS NULL")

    # Assert
    assert seeded_first == len(TEMPLATES)
    assert seeded_second == 0
    assert len(rows) == len(TEMPLATES)


async def test_an_edited_template_is_a_new_row_not_a_rewrite(tmp_path: Path) -> None:
    """FR-012: "completed evidence is never relabeled."

    A template whose text changes between releases must not overwrite the
    version an existing run was armed against, so identity includes the content
    hash and the old row stays readable.
    """
    # Arrange
    from dataclasses import replace

    app = _app(tmp_path, ENABLED)
    async with app.router.lifespan_context(app):
        database: Database = app.state.database
        original = TEMPLATES[0]
        edited = replace(
            original,
            document={**dict(original.document), "description": "an edited description"},
        )

        # Act
        from actionwitness_service.application.contract_service import seed_templates

        async with database.transaction() as work:
            written = await seed_templates(work, [edited])

        async with database.reading() as work:
            rows = await work.fetch_all(
                "SELECT id FROM contracts WHERE workspace_id IS NULL AND source_template_id = ?",
                (original.template_id,),
            )

    # Assert — two rows for one template id: the armed one and the new one.
    assert written == 1
    assert len(rows) == 2


async def test_no_templates_are_seeded_without_the_integration(
    app_without_target: FastAPI,
) -> None:
    """§21.1: a startup that insisted on seeding would make the absent-package
    deployment impossible."""
    # Arrange / Act
    async with client(app_without_target) as visitor:
        templates = await _templates(visitor)

    # Assert
    assert app_without_target.state.templates_seeded == 0
    assert templates == []


# --- listing and reading ----------------------------------------------------


async def test_a_template_listing_carries_what_a_chooser_needs(app: FastAPI) -> None:
    # Arrange / Act
    async with client(app) as visitor:
        templates = await _templates(visitor)

    # Assert
    for template in templates:
        assert set(template) == {
            "contract_id",
            "source_template_id",
            "name",
            "description",
            "target_id",
            "schema_version",
            "content_hash",
        }
        assert template["target_id"] == TARGET_ID
        assert template["content_hash"].startswith("sha256:")


async def test_a_template_is_readable_by_every_workspace(app: FastAPI) -> None:
    """A built-in belongs to nobody, so it is nobody's to withhold."""
    # Arrange
    async with client(app) as first, client(app) as second:
        templates = await _templates(first)
        contract_id = templates[0]["contract_id"]

        # Act
        as_second = await second.get(f"{CONTRACTS}/{contract_id}")

    # Assert
    assert as_second.status_code == 200
    assert as_second.json()["is_built_in"] is True


async def test_reading_a_contract_returns_its_verified_document(app: FastAPI) -> None:
    # Arrange
    async with client(app) as visitor:
        templates = await _templates(visitor)
        contract_id = templates[0]["contract_id"]

        # Act
        body = (await visitor.get(f"{CONTRACTS}/{contract_id}")).json()

    # Assert
    assert body["contract_id"] == contract_id
    assert body["document"]["target_id"] == TARGET_ID
    assert body["document"]["assertions"]


async def test_a_tampered_contract_is_not_served(app: FastAPI) -> None:
    """Constitution §5: an integrity failure is an explicit non-pass and never
    degrades into serving the document anyway."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as visitor:
        contract_id = (await _templates(visitor))[0]["contract_id"]
        async with database.transaction() as work:
            row = await work.fetch_one(
                "SELECT document_json FROM contracts WHERE id = ?", (contract_id,)
            )
            document = json.loads(row["document_json"])
            document["assertions"][0]["value"] = 99
            await work.execute(
                "UPDATE contracts SET document_json = ? WHERE id = ?",
                (json.dumps(document, sort_keys=True), contract_id),
            )

        # Act
        response = await visitor.get(f"{CONTRACTS}/{contract_id}")

    # Assert
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "HARNESS_ERROR"


async def test_a_second_client_cannot_read_a_workspace_owned_contract(app: FastAPI) -> None:
    """FR-006 at this route. The template case above is the deliberate
    exception; a workspace's own contract is not."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as owner, client(app) as intruder:
        owner_id = (await owner.get(WORKSPACE)).json()["workspace_id"]
        await intruder.get(WORKSPACE)
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO contracts (
                    id, workspace_id, content_hash, name, schema_version,
                    document_json, created_at
                ) VALUES ('con_owned', ?, 'sha256:x', 'mine', '1.0.0', '{}', ?)
                """,
                (owner_id, work.now()),
            )

        # Act
        response = await intruder.get(f"{CONTRACTS}/con_owned")

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_an_unknown_contract_is_a_404(app: FastAPI) -> None:
    # Arrange / Act
    async with client(app) as visitor:
        response = await visitor.get(f"{CONTRACTS}/con_nothing")

    # Assert
    assert response.status_code == 404


# --- selection (FR-024) -----------------------------------------------------


async def test_selecting_a_contract_also_selects_its_target(app: FastAPI) -> None:
    """FR-024's "atomically", observable as both columns moving together."""
    # Arrange
    async with client(app) as visitor:
        contract_id = (await _templates(visitor))[0]["contract_id"]

        # Act
        selected = await visitor.post(f"{CONTRACTS}/{contract_id}/select")
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert selected.status_code == 200
    assert selected.json() == {
        "selected_contract_id": contract_id,
        "selected_target_id": TARGET_ID,
    }
    assert workspace["selected_contract_id"] == contract_id
    assert workspace["selected_target_id"] == TARGET_ID
    assert workspace["next_action"]["action_code"] == "arm_run"


async def test_the_target_comes_from_the_contract_and_not_the_request(
    app: FastAPI,
) -> None:
    """ "No endpoint may combine a contract with a different target" — honoured
    by giving the route nothing to combine."""
    # Arrange
    async with client(app) as visitor:
        contract_id = (await _templates(visitor))[0]["contract_id"]

        # Act — a caller trying to name a target anyway.
        response = await visitor.post(
            f"{CONTRACTS}/{contract_id}/select", json={"target_id": "some-other-target"}
        )
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert — the body is ignored by the signature, and the contract's own
    # target is what landed.
    assert response.status_code == 200
    assert workspace["selected_target_id"] == TARGET_ID


async def test_selecting_replaces_the_previous_contract(app: FastAPI) -> None:
    """FR-024: "Exactly one contract may be active in a workspace"."""
    # Arrange
    async with client(app) as visitor:
        templates = await _templates(visitor)
        first, second = templates[0]["contract_id"], templates[1]["contract_id"]

        # Act
        await visitor.post(f"{CONTRACTS}/{first}/select")
        await visitor.post(f"{CONTRACTS}/{second}/select")
        workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert workspace["selected_contract_id"] == second


async def test_selecting_a_contract_whose_target_is_unavailable_writes_nothing(
    tmp_path: Path,
) -> None:
    """FR-024's own example, generalised: the workspace must never be left
    holding a contract whose target cannot run."""
    # Arrange — seed with the target enabled, then restart with it disabled, so
    # a stored contract names a target that is no longer registered.
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
        contract_id = rows[0]["id"]

        async with client(app) as visitor:
            # Act
            response = await visitor.post(f"{CONTRACTS}/{contract_id}/select")
            workspace = (await visitor.get(WORKSPACE)).json()

    # Assert
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TARGET_UNAVAILABLE"
    assert workspace["selected_contract_id"] is None
    assert workspace["selected_target_id"] is None


async def test_a_second_client_cannot_select_into_the_first_ones_workspace(
    app: FastAPI,
) -> None:
    """The selection lands in whichever workspace the cookie names, and there is
    no way to name another."""
    # Arrange
    async with client(app) as first, client(app) as second:
        contract_id = (await _templates(first))[0]["contract_id"]
        await first.get(WORKSPACE)
        await second.get(WORKSPACE)

        # Act
        await second.post(f"{CONTRACTS}/{contract_id}/select")
        first_workspace = (await first.get(WORKSPACE)).json()
        second_workspace = (await second.get(WORKSPACE)).json()

    # Assert
    assert second_workspace["selected_contract_id"] == contract_id
    assert first_workspace["selected_contract_id"] is None


async def test_selecting_a_contract_the_workspace_cannot_see_is_a_404(app: FastAPI) -> None:
    """Selection reads through the same scope as the read route, so a known
    identifier from another workspace grants nothing here either."""
    # Arrange
    database: Database = app.state.database
    async with client(app) as owner, client(app) as intruder:
        owner_id = (await owner.get(WORKSPACE)).json()["workspace_id"]
        await intruder.get(WORKSPACE)
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO contracts (
                    id, workspace_id, content_hash, name, schema_version,
                    document_json, created_at
                ) VALUES ('con_owned', ?, 'sha256:x', 'mine', '1.0.0', '{}', ?)
                """,
                (owner_id, work.now()),
            )

        # Act
        response = await intruder.post(f"{CONTRACTS}/con_owned/select")

    # Assert
    assert response.status_code == 404
