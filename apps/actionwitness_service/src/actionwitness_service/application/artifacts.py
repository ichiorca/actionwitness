"""Immutable artifact storage (§17.1, §17.2, FR-008, FR-009).

An artifact is a file plus a row. The file holds the document; the row holds its
identity, its size, and the workspace it belongs to — which is what lets FR-008
cap "25 artifacts and 10 MiB of persisted artifact bytes" per workspace and
FR-009's cleanup find the files when a workspace expires.

**The bytes on disk are the bytes that were hashed.** The document is serialized
with the core's canonical serializer (§17.2), and that exact text is both hashed
and written. Writing pretty-printed JSON beside a hash taken over canonical text
would produce a stored artifact whose own hash a reader could not reproduce,
which is the failure `as_stored_document` exists to prevent.

**The file is written before the row is inserted.** The insert belongs to the
caller's transaction — for an outcome report, the same one that seals the run —
and file I/O must not happen inside it (ADR-0003: nothing is held across a
wait). Crashing between the two leaves a file with no row, which the next write
overwrites and which no reader can reach. The reverse order would leave a row
pointing at a file that does not exist, which a reader *would* reach.

Paths are workspace-scoped: `<workspace>/<run>/<type>.json`. That is what makes
FR-009's traversal check meaningful — an artifact root containing one directory
per workspace has an obvious shape, and a path that climbs out of it is
obviously wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from actionwitness_core.kernel import JsonValue
from actionwitness_core.security.canonical import canonical_text, content_hash

from actionwitness_service.application.limits import WorkspaceCeilings
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import new_id

__all__ = ["OUTCOME_REPORT", "ArtifactStore", "WrittenArtifact"]

#: §17.1's `artifacts.artifact_type`. Project-allocated: the specification names
#: the column but enumerates no vocabulary, and one value is not yet a closed
#: set worth registering. The eval, benchmark, and regression types arrive with
#: the milestones that produce them.
OUTCOME_REPORT: Final = "outcome_report"

#: Identifiers reaching the filesystem are checked rather than trusted. They are
#: server-issued (`ws_…`, `run_…`), so anything else is a bug, and a bug that
#: reached `Path` would be a traversal.
_SAFE_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class WrittenArtifact:
    """A file on disk, and everything the row will need to describe it."""

    relative_path: str
    byte_size: int
    content_hash: str
    artifact_type: str
    schema_version: str


class ArtifactStore:
    """Writes artifact files and records them against a workspace."""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def write(
        self,
        workspace_id: str,
        run_id: str,
        document: dict[str, JsonValue],
        *,
        artifact_type: str,
        schema_version: str,
    ) -> WrittenArtifact:
        """Serialize canonically, hash the text, and write those exact bytes."""
        _require_safe(workspace_id, "workspace")
        _require_safe(run_id, "run")

        text = canonical_text(document)
        encoded = text.encode("utf-8")
        relative = f"{workspace_id}/{run_id}/{artifact_type}.json"

        target = self._root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)

        return WrittenArtifact(
            relative_path=relative,
            byte_size=len(encoded),
            content_hash=content_hash(document),
            artifact_type=artifact_type,
            schema_version=schema_version,
        )

    async def record(
        self,
        work: UnitOfWork,
        workspace_id: str,
        run_id: str | None,
        written: WrittenArtifact,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Insert the row, inside the caller's transaction.

        FR-008's ceilings are checked here rather than before the write, because
        the count and the insert have to be the same transaction — a guard that
        ran earlier would count rows a concurrent creation had not yet
        committed. The file may therefore exist for an artifact the ceiling
        refuses; it is unreachable without a row, and the next write of the same
        artifact replaces it.
        """
        import json

        await WorkspaceCeilings(work, workspace_id).guard_new_artifact(written.byte_size)

        artifact_id = new_id("art")
        await work.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, run_id, artifact_type, schema_version,
                content_hash, metadata_json, relative_path, byte_size, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                workspace_id,
                run_id,
                written.artifact_type,
                written.schema_version,
                written.content_hash,
                json.dumps(metadata or {}, sort_keys=True),
                written.relative_path,
                written.byte_size,
                work.now(),
            ),
        )
        return artifact_id

    def read_text(self, relative_path: str) -> str:
        """The stored text, for a reader that wants to verify the hash itself."""
        target = (self._root / relative_path).resolve()
        if not target.is_relative_to(self._root.resolve()):
            raise ValueError("the stored path escapes the artifact root")
        return target.read_text(encoding="utf-8")


def _require_safe(identifier: str, what: str) -> None:
    if not _SAFE_IDENTIFIER.match(identifier):
        raise ValueError(f"{what} identifier {identifier!r} is not safe as a path segment")
