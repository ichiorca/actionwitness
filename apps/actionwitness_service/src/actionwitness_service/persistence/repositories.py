"""Repository implementations of the core's `ports` protocols.

Spec v1.9 §17.1 (the tables and their immutability rules), §16.1 (events are
append-only; a correction before verification completes is "a new event rather
than mutation of an existing event"), FR-034 (monotonic sequence within a run),
FR-043 (a snapshot is immutable from creation), FR-006 (a known identifier from
another workspace grants nothing); ADR-0003 (sequence allocation inside the
appending transaction, with the unique constraint as the backstop).

Each repository is **constructed from a `UnitOfWork`** rather than opening one.
That is ADR-0003's "one owner per transaction" expressed in types: the
application service opens the transaction and builds the repositories onto it,
and a repository never sees a connection it could start a second transaction on.
It also lets the method signatures match `actionwitness_core.ports` exactly, so
`isinstance(repo, ContractRepository)` is a real check rather than a hopeful one.

**There is no update or delete method on an insert-only table**, and that absence
is the design. The core's protocols declare none, §17.1 says of snapshots that
"the repository exposes no update method for this table", and
`tests/adapters/test_ports.py` fails if one appears. A correction is a new row,
not an edit — which is what makes a hash-linked chain worth verifying.

Workspace scoping lives in the `WHERE` clause, not in a caller that remembers to
filter afterwards. FR-006 refuses another workspace's resource "even when its
identifier is known", and the dependable way to honour that is for the query to
be unable to select the row at all.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from actionwitness_core.contracts.models import ContractRecord
from actionwitness_core.journeys.enums import SnapshotPhase
from actionwitness_core.ports.models import Observation

from actionwitness_service.persistence.database import UnitOfWork

__all__ = [
    "ContractRepository",
    "EventRepository",
    "FindingRepository",
    "SnapshotRepository",
    "new_id",
]


def new_id(prefix: str) -> str:
    """A random row identifier.

    Row identity is not replayed evidence — a snapshot's identity is its
    `(run_id, phase)` and an event's is its `(run_id, sequence_number)`, both of
    which are deterministic. Anything a fixture must reproduce takes its
    identifier from the core's injected `IdentifierSource` instead.
    """
    return f"{prefix}_{uuid.uuid4().hex}"


class ContractRepository:
    """Insert-only contract storage (§17.1: "there is no update operation").

    Bound at construction to the workspace whose contracts it may write. A
    `workspace_id` of `None` writes a *global built-in template*, which is what
    lets FR-009's cleanup delete a workspace's own contracts while "preserving
    global built-in templates": the template belongs to nobody, so no cascade
    reaches it.
    """

    def __init__(self, work: UnitOfWork, workspace_id: str | None = None) -> None:
        self._work = work
        self._workspace_id = workspace_id

    async def add(self, record: ContractRecord) -> None:
        """Store one immutable contract under this repository's workspace."""
        document = dict(record.document)
        await self._work.execute(
            """
            INSERT INTO contracts (
                id, workspace_id, source_template_id, content_hash,
                name, schema_version, document_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.contract_id,
                self._workspace_id,
                _optional_str(document.get("source_template_id")),
                record.content_hash,
                str(document.get("name", "")),
                record.schema_version,
                json.dumps(document, sort_keys=True),
                self._work.now(),
            ),
        )

    async def get(self, workspace_id: str, contract_id: str) -> ContractRecord | None:
        """One contract visible to `workspace_id`, or `None`.

        Visible means owned by that workspace *or* a global template. A contract
        owned by a different workspace matches neither arm, so a client that
        learned the identifier elsewhere selects nothing and the route above
        turns that into a 404 rather than a 403 — a 403 would confirm that the
        identifier names something real (FR-006, §20.1).
        """
        row = await self._work.fetch_one(
            """
            SELECT id, content_hash, schema_version, document_json, created_at
              FROM contracts
             WHERE id = ? AND (workspace_id = ? OR workspace_id IS NULL)
            """,
            (contract_id, workspace_id),
        )
        return None if row is None else _contract_of(row)

    async def list_templates(self) -> list[ContractRecord]:
        """Every global built-in template, in a stable order (§15.2)."""
        rows = await self._work.fetch_all(
            """
            SELECT id, content_hash, schema_version, document_json, created_at
              FROM contracts
             WHERE workspace_id IS NULL
             ORDER BY created_at, id
            """
        )
        return [_contract_of(row) for row in rows]


class EventRepository:
    """Append-only run timeline with monotonic sequencing (§16.1, FR-034)."""

    def __init__(self, work: UnitOfWork) -> None:
        self._work = work

    async def append(self, run_id: str, event: Mapping[str, object]) -> int:
        """Append one event and return the sequence number it was given.

        ADR-0003: allocation is `MAX(sequence_number) + 1` scoped to the run,
        computed inside the same `BEGIN IMMEDIATE` transaction that inserts the
        row. The write lock is already held, so no second appender can read the
        same maximum. The unique constraint on `(run_id, sequence_number)` is
        therefore a backstop, not the mechanism: if it ever fires, this
        transaction was opened wrongly, and the failure surfaces rather than
        being retried into a duplicate event.
        """
        row = await self._work.fetch_one(
            "SELECT COALESCE(MAX(sequence_number), 0) AS highest FROM events WHERE run_id = ?",
            (run_id,),
        )
        sequence = (int(row["highest"]) if row else 0) + 1

        await self._work.execute(
            """
            INSERT INTO events (
                id, run_id, sequence_number, event_type, actor,
                annotated_sequence_number, tool_identity_hash, tool_name,
                correlation_id, request_id, redacted_payload_json, status,
                reported_status, state_version_before, state_version_after,
                state_hash_before, state_hash_after, duration_ms, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("evt"),
                run_id,
                sequence,
                str(event["event_type"]),
                str(event["actor"]),
                event.get("annotated_sequence_number"),
                _optional_str(event.get("tool_identity_hash")),
                _optional_str(event.get("tool_name")),
                _optional_str(event.get("correlation_id")),
                _optional_str(event.get("request_id")),
                json.dumps(dict(event.get("redacted_payload") or {}), sort_keys=True),  # type: ignore[arg-type]
                _optional_str(event.get("status")),
                _optional_str(event.get("reported_status")),
                _optional_str(event.get("state_version_before")),
                _optional_str(event.get("state_version_after")),
                _optional_str(event.get("state_hash_before")),
                _optional_str(event.get("state_hash_after")),
                event.get("duration_ms"),
                self._work.now(),
            ),
        )
        return sequence

    async def list_after(
        self, run_id: str, after_sequence: int, limit: int
    ) -> Sequence[Mapping[str, object]]:
        """§15.3's paged read: the events after a sequence number, in order.

        No ceiling is applied here. FR-008 caps a run at 250 events, so the page
        is bounded by the domain rather than by a number this layer invents, and
        a repository that quietly returned fewer rows than asked would make a
        polling client believe it had reached the end.
        """
        if limit < 1:
            raise ValueError("a page must ask for at least one event")
        rows = await self._work.fetch_all(
            """
            SELECT * FROM events
             WHERE run_id = ? AND sequence_number > ?
             ORDER BY sequence_number
             LIMIT ?
            """,
            (run_id, after_sequence, limit),
        )
        return [_event_of(row) for row in rows]

    async def count(self, run_id: str) -> int:
        """How many events this run holds. FR-008's per-run ceiling reads this."""
        row = await self._work.fetch_one(
            "SELECT COUNT(*) AS total FROM events WHERE run_id = ?", (run_id,)
        )
        return int(row["total"]) if row else 0


class SnapshotRepository:
    """Insert-only snapshot storage (§17.1, FR-043)."""

    def __init__(self, work: UnitOfWork) -> None:
        self._work = work

    async def add(self, run_id: str, phase: SnapshotPhase, observation: Observation) -> None:
        """Store one authoritative observation against a run phase.

        The payload must arrive already redacted. §20.3 requires redaction
        "before persistence, hashing, or export", and `Observation.content_hash`
        hashes whatever payload it was built with — so a caller that redacted
        afterwards would store a hash describing a document nobody kept.
        """
        await self._work.execute(
            """
            INSERT INTO snapshots (
                id, run_id, phase, provider, namespace, provenance,
                schema_version, state_version, content_hash,
                redacted_state_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id("snap"),
                run_id,
                str(phase.value),
                observation.provider_id,
                observation.namespace,
                observation.provenance,
                observation.schema_version,
                observation.state_version,
                observation.content_hash(),
                json.dumps(dict(observation.payload), sort_keys=True),
                self._work.now(),
            ),
        )

    async def get(self, run_id: str, phase: SnapshotPhase) -> Observation | None:
        """Rebuild the stored observation, verifying its hash on the way out.

        Constitution §4: persisted JSON is "validated on both write and read".
        A row whose payload no longer hashes to its stored `content_hash` has
        been altered outside the append-only path, so this refuses to hand it
        back as an observation rather than letting a tampered document settle a
        verdict (§17.2, FR-042).
        """
        row = await self._work.fetch_one(
            """
            SELECT phase, provider, namespace, provenance, schema_version,
                   state_version, content_hash, redacted_state_json, created_at
              FROM snapshots WHERE run_id = ? AND phase = ?
            """,
            (run_id, str(phase.value)),
        )
        if row is None:
            return None

        observation = Observation(
            namespace=row["namespace"],
            provider_id=row["provider"],
            provenance=row["provenance"],
            schema_version=row["schema_version"],
            payload=json.loads(row["redacted_state_json"]),
            state_version=row["state_version"],
            captured_at=_instant(row["created_at"]),
        )
        if observation.content_hash() != row["content_hash"]:
            raise SnapshotIntegrityError(run_id, phase)
        return observation


class FindingRepository:
    """Insert-only findings for one terminal run (§17.1)."""

    def __init__(self, work: UnitOfWork) -> None:
        self._work = work

    async def add_all(self, run_id: str, findings: Sequence[Mapping[str, object]]) -> None:
        for finding in findings:
            await self._work.execute(
                """
                INSERT INTO findings (
                    id, run_id, check_id, check_type, classification, severity,
                    status, path, paths_json, applied_exemptions_json,
                    attributed_cause_json, expected_json, actual_json, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("find"),
                    run_id,
                    str(finding["check_id"]),
                    str(finding["check_type"]),
                    _optional_str(finding.get("classification")),
                    str(finding["severity"]),
                    str(finding["status"]),
                    _optional_str(finding.get("path")),
                    _json_or_none(finding.get("paths")),
                    _json_or_none(finding.get("applied_exemptions")),
                    _json_or_none(finding.get("attributed_cause")),
                    json.dumps(finding.get("expected"), sort_keys=True, default=str),
                    json.dumps(finding.get("actual"), sort_keys=True, default=str),
                    json.dumps(dict(finding.get("evidence") or {}), sort_keys=True, default=str),  # type: ignore[arg-type]
                ),
            )

    async def list_for_run(self, run_id: str) -> Sequence[Mapping[str, object]]:
        rows = await self._work.fetch_all(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY check_id, id", (run_id,)
        )
        return [dict(row) for row in rows]


class SnapshotIntegrityError(RuntimeError):
    """A stored snapshot no longer hashes to its recorded content hash.

    Constitution §5: "evidence-chain verification failure ... produces an
    explicit non-pass result; it never degrades to success." Raising is that
    explicitness — returning the payload anyway, or `None`, would let a
    tampered row read as merely absent.
    """

    def __init__(self, run_id: str, phase: SnapshotPhase) -> None:
        super().__init__(
            f"the stored snapshot for run {run_id} phase {phase.value} does not match "
            "its recorded content hash"
        )
        self.run_id = run_id
        self.phase = phase


# --- row mapping ------------------------------------------------------------


def _contract_of(row: Any) -> ContractRecord:
    """Rebuild a validated record from its stored row.

    Validated on read as well as on write (constitution §4). A hand-edited row
    fails here rather than flowing onward as a contract nobody authored.
    """
    return ContractRecord(
        contract_id=row["id"],
        schema_version=row["schema_version"],
        content_hash=row["content_hash"],
        document=json.loads(row["document_json"]),
        created_at=_instant(row["created_at"]),
    )


def _event_of(row: Any) -> dict[str, object]:
    event = dict(row)
    event["redacted_payload"] = json.loads(event.pop("redacted_payload_json") or "{}")
    return event


def _instant(stored: str) -> datetime:
    """Parse a stored ISO-8601 UTC instant back into an aware `datetime`.

    The core's `UtcInstant` is a `BeforeValidator`, not a coercion: it refuses a
    string outright rather than guessing at a format. Parsing is therefore this
    boundary's job, which is where it belongs — SQLite has no datetime type, so
    something has to decide what the text meant, and the core deliberately
    declines to.
    """
    return datetime.fromisoformat(stored.replace("Z", "+00:00")).astimezone(UTC)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _json_or_none(value: object) -> str | None:
    """Store a collection as JSON, or `None` when there is none.

    §17.1 distinguishes a finding that concerns one path (`path` set,
    `paths_json` null) from one that lists several, so an empty collection has
    to round-trip as null rather than as `[]` — otherwise every single-path
    finding would also look like an empty multi-path one.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        return json.dumps(dict(value), sort_keys=True, default=str)
    if isinstance(value, str):
        return json.dumps([value])
    items = [str(item) for item in value]  # type: ignore[union-attr]
    return json.dumps(items) if items else None
