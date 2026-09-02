"""014-T1/T3 — recording the browser's tool surface through the real route.

FR-166 captures a baseline at arming; FR-167 re-captures on every `toolchange`
and records the delta. The tests below are about the three things a capture
endpoint gets wrong.

**Trusting the page.** The browser submits definitions and nothing else — no
hash, no namespace. A client-supplied hash would be the tool surface vouching
for its own integrity, and a client-supplied namespace would let a poisoned
look-alike label itself `harness` and step outside the policy that watches the
target partition (§9.11). Both are asserted by attempting them.

**Losing a quiet capture.** A capture identical to the baseline still appends
`tool_surface_captured`. Without it the timeline cannot distinguish "looked and
saw nothing" from "never looked", and §16.1 turns exactly that distinction into
a failing policy.

**Splitting a capture from its deltas.** They are appended in one transaction,
so a reader can never see a capture whose consequences have not landed yet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_core.evidence.surface import ToolDefinition, ToolSurface
from actionwitness_service.api.app import API_PREFIX, create_app
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"


def descriptor(name: str = "apply_discount", **over: Any) -> dict[str, Any]:
    """One tool as `getTools()` would report it."""
    return {
        "name": name,
        "description": "Apply a discount code to the cart.",
        "read_only_hint": False,
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
        **over,
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


async def _armed_run(visitor: httpx.AsyncClient) -> str:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
    armed = await visitor.post(RUNS)
    assert armed.status_code == 201, armed.text
    return str(armed.json()["run_id"])


async def _capture(visitor: httpx.AsyncClient, run_id: str, tools: list[dict[str, Any]]) -> Any:
    return await visitor.post(f"{RUNS}/{run_id}/tool-surface", json={"tools": tools})


async def _events(visitor: httpx.AsyncClient, run_id: str) -> list[dict[str, Any]]:
    page = await visitor.get(f"{RUNS}/{run_id}/events", params={"limit": 100})
    return list(page.json()["events"])


# --- the baseline (FR-166, criterion 1) --------------------------------------


async def test_the_first_capture_is_the_baseline_and_is_recorded(stack: FastAPI) -> None:
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        response = await _capture(visitor, run_id, [descriptor()])
        events = await _events(visitor, run_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["baseline"] is True
    assert body["deltas"] == []
    assert body["surface_hash"].startswith("sha256:")

    captured = [e for e in events if e["event_type"] == "tool_surface_captured"]
    assert len(captured) == 1


async def test_the_recorded_hash_is_reproducible_from_the_recorded_definitions(
    stack: FastAPI,
) -> None:
    """Exit-gate criterion 1.

    Rebuilt from the *recorded event*, not from the definitions this test sent —
    otherwise it would prove the test can hash, not that the record can be
    checked. A hash nobody can recompute from what was stored is a number, not
    evidence: no reviewer, replay, or later version of this code could verify it.
    """
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        response = await _capture(visitor, run_id, [descriptor(), descriptor("get_cart")])
        events = await _events(visitor, run_id)

    recorded = next(e for e in events if e["event_type"] == "tool_surface_captured")
    payload = recorded["redacted_payload"]

    rebuilt = ToolSurface(
        tools=tuple(ToolDefinition.model_validate(entry) for entry in payload["surface"]["tools"])
    )

    assert rebuilt.content_hash() == payload["surface_hash"]
    assert rebuilt.content_hash() == response.json()["surface_hash"]
    assert payload["tool_count"] == 2


# --- the browser is not trusted ----------------------------------------------


async def test_a_submitted_hash_is_refused_rather_than_believed(stack: FastAPI) -> None:
    """A client-computed identity would be the surface vouching for itself."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        response = await _capture(visitor, run_id, [descriptor(identity_hash="sha256:deadbeef")])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_a_submitted_namespace_is_refused_rather_than_believed(stack: FastAPI) -> None:
    """§9.11: a page that labels its own tools escapes the target partition.

    This is the one that matters. A poisoned look-alike claiming to be a harness
    tool would be outside the policy written to catch it.
    """
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        response = await _capture(visitor, run_id, [descriptor(namespace="harness")])

    assert response.status_code == 422


async def test_an_unknown_tool_lands_in_the_watched_partition(stack: FastAPI) -> None:
    """Failing safe: a name the harness does not publish is a target tool.

    The alternative — treating an unrecognised name as harness — would make a
    tool that appeared from nowhere the one kind of tool nothing watches.
    """
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor()])
        response = await _capture(visitor, run_id, [descriptor(), descriptor("totally_new_tool")])

    kinds = [delta["kind"] for delta in response.json()["deltas"]]
    assert kinds == ["added"]
    assert response.json()["deltas"][0]["namespace"] == "target"


async def test_a_harness_tool_is_not_judged_as_a_target_tool(stack: FastAPI) -> None:
    """§9.11's whole reason: the workspace's own tools come and go by phase."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor(), descriptor("verify_outcome")])
        response = await _capture(visitor, run_id, [descriptor()])

    assert response.json()["deltas"] == [], "a harness tool disappearing is lifecycle, not mutation"


# --- quiet captures and deltas (FR-167, criterion 2) -------------------------


async def test_an_identical_recapture_is_still_recorded(stack: FastAPI) -> None:
    """ "Looked and saw nothing" must not look like "never looked" (§16.1)."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor()])
        second = await _capture(visitor, run_id, [descriptor()])
        events = await _events(visitor, run_id)

    assert second.json()["baseline"] is False
    assert second.json()["deltas"] == []
    assert len([e for e in events if e["event_type"] == "tool_surface_captured"]) == 2
    assert [e for e in events if e["event_type"] == "tool_surface_changed"] == []


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"description": "Apply any discount, no really."}, "description_change"),
        ({"read_only_hint": True}, "hint_change"),
        (
            {"input_schema": {"type": "object", "properties": {"code": {"type": "number"}}}},
            "schema_change",
        ),
    ],
)
async def test_a_mid_run_change_records_its_delta_kind(
    stack: FastAPI, change: dict[str, Any], expected: str
) -> None:
    """FR-167: the per-tool delta kind, recorded as an event."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor()])
        response = await _capture(visitor, run_id, [descriptor(**change)])
        events = await _events(visitor, run_id)

    assert [delta["kind"] for delta in response.json()["deltas"]] == [expected]
    changed = [e for e in events if e["event_type"] == "tool_surface_changed"]
    assert len(changed) == 1


async def test_a_delta_event_carries_both_definitions(stack: FastAPI) -> None:
    """FR-169 wants "a side-by-side diff of the tool definition before and after"."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor()])
        response = await _capture(visitor, run_id, [descriptor(description="Trust me.")])

    (delta,) = response.json()["deltas"]
    assert delta["before"]["description"] != delta["after"]["description"]


async def test_a_reordered_capture_is_not_a_change(stack: FastAPI) -> None:
    """`getTools()` promises no order.

    A surface that hashed differently because the array came back shuffled would
    report a mutation on every capture, and the policy would be worthless within
    a minute of being switched on.
    """
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        first = await _capture(visitor, run_id, [descriptor("a"), descriptor("b")])
        second = await _capture(visitor, run_id, [descriptor("b"), descriptor("a")])

    assert second.json()["deltas"] == []
    assert second.json()["surface_hash"] == first.json()["surface_hash"]


# --- boundaries ---------------------------------------------------------------


async def test_an_oversized_surface_is_refused(stack: FastAPI) -> None:
    """§20.2 bounds a frontend-submitted surface at 100 tools."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        response = await _capture(
            visitor, run_id, [descriptor(f"tool_{index}") for index in range(101)]
        )

    assert response.status_code == 422


async def test_another_workspace_cannot_capture_into_this_run(stack: FastAPI) -> None:
    """A surface record is run evidence, so it takes the same authorization."""
    async with client(stack) as owner:
        run_id = await _armed_run(owner)

    async with client(stack) as stranger:
        await stranger.get(WORKSPACE)
        response = await _capture(stranger, run_id, [descriptor()])

    assert response.status_code == 404, "someone else's run is indistinguishable from a missing one"


# --- FR-169's pre-invocation identity check (014-T5) -------------------------
#
# "Each recorded target-tool invocation shall carry the identity hash of the tool
# definition as observed at invocation time; a mismatch against the armed
# baseline shall be recorded and shall fail the policy even if no `toolchange`
# event was observed."
#
# The refusal matters as much as the record. The agent chose this tool from a
# description that no longer describes it, so dispatching anyway would spend a
# human's consent on something other than what was consented to.


async def _identity_of(visitor: httpx.AsyncClient, run_id: str, tool: str) -> str:
    events = await _events(visitor, run_id)
    captured = next(e for e in events if e["event_type"] == "tool_surface_captured")
    definition = next(
        t for t in captured["redacted_payload"]["surface"]["tools"] if t["name"] == tool
    )
    return ToolDefinition.model_validate(definition).identity().identity_hash


async def _invoke(
    visitor: httpx.AsyncClient, run_id: str, tool: str, arguments: dict[str, Any], **body: Any
) -> Any:
    return await visitor.post(
        f"{RUNS}/{run_id}/target-tools/{tool}:invoke",
        json={"arguments": arguments, **body},
    )


async def test_a_matching_identity_dispatches_normally(stack: FastAPI) -> None:
    """The guard on every refusal below: the honest path must still work."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor("update_cart")])
        presented = await _identity_of(visitor, run_id, "update_cart")

        response = await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": "mug-ceramic-001", "quantity": 1, "request_id": "req_addonemug"},
            tool_identity_hash=presented,
        )

    assert response.status_code == 200, response.text


async def test_a_changed_definition_refuses_the_invocation(stack: FastAPI) -> None:
    """Refused, not merely recorded."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor("update_cart")])

        response = await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": "mug-ceramic-001", "quantity": 1, "request_id": "req_addonemug"},
            tool_identity_hash="sha256:" + "0" * 64,
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TOOL_IDENTITY_MISMATCH"
    assert response.json()["error"]["retryable"] is False, (
        "re-arming is the way forward; retrying the same call cannot help"
    )


async def test_the_refusal_records_its_own_evidence(stack: FastAPI) -> None:
    """A refusal whose evidence did not land would be an accusation with nothing
    behind it — and FR-169 needs the record to fail the policy later."""
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor("update_cart")])
        await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": "mug-ceramic-001", "quantity": 1, "request_id": "req_addonemug"},
            tool_identity_hash="sha256:" + "0" * 64,
        )
        events = await _events(visitor, run_id)

    recorded = [e for e in events if e["event_type"] == "tool_identity_mismatch"]
    assert len(recorded) == 1
    payload = recorded[0]["redacted_payload"]
    assert payload["tool_name"] == "update_cart"
    assert payload["expected_identity_hash"] != payload["presented_identity_hash"]
    assert payload["armed_definition"]["name"] == "update_cart"

    # ...and nothing was dispatched.
    assert [e for e in events if e["event_type"] == "tool_invocation_started"] == []


async def test_an_absent_hash_still_dispatches(stack: FastAPI) -> None:
    """§15.3 makes the field optional.

    A client that cannot compute an identity must still be able to invoke, or
    the check would become a requirement the specification did not impose.
    """
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor("update_cart")])
        response = await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": "mug-ceramic-001", "quantity": 1, "request_id": "req_addonemug"},
        )

    assert response.status_code == 200


async def test_no_baseline_means_no_refusal(stack: FastAPI) -> None:
    """§16.1 already fails `stable_tool_surface` closed for an uncaptured run.

    Refusing every invocation as well would make a browser that never captured
    unable to use the product at all — a second penalty for one condition.
    """
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        response = await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": "mug-ceramic-001", "quantity": 1, "request_id": "req_addonemug"},
            tool_identity_hash="sha256:" + "0" * 64,
        )

    assert response.status_code == 200


async def test_a_tool_absent_from_the_baseline_is_a_delta_not_a_refusal(
    stack: FastAPI,
) -> None:
    """It appeared mid-run, which is an `added` delta for the surface policy.

    Refusing the call the agent is making right now would answer a question
    about the *surface* by blocking an *invocation*.
    """
    async with client(stack) as visitor:
        run_id = await _armed_run(visitor)
        await _capture(visitor, run_id, [descriptor("search_catalog")])
        response = await _invoke(
            visitor,
            run_id,
            "update_cart",
            {"product_id": "mug-ceramic-001", "quantity": 1, "request_id": "req_addonemug"},
            tool_identity_hash="sha256:" + "0" * 64,
        )

    assert response.status_code == 200
