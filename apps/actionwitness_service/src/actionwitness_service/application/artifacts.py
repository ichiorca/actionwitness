"""Immutable artifact storage (§17.1, §17.2, FR-008, FR-009).

An artifact is a file plus a row. The file holds the document; the row holds its
identity, its size, and the workspace it belongs to — which is what lets FR-008
cap "25 artifacts and 10 MiB of persisted artifact bytes" per workspace and
FR-009's cleanup find the files when a workspace expires.

**The recorded hash is the document's own identity, not a second one.** §17.2
says an artifact hash "covers the complete top-level object except its own
top-level `content_hash` member", so the row records exactly what a document
carrying an embedded `content_hash` already claims. Hashing the whole object
instead would give one report two hashes that both look authoritative — the
stored document would say one thing and the row pointing at it another, with
nothing to tell a reader which was the identity. `document_content_hash` drops
the member; for an artifact type that carries none, it is the plain content
hash.

**The bytes on disk are canonical.** The document is serialized with the core's
canonical serializer (§17.2), and that exact text is written, so a reader can
recompute the identity from the stored file alone.

**The file is written before the row is inserted.** The insert belongs to the
caller's transaction — for an outcome report, the same one that seals the run —
and file I/O must not happen inside it (ADR-0003: nothing is held across a
wait). Crashing between the two leaves a file with no row, which no reader can
reach. The reverse order would leave a row pointing at a file that does not
exist, which a reader *would* reach.

**Paths are content-addressed:** `<workspace>/<run>/<type>-<digest>.json`. The
workspace scoping is what makes FR-009's traversal check meaningful — an
artifact root containing one directory per workspace has an obvious shape, and a
path that climbs out of it is obviously wrong. The digest is what keeps a
committed row honest. Without it the path was a constant per `(workspace, run,
type)`, and a surface that legitimately writes the same type twice — a benchmark
suite accepts repeated imports while it is still `draft` — overwrote the first
file in place while the first `artifacts` row, its `content_hash`, and the trials
referencing it all stayed live. The row then described bytes that were no longer
there. Naming the file after its own hash makes that unrepresentable: different
documents cannot collide, and the same document rewritten is byte-identical.

**Every write is atomic.** Bytes go to a temporary file in the destination
directory, are flushed and `fsync`ed, and are then `os.replace`d over the final
name. Writing the final name directly truncates it first, so a crash mid-write
left a *committed* row pointing at a half-written file — which is
indistinguishable, to the verifier that reads it back, from evidence somebody
tampered with.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from actionwitness_core.kernel import JsonValue
from actionwitness_core.security.canonical import canonical_text, document_content_hash

from actionwitness_service.application.limits import WorkspaceCeilings
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import new_id

__all__ = ["OUTCOME_REPORT", "ArtifactCorrupt", "ArtifactStore", "WrittenArtifact"]


class ArtifactCorrupt(Exception):
    """A stored artifact is not the one that was sealed.

    Deliberately not an `ApiError`: storage does not decide status codes, and
    the two routes that read artifacts back phrase the refusal differently for
    different readers. What they share is that neither serves the document.
    """


#: §17.1's `artifacts.artifact_type`. Project-allocated: the specification names
#: the column but enumerates no vocabulary, and one value is not yet a closed
#: set worth registering. The eval, benchmark, and regression types arrive with
#: the milestones that produce them.
OUTCOME_REPORT: Final = "outcome_report"

#: Identifiers reaching the filesystem are checked rather than trusted. They are
#: server-issued (`ws_…`, `run_…`), so anything else is a bug, and a bug that
#: reached `Path` would be a traversal.
_SAFE_IDENTIFIER: Final = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _write_atomically(target: Path, encoded: bytes) -> None:
    """Write bytes that are either wholly present or wholly absent.

    The temporary file is created in the *destination* directory so `os.replace`
    is a same-filesystem rename rather than a copy — across filesystems it would
    stop being atomic, which is the one property this function exists for. The
    `fsync` before the rename is what makes the atomicity cover the contents and
    not merely the name: without it a crash can leave the new name pointing at
    unflushed blocks. A failed write takes the partial file with it, because the
    whole point is that no reader ever sees one.
    """
    partial: Path | None = None
    try:
        # `delete=False` because the file has to outlive the handle — the rename
        # is what publishes it. Cleanup is the `except` below, and it runs after
        # the handle is closed so the unlink also works on Windows.
        with tempfile.NamedTemporaryFile(
            dir=target.parent, prefix=f".{target.name}.", suffix=".part", delete=False
        ) as handle:
            partial = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, target)
    except BaseException:
        if partial is not None:
            partial.unlink(missing_ok=True)
        raise


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
        digest = document_content_hash(document)
        # Enough of the digest to make a collision infeasible, short enough that
        # the directory stays readable to a person looking for one run's report.
        stamp = digest.removeprefix("sha256:")[:16]
        relative = f"{workspace_id}/{run_id}/{artifact_type}-{stamp}.json"

        target = self._root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(target, encoded)

        return WrittenArtifact(
            relative_path=relative,
            byte_size=len(encoded),
            content_hash=digest,
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
        benchmark_suite_id: str | None = None,
        source_artifact_id: str | None = None,
    ) -> str:
        """Insert the row, inside the caller's transaction.

        `source_artifact_id` is FR-094's derived→source link: a benchmark report
        *references* the evaluator artifact it was computed from and never
        contains or rewrites it. The column carries the reference so the
        relationship is a row anybody can follow, rather than a claim inside a
        document that would have to be trusted.

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
                id, workspace_id, run_id, benchmark_suite_id, source_artifact_id,
                artifact_type, schema_version, content_hash, metadata_json,
                relative_path, byte_size, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                workspace_id,
                run_id,
                benchmark_suite_id,
                source_artifact_id,
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

    async def relative_path(
        self, work: UnitOfWork, workspace_id: str, artifact_id: str
    ) -> str | None:
        """Where one workspace's artifact was stored, or `None` if it is not hers.

        The workspace term is not a convenience: an artifact id is guessable in
        the way every opaque id is, and a lookup without it would let a caller
        who learned an id read another workspace's evidence through the download
        route. `None` rather than a raise, because the caller knows whether a
        missing row is a 404 or an internal inconsistency and this does not.
        """
        found = await self.stored_reference(work, workspace_id, artifact_id)
        return None if found is None else found[0]

    async def stored_reference(
        self, work: UnitOfWork, workspace_id: str, artifact_id: str
    ) -> tuple[str, str] | None:
        """Where the artifact is, and the hash it was recorded with.

        The pair travels together because verifying one against the other is the
        only way to know a stored artifact is still the sealed one, and a caller
        that fetched the path alone would have nothing to check it against. Same
        workspace scoping as `relative_path`, for the same reason: an artifact id
        is guessable, and a lookup without the workspace term would read another
        workspace's evidence.
        """
        row = await work.fetch_one(
            "SELECT relative_path, content_hash FROM artifacts WHERE id = ? AND workspace_id = ?",
            (artifact_id, workspace_id),
        )
        return None if row is None else (str(row["relative_path"]), str(row["content_hash"]))

    def read_bytes(self, relative_path: str) -> bytes:
        """The stored bytes, exactly as written.

        Verification hashes these rather than a re-encoded string: `write`
        hashed the canonical bytes, so anything that decodes and re-encodes on
        the way in has already stopped comparing like with like.
        """
        target = (self._root / relative_path).resolve()
        if not target.is_relative_to(self._root.resolve()):
            raise ValueError("the stored path escapes the artifact root")
        return target.read_bytes()

    def read_text(self, relative_path: str) -> str:
        """The stored text, for a reader that wants to verify the hash itself."""
        return self.read_bytes(relative_path).decode("utf-8")

    def verified_document(self, relative_path: str, expected_hash: str) -> dict[str, Any]:
        """The stored document, or `ArtifactCorrupt` — never an unchecked one.

        Four checks, and each one is a way a stored artifact stops being the
        thing that was sealed: unreadable, undecodable, a different document, or
        the same document in different bytes. The last is the subtle one — a
        file somebody reformatted still hashes its *content* to the recorded
        value, but a reader recomputing from those bytes gets a different answer
        than the writer did, so it is no longer evidence anybody can check.

        Raises rather than returning a sentinel, and raises a plain exception
        rather than an `ApiError`: this module owns storage, not HTTP status
        codes, and each caller states its own refusal (constitution §5 — an
        integrity failure is an explicit non-pass and never degrades into
        serving the document anyway).
        """
        try:
            stored = self.read_bytes(relative_path)
        except (OSError, ValueError) as missing:
            # A row without its file is the crash window this module documents
            # in reverse; unreadable evidence either way.
            raise ArtifactCorrupt("the stored artifact could not be read") from missing

        try:
            text = stored.decode("utf-8")
            document = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError) as unreadable:
            raise ArtifactCorrupt("the stored artifact is not readable JSON") from unreadable

        if not isinstance(document, dict):
            raise ArtifactCorrupt("the stored artifact is not a document")
        if document_content_hash(document) != expected_hash:
            raise ArtifactCorrupt("the stored artifact does not match its recorded hash")
        if canonical_text(document) != text:
            raise ArtifactCorrupt("the stored artifact is no longer the bytes that were sealed")
        return document


def _require_safe(identifier: str, what: str) -> None:
    if not _SAFE_IDENTIFIER.match(identifier):
        raise ValueError(f"{what} identifier {identifier!r} is not safe as a path segment")
