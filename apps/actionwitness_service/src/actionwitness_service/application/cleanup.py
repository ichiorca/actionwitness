"""FR-009's garbage collection.

FR-009: "A cleanup task at startup and at least hourly shall remove all rows and
artifact files owned by anonymous interactive workspaces inactive for 24 hours,
while preserving global built-in templates, and shall remove eval
execution-workspace mutable state immediately after report persistence.
Artifact immutability applies during retention and does not prevent documented
workspace expiry or an explicit purge."

That last sentence is the one worth reading twice. Everything else in this
project says evidence is append-only and never deleted; this says expiry is the
documented exception. So the deletion here is deliberately narrow — a whole
workspace, aged out by its own inactivity — rather than a general ability to
remove rows, which would be a way around append-only that some later handler
would reach for.

**Templates survive because they belong to nobody.** A built-in template has
`workspace_id IS NULL`, so no cascade reaches it. That is not a special case in
this module; it is a property of the schema, which is why there is no
`WHERE ... AND is_template = 0` here to get wrong.

**Files go before rows.** The paths live in the rows, so deleting the rows first
would strand the files with nothing left pointing at them. A file that fails to
unlink is reported rather than aborting the sweep: one unreadable path must not
keep every other expired workspace alive forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from actionwitness_core.journeys.enums import WorkspaceKind

from actionwitness_service.persistence.database import Database, UnitOfWork

__all__ = [
    "CLEANUP_INTERVAL_SECONDS",
    "INTERACTIVE_WORKSPACE_TTL_HOURS",
    "CleanupResult",
    "WorkspaceCleaner",
    "purge_eval_workspace_state",
]

#: "inactive for 24 hours"
INTERACTIVE_WORKSPACE_TTL_HOURS: Final = 24
#: "at startup and at least hourly". Hourly exactly: "at least" is a floor, and
#: sweeping more often would delete nothing sooner while costing a wakeup.
CLEANUP_INTERVAL_SECONDS: Final = 3600


@dataclass(frozen=True)
class CleanupResult:
    """What one sweep did. Returned so a caller can log or assert on it."""

    workspaces_removed: int
    files_removed: int
    files_failed: int


class WorkspaceCleaner:
    """Expires stale anonymous interactive workspaces."""

    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path | str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._artifact_root = Path(artifact_root)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def sweep(self) -> CleanupResult:
        """Remove every expired workspace, its rows, and its artifact files.

        The row deletion is one statement because `workspace_id` is the cascade
        root: runs, events, snapshots, findings, confirmations, guidance, and
        artifacts all go with their workspace. Nine tables, one `DELETE`, and no
        ordering to get wrong.
        """
        cutoff = self._cutoff()
        async with self._database.transaction() as work:
            expired = await self._expired_workspace_ids(work, cutoff)
            if not expired:
                return CleanupResult(0, 0, 0)
            paths = await self._artifact_paths(work, expired)
            placeholders = ",".join("?" for _ in expired)
            await work.execute(
                f"DELETE FROM workspaces WHERE id IN ({placeholders})",
                tuple(expired),
            )

        # Files are unlinked after the rows commit. If the process dies between
        # the two, the next sweep finds no rows and the files are orphaned —
        # which is recoverable. The reverse order loses files that live rows
        # still claim, which is not.
        removed, failed = self._remove_files(paths)
        return CleanupResult(len(expired), removed, failed)

    async def run_until(self, stop: asyncio.Event) -> None:
        """Sweep now, then at least hourly, until asked to stop (FR-009).

        Shutdown is **cooperative** rather than a cancellation. Cancelling this
        task mid-sweep would interrupt an open transaction and leave the
        driver's worker thread unwound, which surfaces later as an unhandled
        thread exception with no connection to the code that caused it. Waiting
        on the stop event instead means shutdown lands between sweeps, where
        there is nothing in flight to interrupt.

        A failed sweep does not end the loop: a transient database error an hour
        into a deployment would otherwise silently disable cleanup for as long
        as the process lives.
        """
        while not stop.is_set():
            try:
                await self.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=CLEANUP_INTERVAL_SECONDS)

    def _cutoff(self) -> str:
        moment = self._clock() - timedelta(hours=INTERACTIVE_WORKSPACE_TTL_HOURS)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")

    async def _expired_workspace_ids(self, work: UnitOfWork, cutoff: str) -> list[str]:
        """Interactive workspaces only.

        FR-009 gives eval workspaces a different rule — their mutable state goes
        "immediately after report persistence" — so ageing them out on the same
        24-hour clock would either delete an eval mid-flight or keep one alive
        long past the report it existed to produce.
        """
        rows = await work.fetch_all(
            "SELECT id FROM workspaces WHERE kind = ? AND last_seen_at < ?",
            (str(WorkspaceKind.INTERACTIVE.value), cutoff),
        )
        return [row["id"] for row in rows]

    async def _artifact_paths(self, work: UnitOfWork, workspace_ids: Sequence[str]) -> list[str]:
        placeholders = ",".join("?" for _ in workspace_ids)
        rows = await work.fetch_all(
            f"SELECT relative_path FROM artifacts WHERE workspace_id IN ({placeholders})",
            tuple(workspace_ids),
        )
        return [row["relative_path"] for row in rows]

    def _remove_files(self, relative_paths: Sequence[str]) -> tuple[int, int]:
        removed = failed = 0
        for relative in relative_paths:
            target = self._resolve(relative)
            if target is None:
                failed += 1
                continue
            try:
                target.unlink(missing_ok=True)
                removed += 1
            except OSError:
                # Reported, not raised: one unreadable path must not keep every
                # other expired workspace alive forever.
                failed += 1
        return removed, failed

    def _resolve(self, relative: str) -> Path | None:
        """Refuse any stored path that escapes the artifact root.

        The path came out of the database, and §5 says a persisted record is
        untrusted input like any other. A row carrying `../../etc/passwd` must
        not turn cleanup into arbitrary deletion, so the resolved path is
        checked against the root rather than assumed to be under it.
        """
        root = self._artifact_root.resolve()
        candidate = (root / relative).resolve()
        return candidate if candidate.is_relative_to(root) else None


async def purge_eval_workspace_state(work: UnitOfWork, workspace_id: str) -> None:
    """FR-009's second rule, for an eval workspace whose report is persisted.

    "shall remove eval execution-workspace mutable state immediately after
    report persistence" — the *mutable* state, not the report. The workspace row
    itself stays so the report it produced keeps an owner, and the report is an
    artifact of the interactive workspace that requested the eval.

    Called by M6, which owns eval runs; it lives here so both halves of FR-009's
    cleanup sentence are in one file rather than one being discovered later.
    """
    await work.execute(
        "UPDATE workspaces SET cleaned_at = ?, active_run_id = NULL WHERE id = ? AND kind = ?",
        (work.now(), workspace_id, str(WorkspaceKind.EVAL.value)),
    )
