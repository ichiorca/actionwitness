"""Serving the stored outcome report (§15.3, §17.2, §23).

The report is read back from the artifact it was sealed into, not recomposed
from the run's rows. Recomposing would produce a document that *looks* like the
verdict while being computed by today's code from today's data — so a report
fetched after a later change could disagree with the one the run actually
produced, and nobody would be able to tell which was the verdict. The artifact
is what was hashed at seal time, and it is what a reader can verify.

**The bytes are verified before they are served.** The constitution is explicit
that evidence is "verified before being trusted" and that a verification failure
"produces an explicit non-pass result; it never degrades to success". A stored
report whose hash no longer matches its row is not a report — returning it with
a warning attached would still put a corrupted verdict in front of a reader who
asked what happened, so it is refused outright.

Verification checks two things, because either alone can be defeated. The
document's §17.2 identity must match the hash recorded in its row, which catches
edited content; and the stored bytes must be the canonical serialization of what
was parsed, which catches an edit that content-hashes the same because the change
was in whitespace or key order. The row is the anchor in both: an embedded
`content_hash` recomputed by whoever edited the file would agree with itself.
"""

from __future__ import annotations

from typing import Any, Final

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.artifacts import (
    OUTCOME_REPORT,
    ArtifactCorrupt,
    ArtifactStore,
)
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.persistence.database import UnitOfWork

__all__ = ["ReportService"]

#: What a caller learns when the stored bytes do not match their recorded hash.
#: Deliberately says nothing about the path, the expected hash, or the run —
#: §15.8 and §20 keep internals out of a response, and this one would otherwise
#: hand a reader the two values needed to forge a replacement.
_CORRUPT: Final = (
    "The stored report failed integrity verification and was not returned. "
    "Treat this run's outcome as unavailable rather than as a pass."
)


class ReportService:
    """Resolves, verifies, and returns a run's sealed outcome report."""

    def __init__(self, work: UnitOfWork, workspace_id: str, artifacts: ArtifactStore) -> None:
        self._work = work
        self._workspace_id = workspace_id
        self._artifacts = artifacts

    async def report(self, run_id: str) -> dict[str, Any]:
        """§15.3's `GET /runs/{run_id}/report`.

        A run with no report is a **409**, not a 404: the run exists and the
        caller may read it, but it has not been verified yet. A 404 would say
        the run was not theirs, which is the answer a foreign run gets, and
        making the two indistinguishable would tell an unverified caller
        nothing about how to proceed.
        """
        run = await WorkspaceScope(self._work, self._workspace_id).run(run_id)

        row = await self._work.fetch_one(
            """
            SELECT content_hash, relative_path, schema_version, created_at
              FROM artifacts
             WHERE workspace_id = ? AND run_id = ? AND artifact_type = ?
             ORDER BY created_at DESC, id DESC
             LIMIT 1
            """,
            (self._workspace_id, run_id, OUTCOME_REPORT),
        )
        if row is None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This run has no outcome report yet. Verify the run first.",
                details=[{"path": "run_id", "message": f"run status is {run['status']}"}],
            )

        return {
            "run_id": str(run["id"]),
            "status": str(run["status"]),
            "schema_version": str(row["schema_version"]),
            "content_hash": str(row["content_hash"]),
            "sealed_at": str(row["created_at"]),
            "report": self._verified(str(row["relative_path"]), str(row["content_hash"])),
        }

    def _verified(self, relative_path: str, expected_hash: str) -> Any:
        """The document, or a refusal — never a document that failed its hash.

        The checks themselves live in `ArtifactStore.verified_document`, which
        is where the bytes and the hash are written, so the rule that decides
        whether stored evidence is still the sealed evidence has one
        implementation rather than one per reader. What stays here is the
        refusal this route sends: `_CORRUPT` deliberately names no path and no
        hash, because a reader who learned both would hold the two values needed
        to forge a replacement.
        """
        try:
            return self._artifacts.verified_document(relative_path, expected_hash)
        except ArtifactCorrupt as corrupt:
            raise ApiError(ApiErrorCode.HARNESS_ERROR, _CORRUPT) from corrupt
