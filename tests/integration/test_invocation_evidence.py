"""005-T4 — the evidence one invocation leaves behind (FR-032, §20.3, §23.3).

FR-032 lists what a terminal invocation records: "redacted inputs, bounded
output summary, `reported_status`, duration, correlation ID, and canonical
`state_version_before` and `state_version_after`", plus, for mutations,
"redacted canonical state hashes and bounded before/after values for their
declared target-effect paths so idempotency and false-success evidence do not
depend on tool-return text or later actions."

The last clause is what the effect-path tests below are for. `apply_discount`
under the fault reports success and moves nothing, and the recorded evidence for
`target.cart.total` has to say so **from the observation**, without reading the
tool's summary and without waiting for verification — by which time other
actions may have moved the same path.

§20.3's ordering is the other property: redaction happens "before persistence,
hashing, or export". A payload redacted after storage would already have been
written in the clear once, and its hash would describe a document nobody kept.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_core.security.canonical import content_hash
from actionwitness_core.security.redaction import REDACTED
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ENV = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "true"}
WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[FastAPI]:
    store = create_store(database_path=tmp_path / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ=ENV,
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


async def _arm(visitor: httpx.AsyncClient) -> str:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    armed = await visitor.post(RUNS)
    assert armed.status_code == 201, armed.text
    return str(armed.json()["run_id"])


async def _put_the_store_in_pre_fix(app: FastAPI, workspace_id: str) -> None:
    """Arm the discount fault in the store itself.

    Set directly rather than through the harness (which can do this since
    005-T10), so a break in scenario selection fails its own tests rather than
    these.
    """
    adapter = app.state.adapters.adapter("buggy_store")
    response = await adapter._client.post(
        "/demo/api/v1/store/scenario",
        headers={"X-Workspace-Id": workspace_id},
        json={
            "scenario_mode": "pre_fix",
            "fault_profile": "discount_reported_but_not_applied",
        },
    )
    assert response.status_code < 400, response.text


def _invoke(run_id: str, tool: str) -> str:
    return f"{RUNS}/{run_id}/target-tools/{tool}:invoke"


async def _event(app: FastAPI, run_id: str, event_type: str, tool: str) -> dict:
    database: Database = app.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT * FROM events WHERE run_id = ? AND event_type = ? AND tool_name = ? "
            "ORDER BY sequence_number",
            (run_id, event_type, tool),
        )
    assert rows, f"no {event_type} event for {tool}"
    return dict(rows[-1])


# --- FR-032's field list ----------------------------------------------------


async def test_a_completion_records_every_field_fr_032_names(stack: FastAPI) -> None:
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await visitor.post(
            _invoke(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_evidence1"}},
        )

    # Assert
    event = await _event(stack, run_id, "tool_invocation_completed", "update_cart")
    assert event["reported_status"] == "success"
    assert event["correlation_id"]
    assert event["request_id"]
    assert event["duration_ms"] is not None
    assert event["state_version_before"] is not None
    assert event["state_version_after"] is not None
    assert event["state_hash_before"].startswith("sha256:")
    assert event["state_hash_after"].startswith("sha256:")


async def test_the_start_event_records_the_redacted_inputs(stack: FastAPI) -> None:
    """The arguments belong on the start event: they are what the call was made
    with, and they are known before it returns."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await visitor.post(
            _invoke(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 3, "request_id": "req_inputs01"}},
        )

    # Assert
    event = await _event(stack, run_id, "tool_invocation_started", "update_cart")
    payload = json.loads(event["redacted_payload_json"])
    assert payload["arguments"] == {
        "product_id": MUG,
        "quantity": 3,
        "request_id": "req_inputs01",
    }


async def test_schema_defaults_appear_in_the_recorded_inputs(stack: FastAPI) -> None:
    """The values that reached the target are the values the timeline recorded —
    which is why defaults are applied before dispatch rather than inside the
    adapter."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await visitor.post(_invoke(run_id, "search_catalog"), json={"arguments": {"query": "mug"}})

    # Assert
    event = await _event(stack, run_id, "tool_invocation_started", "search_catalog")
    payload = json.loads(event["redacted_payload_json"])
    assert payload["arguments"]["query"] == "mug"
    assert "max_results" in payload["arguments"]


async def test_the_reported_summary_is_bounded(stack: FastAPI) -> None:
    """§23.3 keeps the tool's own text out of storage at full length."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await visitor.post(_invoke(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    event = await _event(stack, run_id, "tool_invocation_completed", "get_cart")
    payload = json.loads(event["redacted_payload_json"])
    from actionwitness_core.security.limits import MAX_TOOL_RESULT_CHARS

    assert len(payload["reported"]["summary"]) <= MAX_TOOL_RESULT_CHARS + 20


# --- declared target-effect evidence ----------------------------------------


async def test_a_mutation_records_before_and_after_for_its_declared_paths(
    stack: FastAPI,
) -> None:
    """§13.4's declared prefixes for `update_cart`, resolved either side."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await visitor.post(
            _invoke(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 2, "request_id": "req_effects1"}},
        )

    # Assert
    event = await _event(stack, run_id, "tool_invocation_completed", "update_cart")
    effects = json.loads(event["redacted_payload_json"])["effects"]
    assert set(effects) == {
        "target.cart.items",
        "target.cart.subtotal",
        "target.cart.total",
    }
    assert effects["target.cart.total"]["changed"] is True
    assert effects["target.cart.total"]["before"] != effects["target.cart.total"]["after"]


async def test_the_fault_leaves_its_declared_paths_visibly_unmoved(
    stack: FastAPI,
) -> None:
    """FR-032's purpose, stated as a test.

    `apply_discount` reports success and changes nothing. The evidence says so
    from the *observation*, without reading the tool's summary and without
    waiting for verification.
    """
    # Arrange
    async with client(stack) as visitor:
        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        await _put_the_store_in_pre_fix(stack, workspace_id)
        run_id = await _arm(visitor)
        await visitor.post(
            _invoke(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_seedcart"}},
        )

        # Act
        await visitor.post(
            _invoke(run_id, "apply_discount"), json={"arguments": {"code": "SAVE20"}}
        )

    # Assert
    event = await _event(stack, run_id, "tool_invocation_completed", "apply_discount")
    payload = json.loads(event["redacted_payload_json"])
    assert payload["reported"]["status"] == "success"

    effects = payload["effects"]
    # §13.4 declares these two for `apply_discount`.
    assert set(effects) == {"target.cart.discount", "target.cart.total"}
    assert effects["target.cart.discount"]["changed"] is False
    assert effects["target.cart.total"]["changed"] is False
    # And the evidence is self-contained: the values are here, so a reader does
    # not have to re-observe or trust the summary.
    assert effects["target.cart.total"]["before"] == effects["target.cart.total"]["after"]


async def test_a_read_only_tool_records_no_effect_evidence(stack: FastAPI) -> None:
    """§13.4 lists a read-only tool's effects as none, and §12.2 forbids the
    harness inferring any."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)
        await visitor.post(_invoke(run_id, "get_cart"), json={"arguments": {}})

    # Assert
    event = await _event(stack, run_id, "tool_invocation_completed", "get_cart")
    assert json.loads(event["redacted_payload_json"])["effects"] == {}


async def test_effect_evidence_is_unknown_when_the_observation_failed(
    stack: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unobservable target cannot report that a path stayed still."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

        from integrations.buggy_store import BuggyStoreObservationProvider

        original = BuggyStoreObservationProvider.capture
        seen = {"n": 0}

        async def flaky(self, workspace_id):  # type: ignore[no-untyped-def]
            seen["n"] += 1
            if seen["n"] > 1:
                raise RuntimeError("unreachable")
            return await original(self, workspace_id)

        monkeypatch.setattr(BuggyStoreObservationProvider, "capture", flaky)

        # Act
        await visitor.post(
            _invoke(run_id, "update_cart"),
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_blindcall"}},
        )

    # Assert
    event = await _event(stack, run_id, "tool_invocation_completed", "update_cart")
    effects = json.loads(event["redacted_payload_json"])["effects"]
    for declared in effects.values():
        assert declared["changed"] is None


# --- §20.3's ordering -------------------------------------------------------


async def test_the_stored_snapshot_hash_describes_the_stored_payload(
    stack: FastAPI,
) -> None:
    """Redaction happens before hashing, so the hash describes what was kept.

    Hashing first and redacting second would leave a stored hash that no reader
    could reproduce from the stored document.
    """
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _arm(visitor)

    # Assert
    database: Database = stack.state.database
    async with database.reading() as work:
        snapshot = await work.fetch_one(
            "SELECT content_hash, redacted_state_json FROM snapshots WHERE run_id = ?",
            (run_id,),
        )
    assert snapshot["content_hash"] == content_hash(json.loads(snapshot["redacted_state_json"]))


async def test_a_contracts_redaction_paths_reach_the_stored_evidence(
    stack: FastAPI,
) -> None:
    """§20.3: contract paths apply "in addition to defaults", and the policy
    comes from the contract the run was armed against (FR-025)."""
    # Arrange — a contract that redacts the store's delivery note.
    database: Database = stack.state.database
    async with client(stack) as visitor:
        templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
        source = next(t for t in templates if t["source_template_id"] == CANONICAL)
        document = (await visitor.get(f"{CONTRACTS}/{source['contract_id']}")).json()["document"]
        document["redaction"] = {"paths": ["**.delivery_note"]}

        workspace_id = (await visitor.get(WORKSPACE)).json()["workspace_id"]
        async with database.transaction() as work:
            await work.execute(
                """
                INSERT INTO contracts (
                    id, workspace_id, content_hash, name, schema_version,
                    document_json, created_at
                ) VALUES ('con_redacting', ?, ?, 'redacting', '1.0', ?, ?)
                """,
                (
                    workspace_id,
                    content_hash(document),
                    json.dumps(document, sort_keys=True),
                    work.now(),
                ),
            )
        assert (await visitor.post(f"{CONTRACTS}/con_redacting/select")).status_code == 200

        # Act — armed directly, since `_arm` would re-select the canonical
        # template and overwrite the selection under test.
        armed = await visitor.post(RUNS)
        assert armed.status_code == 201, armed.text
        run_id = str(armed.json()["run_id"])

    # Assert — the note is redacted in the stored baseline.
    async with database.reading() as work:
        snapshot = await work.fetch_one(
            "SELECT redacted_state_json FROM snapshots WHERE run_id = ?", (run_id,)
        )
    payload = json.loads(snapshot["redacted_state_json"])
    assert payload["preferences"]["delivery_note"] == REDACTED


async def test_the_default_keys_are_never_switched_off(stack: FastAPI) -> None:
    """A contract can widen redaction and never narrow it (§20.3)."""
    # Arrange
    from actionwitness_core.security.redaction import RedactionPolicy, redact

    policy = RedactionPolicy.from_paths(["**.delivery_note"])

    # Act
    result = redact({"email": "a@b.test", "delivery_note": "x"}, policy)

    # Assert
    assert result == {"email": REDACTED, "delivery_note": REDACTED}
