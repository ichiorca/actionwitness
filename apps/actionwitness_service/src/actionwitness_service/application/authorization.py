"""Workspace-scoped resource resolution (FR-006, §20.1).

FR-006: "Every harness-UI stateful endpoint shall resolve the workspace from the
server-issued cookie and reject attempts to access another workspace's run,
eval, benchmark, trial, Shopify pairing, or artifact, **even when its identifier
is known**."

That last clause is the whole design. It is not enough for a handler to fetch a
row and then compare `row["workspace_id"]` to the caller's — that pattern works
until one handler forgets the comparison, and the forgetting is invisible in
review because the code that fetches looks complete. So the workspace goes into
the `WHERE` clause: a resource belonging to somebody else does not resolve, and
there is no fetched row for a handler to mishandle.

**The refusal is 404, not 403.** A 403 would tell the caller that the identifier
names something real — which is the one fact FR-006 is protecting. An identifier
from another workspace and an identifier that never existed are reported
identically, because to this caller they are the same thing: nothing they may
see. The distinction survives only in server-side logs.

Contracts are the single exception to workspace ownership, and it is a
deliberate one: a row with `workspace_id IS NULL` is a global built-in template
(§17.1, FR-009), visible everywhere and owned by nobody.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.persistence.database import UnitOfWork

__all__ = ["WorkspaceScope", "not_found"]

#: Every message a caller may receive for a resource they cannot see. One
#: string, so that "yours but missing" and "someone else's" cannot be told apart
#: by their wording — which is how this kind of leak usually survives review.
_NOT_FOUND_MESSAGE: Final = "No such resource in this workspace."


def not_found() -> ApiError:
    """The single refusal for anything the caller may not see."""
    return ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, _NOT_FOUND_MESSAGE)


class WorkspaceScope:
    """Reads bound to one workspace.

    Constructed per request from the cookie-resolved workspace, never from a
    body, a path parameter, or a header: §20.1 and FR-006 both make the
    server-issued cookie the only authorization input, and a scope that could be
    built from client-supplied data would be a scope a client could widen.
    """

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    async def run(self, run_id: str) -> Mapping[str, Any]:
        return await self._owned("runs", run_id)

    async def confirmation(self, confirmation_id: str) -> Mapping[str, Any]:
        return await self._owned("confirmation_requests", confirmation_id)

    async def artifact(self, artifact_id: str) -> Mapping[str, Any]:
        return await self._owned("artifacts", artifact_id)

    async def contract(self, contract_id: str) -> Mapping[str, Any]:
        """A contract this workspace owns, or a global built-in template.

        The `workspace_id IS NULL` arm is the only place in this class where a
        row the caller does not own resolves, and it is safe precisely because
        such a row is owned by nobody: there is no other workspace whose state
        it could reveal.
        """
        row = await self._work.fetch_one(
            """
            SELECT * FROM contracts
             WHERE id = ? AND (workspace_id = ? OR workspace_id IS NULL)
            """,
            (contract_id, self._workspace_id),
        )
        if row is None:
            raise not_found()
        return dict(row)

    async def find_run(self, run_id: str) -> Mapping[str, Any] | None:
        """`run` without the refusal, for callers deciding rather than serving."""
        return await self._optional("runs", run_id)

    async def _owned(self, table: str, resource_id: str) -> Mapping[str, Any]:
        row = await self._optional(table, resource_id)
        if row is None:
            raise not_found()
        return row

    async def _optional(self, table: str, resource_id: str) -> Mapping[str, Any] | None:
        # `table` is never caller-supplied: every call site passes a literal
        # from this module, because SQLite cannot bind an identifier and a
        # table name reaching here from a request would be an injection point.
        if table not in _SCOPED_TABLES:
            raise ValueError(f"{table!r} is not a workspace-scoped table")
        row = await self._work.fetch_one(
            f"SELECT * FROM {table} WHERE id = ? AND workspace_id = ?",
            (resource_id, self._workspace_id),
        )
        return None if row is None else dict(row)


#: The tables carrying a non-nullable `workspace_id`. `contracts` is absent
#: because its ownership is nullable and it has its own accessor above; `events`
#: is absent because it has no `workspace_id` at all — an event is reached
#: through its run, so scoping it means scoping the run first. That indirection
#: is deliberate: it makes "which workspace owns this event?" a question with
#: exactly one answer.
_SCOPED_TABLES: Final = frozenset({"runs", "confirmation_requests", "artifacts", "guidance_events"})
