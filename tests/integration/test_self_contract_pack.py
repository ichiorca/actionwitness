"""FR-173 — the built-in contract pack for the `self` target (§12.20).

FR-173 names four invariants the pack must cover "at minimum", and this suite is
the traceability for them:

* arming twice does not create two runs —
  `self_arming_twice_starts_one_run`
* verification cannot complete while a confirmation is pending —
  `self_no_verdict_while_confirmation_pends`
* a completed run's timeline is immutable —
  `self_completed_run_timeline_is_immutable`
* a rejected contract candidate does not enter an armed contract —
  `self_rejected_candidate_stays_out`

`CLAUSES` below is that mapping as data, so a deleted template fails a test that
names the clause it was carrying.

**The path test is the one worth reading twice.** A contract whose assertion
names a path the observation provider never produces is not a check that fails —
it is a check that reports `path_not_found` forever, and a reader skimming the
pack would see four contracts and believe four things were being verified. So
the paths are not compared against a list written here: a *real* observation is
captured through the provider, and every precondition and assertion path in the
pack is resolved against it. A list would be a second copy of the projection and
would agree with itself while disagreeing with the code.

`self` is registered whatever the demo store is doing, so the seeding tests
assert both directions of §21.1's independence: the store's pack does not
suppress this one, and this one does not suppress the store's.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.contracts.paths import ObservationPath, resolve
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.persistence.database import Database
from fastapi import FastAPI
from integrations.self_target.templates import TemplateExpansionError, expand
from integrations.self_target.tools import TOOL_NAMES

from integrations.buggy_store import TEMPLATES as STORE_TEMPLATES
from integrations.self_target import TARGET_ID, TEMPLATES, SelfObservationProvider

#: Marked per test rather than at module scope: the pack's shape is checked
#: synchronously and only the harness journeys need a loop, and pytest-asyncio's
#: strict mode refuses a synchronous test carrying the asyncio mark.
pytestmark = pytest.mark.integration

CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
WORKSPACE = f"{API_PREFIX}/workspace"

#: FR-173's four clauses, in the requirement's own order, against the template
#: that carries each. Written here as the requirement's sentence rather than as
#: a template id alone, so a reader can check the mapping without opening the
#: pack — and so deleting a clause's template fails a test that names it.
CLAUSES: dict[str, str] = {
    "arming twice does not create two runs": "self_arming_twice_starts_one_run",
    "verification cannot complete while a confirmation is pending": (
        "self_no_verdict_while_confirmation_pends"
    ),
    "a completed run's timeline is immutable": "self_completed_run_timeline_is_immutable",
    "a rejected contract candidate does not enter an armed contract": (
        "self_rejected_candidate_stays_out"
    ),
}


def _app(tmp_path: Path, *, store_enabled: bool) -> FastAPI:
    return create_app(
        environ={
            "HARNESS_ENV": "local",
            "BUGGY_STORE_ENABLED": "true" if store_enabled else "false",
            "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        },
        database_path=tmp_path / "harness.sqlite3",
    )


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """A harness with both integrations available.

    The store is on so that "the self pack is seeded" is never confused with
    "the self pack is the only thing seeded" — the two packs have to coexist.
    """
    app = _app(tmp_path, store_enabled=True)
    async with app.router.lifespan_context(app):
        yield app


def visitor(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _new_workspace(app: FastAPI) -> str:
    """A workspace created the way a browser creates one: by asking for it."""
    async with visitor(app) as client:
        response = await client.get(WORKSPACE)
        assert response.status_code == 200, response.text
        return str(response.json()["workspace_id"])


async def _seeded_template_ids(app: FastAPI) -> set[str]:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT source_template_id FROM contracts WHERE workspace_id IS NULL"
        )
    return {str(row["source_template_id"]) for row in rows}


# --- what the pack is -------------------------------------------------------


def test_the_pack_carries_one_contract_for_each_clause() -> None:
    """FR-173's "at minimum" list, and nothing silently missing from it."""
    # Arrange / Act
    published = {template.template_id for template in TEMPLATES}

    # Assert
    assert published == set(CLAUSES.values())


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_every_contract_in_the_pack_parses(template: object) -> None:
    """§17: no document is stored that the core has not validated.

    Seeding parses each template at startup, so a template that stopped parsing
    would take the deployment down rather than fail quietly — this is the same
    check, where a reader can see which template broke.
    """
    # Arrange / Act
    contract = parse_contract(dict(template.document))  # type: ignore[attr-defined]

    # Assert
    assert contract.target_id == TARGET_ID
    assert contract.assertions or contract.policies


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_every_contract_names_only_published_tools(template: object) -> None:
    """§10.2: a contract may not name a tool the adapter does not publish.

    Checked against `tools.TOOL_NAMES` rather than against a list here, because
    the adapter is the authority on its own surface (§9.1) — and because
    `verify_outcome` is deliberately absent from it, so a template that named it
    would be asking the observed workspace to drive the machinery recording it.
    """
    # Arrange
    contract = parse_contract(dict(template.document))  # type: ignore[attr-defined]

    # Act
    named = contract.referenced_tools()

    # Assert
    assert named, "a contract that names no tool asserts nothing about a journey"
    assert named <= set(TOOL_NAMES)


def test_the_pack_takes_no_operator_scalars() -> None:
    """FR-021: a scalar the template does not allowlist is refused, not ignored.

    A caller told their contract was created would otherwise believe they had
    constrained a quantity no term in this pack mentions.
    """
    # Arrange
    template_id = CLAUSES["arming twice does not create two runs"]

    # Act
    verbatim = expand(template_id, {"contract_name": None, "quantity": None})
    with pytest.raises(TemplateExpansionError) as rejected:
        expand(template_id, {"quantity": 3, "discount_code": "SAVE20"})

    # Assert — an empty submission reproduces the template exactly.
    assert dict(verbatim) == dict(TEMPLATES[0].document)
    assert {field for field, _ in rejected.value.details} == {"quantity", "discount_code"}


# --- the paths the pack asserts on ------------------------------------------


@pytest.mark.asyncio
async def test_every_asserted_path_resolves_in_a_real_observation(harness: FastAPI) -> None:
    """Every term in the pack addresses a fact the provider actually produces.

    Resolved against a *captured* observation rather than against a list of
    paths written here: a list would be a second copy of the projection, and it
    would agree with itself long after the projection had moved.
    """
    # Arrange
    observed = await _new_workspace(harness)
    provider: SelfObservationProvider = harness.state.adapters.adapter(
        "self"
    ).observation_provider()
    observation = await provider.capture_observed("recording-workspace", observed)
    context = observation.as_context()

    # Act / Assert
    for template in TEMPLATES:
        contract = parse_contract(dict(template.document))
        for term in (*contract.preconditions, *contract.assertions):
            assert resolve(term.path, context).found, (
                f"{template.template_id} asserts on {term.path}, which the self "
                "observation provider does not produce"
            )

    # And the resolver would have noticed: a path the projection has no fact for
    # does not resolve, so the assertions above are not vacuously true.
    assert not resolve(ObservationPath.parse("target.workspace.cart"), context).found


# --- the pack, end to end ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("template_id", list(CLAUSES.values()))
async def test_each_contract_can_be_selected_and_armed(harness: FastAPI, template_id: str) -> None:
    """The whole path a person takes: instantiate, select, arm.

    Arming is where the pack meets every gate at once — §10.2's target
    validation, FR-024's atomic target selection, FR-030's precondition check
    against the freshly observed workspace, and FR-172's separate observed
    workspace. A template that parsed but could never be armed would be a
    contract nobody could use.
    """
    # Arrange
    async with visitor(harness) as client:
        created = await client.post(CONTRACTS, json={"template_id": template_id})
        assert created.status_code == 201, created.text
        contract_id = str(created.json()["contract_id"])

        # Act
        selected = await client.post(f"{CONTRACTS}/{contract_id}/select")
        assert selected.status_code == 200, selected.text
        armed = await client.post(RUNS)

    # Assert
    assert armed.status_code == 201, armed.text
    body = armed.json()
    assert body["target_id"] == TARGET_ID
    assert body["contract_id"] == contract_id


# --- seeding ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_packs_are_seeded_and_seeding_is_idempotent(tmp_path: Path) -> None:
    """§29.1's startup seeding, over two lifespans against one database."""
    # Arrange
    first = _app(tmp_path, store_enabled=True)
    async with first.router.lifespan_context(first):
        seeded_first = first.state.templates_seeded

    # Act — a second application over the same database file.
    second = _app(tmp_path, store_enabled=True)
    async with second.router.lifespan_context(second):
        seeded_second = second.state.templates_seeded
        stored = await _seeded_template_ids(second)

    # Assert
    assert seeded_first == len(TEMPLATES) + len(STORE_TEMPLATES)
    assert seeded_second == 0
    assert stored == {template.template_id for template in TEMPLATES} | {
        template.template_id for template in STORE_TEMPLATES
    }


@pytest.mark.asyncio
async def test_the_self_pack_is_seeded_without_the_demo_store(tmp_path: Path) -> None:
    """§21.1: one integration being absent never suppresses another.

    The credential-free deployment runs with the demo store off, and the self
    target is registered there like anywhere else — so its pack has to arrive
    rather than being lost with the store's.
    """
    # Arrange
    app = _app(tmp_path, store_enabled=False)

    # Act
    async with app.router.lifespan_context(app):
        stored = await _seeded_template_ids(app)
        seeded = app.state.templates_seeded
        async with visitor(app) as client:
            listed = (await client.get(f"{CONTRACTS}/templates")).json()["templates"]

    # Assert
    assert seeded == len(TEMPLATES)
    assert stored == {template.template_id for template in TEMPLATES}
    # The listing is what a chooser renders, and every self template accepts no
    # scalar — the form for one is a name and nothing else (FR-021).
    assert {item["source_template_id"] for item in listed} == stored
    assert all(item["parameters"] == [] for item in listed)
