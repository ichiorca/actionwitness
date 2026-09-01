"""Anonymous workspace identity and lifecycle (FR-005, FR-006, §20.1).

FR-005: "On first access, FastAPI shall create a random anonymous workspace and
set an HTTP-only, same-site cookie that is `Secure` in production; documented
local HTTP development may omit only the `Secure` attribute."

FR-006: every stateful endpoint "shall resolve the workspace from the
server-issued cookie and reject attempts to access another workspace's ...
[resource], even when its identifier is known."

Two decisions in here are security decisions rather than conveniences.

**A presented identifier is never adopted.** A cookie naming a workspace that
does not exist mints a *fresh* one; the presented value is discarded. Adopting
it would let a client choose its own workspace identifier, and a client that
chooses its own identifier can choose somebody else's. The cookie is a bearer
token for a server-issued name, not a request for one.

**The identifier is cryptographically random and opaque.** §20.1 requires it,
and the reason is that FR-006's whole promise is that knowing an identifier buys
nothing — a guessable one would make that promise depend on nobody trying.

Nothing here holds a transaction across a wait (ADR-0003). Resolution opens a
short unit of work, commits, and returns; the request handler that follows opens
its own.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from actionwitness_core.journeys.enums import WorkspaceKind

from actionwitness_service.persistence.database import Database, UnitOfWork

__all__ = [
    "WORKSPACE_COOKIE_NAME",
    "WORKSPACE_ID_BYTES",
    "ResolvedWorkspace",
    "WorkspaceStore",
    "new_workspace_id",
]

#: Project-allocated. §20.1 requires the cookie but names neither it nor its
#: size; the `__Host-` prefix is deliberately not used because it mandates
#: `Secure`, which FR-005 permits omitting for documented local HTTP.
WORKSPACE_COOKIE_NAME: Final = "actionwitness_workspace"

#: 256 bits. §20.1 says "cryptographically random" without fixing a width; this
#: is the width at which guessing is not a strategy anyone would attempt.
WORKSPACE_ID_BYTES: Final = 32


def new_workspace_id() -> str:
    """A fresh opaque workspace identifier.

    `token_urlsafe` so the value survives a cookie, a URL, and a log line
    unescaped — an identifier that needs encoding somewhere eventually gets
    compared in two different encodings.
    """
    return f"ws_{secrets.token_urlsafe(WORKSPACE_ID_BYTES)}"


@dataclass(frozen=True)
class ResolvedWorkspace:
    """The workspace this request acts in, and whether the client must be told.

    `issued` drives the `Set-Cookie`: re-sending an unchanged cookie on every
    response is noise, and sending none when a workspace was just created would
    make every request the client's first.
    """

    workspace_id: str
    issued: bool


class WorkspaceStore:
    """Creates, resolves, and touches anonymous workspaces."""

    def __init__(
        self,
        database: Database,
        *,
        id_source: Callable[[], str] = new_workspace_id,
    ) -> None:
        self._database = database
        self._id_source = id_source

    async def resolve(self, presented: str | None) -> ResolvedWorkspace:
        """Resolve the cookie's workspace, creating one when it names none.

        An unknown identifier is *not* an error and *not* adopted: the client
        gets a new workspace and a new cookie. A stale cookie from a cleaned-up
        workspace (FR-009) is the common case, and failing it would strand a
        returning visitor rather than starting them over.
        """
        async with self._database.transaction() as work:
            if presented and await self._exists(work, presented):
                await self._touch(work, presented)
                return ResolvedWorkspace(workspace_id=presented, issued=False)

            workspace_id = self._id_source()
            await self._create(work, workspace_id)
            return ResolvedWorkspace(workspace_id=workspace_id, issued=True)

    async def get(self, work: UnitOfWork, workspace_id: str) -> Mapping[str, Any] | None:
        row = await work.fetch_one("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        return None if row is None else dict(row)

    async def create_eval_workspace(self, work: UnitOfWork, owner_workspace_id: str) -> str:
        """An internal eval workspace owned by an interactive one (§17.1).

        Owned rather than free-standing so FR-009's cleanup can remove it with
        its owner instead of ageing it out separately, and so an eval run can
        never outlive the workspace that asked for it.
        """
        workspace_id = self._id_source()
        await self._create(work, workspace_id, kind=WorkspaceKind.EVAL, owner=owner_workspace_id)
        return workspace_id

    async def _exists(self, work: UnitOfWork, workspace_id: str) -> bool:
        row = await work.fetch_one(
            "SELECT 1 AS present FROM workspaces WHERE id = ?", (workspace_id,)
        )
        return row is not None

    async def _touch(self, work: UnitOfWork, workspace_id: str) -> None:
        """Advance `last_seen_at`, which is what FR-009's staleness scan reads."""
        await work.execute(
            "UPDATE workspaces SET last_seen_at = ? WHERE id = ?",
            (work.now(), workspace_id),
        )

    async def _create(
        self,
        work: UnitOfWork,
        workspace_id: str,
        *,
        kind: WorkspaceKind = WorkspaceKind.INTERACTIVE,
        owner: str | None = None,
    ) -> None:
        now = work.now()
        await work.execute(
            """
            INSERT INTO workspaces (id, kind, owner_workspace_id, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (workspace_id, str(kind.value), owner, now, now),
        )
