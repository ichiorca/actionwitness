"""005-T12 — paged events and the report endpoint (§15.3, §17.2, §23).

Two reads that a client polls a live run with, and one it fetches afterwards.

The property worth stating up front is the one a paging test usually misses:
**`has_more: false` does not mean the run is over.** Events keep arriving, so a
client that stopped polling there would silently lose the rest of the timeline.
`test_the_page_does_not_claim_the_timeline_ended` is the test that pins that
down, and it is the reason `run_status` travels with every page.

The report is read back from the artifact it was sealed into and hash-verified
before it is served. `test_a_tampered_report_is_refused_rather_than_returned`
corrupts the stored bytes on purpose: the constitution says a verification
failure "never degrades to success", and a report is exactly the response where
degrading would be least visible — a reader who asked what happened would be
shown something that looks like an answer.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.persistence.database import Database
from buggy_store.api import create_app as create_store
from fastapi import FastAPI

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"
CANONICAL = "one_mug_save20_no_checkout"
MUG = "mug-ceramic-001"
FAULT = "discount_reported_but_not_applied"


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


async def _select_contract(visitor: httpx.AsyncClient) -> None:
    templates = (await visitor.get(f"{CONTRACTS}/templates")).json()["templates"]
    chosen = next(t for t in templates if t["source_template_id"] == CANONICAL)
    assert (await visitor.post(f"{CONTRACTS}/{chosen['contract_id']}/select")).status_code == 200


async def _scenario(visitor: httpx.AsyncClient, mode: str = "pre_fix") -> None:
    assert (
        await visitor.put(f"{WORKSPACE}/failure-profile", json={"failure_profile": FAULT})
    ).status_code == 200
    assert (
        await visitor.put(f"{WORKSPACE}/scenario-mode", json={"scenario_mode": mode})
    ).status_code == 200


_JOURNEY: tuple[tuple[str, dict], ...] = (
    ("search_catalog", {"query": "mug"}),
    ("update_cart", {"product_id": MUG, "quantity": 1, "request_id": "req_addonemug"}),
    ("apply_discount", {"code": "SAVE20"}),
)


async def _armed(visitor: httpx.AsyncClient) -> str:
    await _select_contract(visitor)
    await _scenario(visitor)
    armed = await visitor.post(RUNS)
    assert armed.status_code == 201, armed.text
    return str(armed.json()["run_id"])


async def _drive(visitor: httpx.AsyncClient, run_id: str) -> None:
    for tool, arguments in _JOURNEY:
        response = await visitor.post(
            f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": arguments}
        )
        assert response.status_code == 200, response.text


async def _verified_run(visitor: httpx.AsyncClient) -> str:
    run_id = await _armed(visitor)
    await _drive(visitor, run_id)
    assert (await visitor.post(f"{RUNS}/{run_id}/verify")).status_code == 200
    return run_id


# --- paging ------------------------------------------------------------------


async def test_the_whole_timeline_is_reachable_one_page_at_a_time(stack: FastAPI) -> None:
    """§15.3's cursor, walked to the end.

    Pages of two, so the walk takes several round trips and a bug in the cursor
    arithmetic shows up as a repeated or skipped event rather than passing by
    luck on a single page.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)

        # Act — walk the cursor exactly as a polling client would.
        collected: list[dict] = []
        cursor = 0
        for _ in range(100):  # a bound, so a broken cursor fails instead of hanging
            page = (
                await visitor.get(
                    f"{RUNS}/{run_id}/events",
                    params={"after_sequence": cursor, "limit": 2},
                )
            ).json()
            collected.extend(page["events"])
            cursor = page["next_after_sequence"]
            if not page["has_more"]:
                break

        # Assert — against the single-page read of the same run.
        everything = (await visitor.get(f"{RUNS}/{run_id}/events", params={"limit": 100})).json()

    sequences = [event["sequence_number"] for event in collected]
    assert sequences == sorted(sequences), "events must arrive in sequence order"
    assert len(sequences) == len(set(sequences)), "no event may be delivered twice"
    assert sequences == [event["sequence_number"] for event in everything["events"]]
    assert sequences[0] == 1, "sequence numbers are dense from one (ADR-0003)"


async def test_a_cursor_past_the_end_returns_an_empty_page_not_an_error(
    stack: FastAPI,
) -> None:
    """A polling client that has caught up asks again. That is the normal case,
    not a failure — and the cursor it gets back must be the one it sent, or it
    would rewind and redeliver the whole timeline."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)

        # Act
        response = await visitor.get(f"{RUNS}/{run_id}/events", params={"after_sequence": 9999})

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert body["events"] == []
    assert body["has_more"] is False
    assert body["next_after_sequence"] == 9999


async def test_the_page_does_not_claim_the_timeline_ended(stack: FastAPI) -> None:
    """`has_more: false` means "no more *right now*", not "the run is over".

    The distinction is invisible on a finished run, so this one polls to
    exhaustion mid-run, appends more events by invoking another tool, and polls
    again. A client that treated the first `has_more: false` as the end would
    have stopped before the run's most important events.
    """
    # Arrange — drive one tool, then drain the timeline.
    async with client(stack) as visitor:
        run_id = await _armed(visitor)
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/search_catalog:invoke",
            json={"arguments": {"query": "mug"}},
        )
        drained = (await visitor.get(f"{RUNS}/{run_id}/events", params={"limit": 100})).json()
        assert drained["has_more"] is False
        assert drained["run_status"] == "running", "the run has not finished"

        # Act — the run continues.
        await visitor.post(
            f"{RUNS}/{run_id}/target-tools/update_cart:invoke",
            json={"arguments": {"product_id": MUG, "quantity": 1, "request_id": "req_more"}},
        )
        resumed = (
            await visitor.get(
                f"{RUNS}/{run_id}/events",
                params={"after_sequence": drained["next_after_sequence"], "limit": 100},
            )
        ).json()

    # Assert
    assert resumed["events"], "events appended after the page said `has_more: false`"
    assert resumed["events"][0]["sequence_number"] == drained["next_after_sequence"] + 1


async def test_a_page_carries_the_run_status_that_ends_polling(stack: FastAPI) -> None:
    """The counterpart: what a client *should* stop on."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)
        page = (await visitor.get(f"{RUNS}/{run_id}/events")).json()

    # Assert
    assert page["run_id"] == run_id
    assert page["run_status"] not in {"running", "verifying"}


@pytest.mark.parametrize("limit", [0, -1, 101, 1000])
async def test_a_limit_outside_the_specified_bound_is_refused(stack: FastAPI, limit: int) -> None:
    """§15.3 fixes `limit={1..100}`.

    Refused rather than clamped: a client that asked for 500, silently received
    100, and was told nothing would read the short page as the end of the
    timeline.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed(visitor)

        # Act
        response = await visitor.get(f"{RUNS}/{run_id}/events", params={"limit": limit})

    # Assert
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "CONTRACT_VALIDATION_FAILED"
    assert any("limit" in detail["path"] for detail in body["details"])


async def test_a_negative_cursor_is_refused(stack: FastAPI) -> None:
    """A negative `after_sequence` would silently mean "from the beginning"."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed(visitor)

        # Act
        response = await visitor.get(f"{RUNS}/{run_id}/events", params={"after_sequence": -5})

    # Assert
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


async def test_a_rejected_query_uses_the_one_error_envelope(stack: FastAPI) -> None:
    """§15.8 defines one error shape. FastAPI's default `{"detail": [...]}` is a
    second one, and a client written against §15.8 would not parse it."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed(visitor)

        # Act
        body = (await visitor.get(f"{RUNS}/{run_id}/events", params={"limit": 500})).json()

    # Assert
    assert "detail" not in body, "FastAPI's own envelope must not reach a client"
    assert set(body["error"]) >= {"code", "message", "retryable", "details"}
    assert body["error"]["retryable"] is False


async def test_events_from_another_workspace_are_not_readable(stack: FastAPI) -> None:
    """AC-11 needs two clients. A single-client test proves the route works, not
    that a second client is locked out.

    Events carry no `workspace_id` of their own, so this is the test that the
    indirection through the run actually holds.
    """
    # Arrange — one workspace's run...
    async with client(stack) as owner:
        run_id = await _verified_run(owner)

    # Act — ...read by a different cookie.
    async with client(stack) as stranger:
        await stranger.get(WORKSPACE)  # take a workspace of its own
        response = await stranger.get(f"{RUNS}/{run_id}/events")

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_the_page_publishes_only_the_named_fields(stack: FastAPI) -> None:
    """The projection is explicit so a column added later is not exported the
    day it is added — §20.3 puts redaction before export."""
    # Arrange / Act
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)
        page = (await visitor.get(f"{RUNS}/{run_id}/events", params={"limit": 100})).json()

    # Assert
    published = {key for event in page["events"] for key in event}
    assert "redacted_payload_json" not in published, "the raw stored column is not the API"
    assert "run_id" not in published, "already on the page; not repeated per event"
    assert {"sequence_number", "event_type", "actor", "redacted_payload"} <= published


# --- the report --------------------------------------------------------------


async def test_the_sealed_report_is_served_with_its_hash(stack: FastAPI) -> None:
    """§15.3's JSON report, and §17.2's verifiable identity for it."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)
        verified = (await visitor.post(f"{RUNS}/{run_id}/verify")).status_code

        # Act
        response = await visitor.get(f"{RUNS}/{run_id}/report")

    # Assert — a second verify is refused, so the report below is the sealed one.
    assert verified == 409
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["content_hash"].startswith("sha256:")
    assert body["report"]["layers"]["business_outcome"] == "failed"
    assert body["report"]["layers"]["tool_execution"] == "passed"


async def test_the_served_report_is_the_document_the_run_sealed(stack: FastAPI) -> None:
    """Read back from the artifact, not recomposed.

    A recomposed report would be computed by today's code from today's rows, so
    it could disagree with the verdict the run actually produced and nobody
    could tell which was authoritative. This pins the served document to the
    hash the verification response returned.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed(visitor)
        await _drive(visitor, run_id)
        sealed = (await visitor.post(f"{RUNS}/{run_id}/verify")).json()

        # Act
        served = (await visitor.get(f"{RUNS}/{run_id}/report")).json()

    # Assert
    assert served["content_hash"] == sealed["report_content_hash"]
    assert served["report"]["layers"] == sealed["layers"]
    assert served["report"]["counts"] == sealed["counts"]


async def test_an_unverified_run_has_no_report_yet(stack: FastAPI) -> None:
    """409, not 404. The run exists and is the caller's; it simply has no
    outcome. A 404 is what a *foreign* run gets, and conflating the two would
    leave a caller unable to tell "not yours" from "not yet"."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _armed(visitor)

        # Act
        response = await visitor.get(f"{RUNS}/{run_id}/report")

    # Assert
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "PRECONDITION_FAILED"
    assert "verify" in body["message"].lower()


async def test_a_report_from_another_workspace_is_not_readable(stack: FastAPI) -> None:
    """The second half of AC-11 for this endpoint, with two clients."""
    # Arrange
    async with client(stack) as owner:
        run_id = await _verified_run(owner)

    # Act
    async with client(stack) as stranger:
        await stranger.get(WORKSPACE)
        response = await stranger.get(f"{RUNS}/{run_id}/report")

    # Assert
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_a_tampered_report_is_refused_rather_than_returned(
    stack: FastAPI, tmp_path: Path
) -> None:
    """The constitution: evidence is verified before it is trusted, and a
    verification failure "never degrades to success".

    A corrupted report is the response where degrading would be least visible —
    it still looks like an answer to "what happened?". So the bytes are edited
    on disk in a way that keeps them valid JSON: a parser would accept this
    document happily, and only the hash catches it.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)
        assert (await visitor.get(f"{RUNS}/{run_id}/report")).status_code == 200

        # Act — flip the verdict on disk.
        stored = next((tmp_path / "artifacts").rglob("outcome_report.json"))
        document = json.loads(stored.read_text(encoding="utf-8"))
        document["layers"]["business_outcome"] = "passed"
        stored.write_text(json.dumps(document), encoding="utf-8")

        response = await visitor.get(f"{RUNS}/{run_id}/report")

    # Assert
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "HARNESS_ERROR"
    assert "passed" not in json.dumps(body), "the forged verdict must not be echoed"


async def test_a_report_whose_file_vanished_is_refused(stack: FastAPI, tmp_path: Path) -> None:
    """A row without its file is unreadable evidence, which is the same explicit
    non-pass as corrupted evidence — not an empty 200."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)

        # Act
        next((tmp_path / "artifacts").rglob("outcome_report.json")).unlink()
        response = await visitor.get(f"{RUNS}/{run_id}/report")

    # Assert
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "HARNESS_ERROR"


async def test_the_refusal_names_no_path_or_hash(stack: FastAPI, tmp_path: Path) -> None:
    """§20: a corruption refusal must not hand a reader the two values needed to
    forge a replacement, nor a filesystem path."""
    # Arrange
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)
        stored = next((tmp_path / "artifacts").rglob("outcome_report.json"))
        expected = (await visitor.get(f"{RUNS}/{run_id}/report")).json()["content_hash"]
        stored.write_text('{"tampered": true}', encoding="utf-8")

        # Act
        body = (await visitor.get(f"{RUNS}/{run_id}/report")).text

    # Assert
    assert expected not in body
    assert "outcome_report.json" not in body
    assert str(tmp_path) not in body
    assert "artifacts" not in body


async def test_a_rewritten_but_equivalent_file_is_still_refused(
    stack: FastAPI, tmp_path: Path
) -> None:
    """The second of the two checks, which the content hash alone cannot make.

    This rewrites the report with identical content and different bytes —
    indented, keys reordered. Its §17.2 identity is unchanged, so a
    hash-only check passes it. But the sealed artifact is the *bytes*, and a
    reader recomputing the hash from a file that is no longer canonical would
    get a different answer than the writer did.
    """
    # Arrange
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)
        stored = next((tmp_path / "artifacts").rglob("outcome_report.json"))
        document = json.loads(stored.read_text(encoding="utf-8"))

        # Act — same document, different serialization.
        rewritten = json.dumps(dict(reversed(list(document.items()))), indent=2, ensure_ascii=False)
        assert json.loads(rewritten) == document, "content is genuinely unchanged"
        stored.write_text(rewritten, encoding="utf-8")

        response = await visitor.get(f"{RUNS}/{run_id}/report")

    # Assert
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "HARNESS_ERROR"


async def test_the_row_records_the_documents_own_identity(stack: FastAPI) -> None:
    """§17.2: an artifact hash "covers the complete top-level object except its
    own top-level `content_hash` member".

    The row and the document it points at must therefore agree. They did not:
    the row hashed the whole stored object, embedded hash included, giving one
    report two hashes that both looked authoritative — with nothing to tell a
    reader which was its identity.
    """
    # Arrange
    database: Database = stack.state.database
    async with client(stack) as visitor:
        run_id = await _verified_run(visitor)
        served = (await visitor.get(f"{RUNS}/{run_id}/report")).json()

    # Assert
    async with database.reading() as work:
        row = await work.fetch_one("SELECT content_hash FROM artifacts WHERE run_id = ?", (run_id,))
    assert row is not None
    assert row["content_hash"] == served["report"]["content_hash"]
    assert row["content_hash"] == served["content_hash"]
