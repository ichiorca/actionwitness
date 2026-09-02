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

Those two decisions together are why FR-009's "stricter limit of 10 workspace
creations per hour" is charged *here*, through `charge_creation`, rather than by
the caller guessing beforehand. Because an unknown identifier is neither adopted
nor refused, "the request carried no cookie" and "the request will create a
workspace" are different questions, and only this method knows the answer to the
second one. A caller that asked the first question instead would let anyone mint
unlimited workspaces by inventing a cookie value per request.

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
    "CreationCharge",
    "CreationRefused",
    "Resolution",
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


@dataclass(frozen=True)
class CreationRefused:
    """Resolution stopped at FR-009's creation allowance, before any row existed.

    Returned rather than raised. A refusal raised from here would travel out
    through middleware, which sits outside the application's exception handlers,
    and would reach the client as a bare 500 instead of §15.8's envelope.

    `retry_after_seconds` is carried out of the limiter rather than recomputed
    by the caller, so the `Retry-After` a refused creation gets is the same
    number, from the same bucket, that a refused request gets.
    """

    retry_after_seconds: int


#: What a resolution attempt produced: a workspace, or a refusal to create one.
type Resolution = ResolvedWorkspace | CreationRefused

#: Spend one workspace creation from FR-009's stricter bucket. `None` when the
#: creation is permitted; otherwise the whole seconds the client must wait.
#:
#: A callable rather than a `RateLimiter` argument so this module keeps no
#: opinion about how the allowance is counted, or about HTTP: the limiter, the
#: client key, and the 429 all stay on the API side of the boundary.
type CreationCharge = Callable[[], int | None]


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

    async def resolve(
        self, presented: str | None, *, charge_creation: CreationCharge | None = None
    ) -> Resolution:
        """Resolve the cookie's workspace, creating one when it names none.

        An unknown identifier is *not* an error and *not* adopted: the client
        gets a new workspace and a new cookie. A stale cookie from a cleaned-up
        workspace (FR-009) is the common case, and failing it would strand a
        returning visitor rather than starting them over.

        `charge_creation` is spent on exactly the branch that creates, because
        no caller can predict that branch from the request alone: a cookie
        naming a workspace that never existed — or that FR-009's cleanup has
        removed — mints one just as surely as no cookie at all, and only the
        lookup below tells them apart. It is spent *after* that check, so a
        returning visitor is never charged, and *before* the `INSERT`, so a
        refused creation leaves no row behind — the refusal returns from inside
        the transaction having written nothing.

        The charge is spent slightly early on purpose: a creation whose `INSERT`
        then fails has still cost a token. Over-counting a failed creation errs
        toward limiting more than necessary, which is the safe direction; the
        alternative — charging only after a successful write — would let a
        client that reliably provokes a write failure create load for free.

        Passing no charge leaves the previous behaviour exactly: every
        resolution creates when it must, and nothing is metered.
        """
        async with self._database.transaction() as work:
            if presented and await self._exists(work, presented):
                await self._touch(work, presented)
                return ResolvedWorkspace(workspace_id=presented, issued=False)

            if charge_creation is not None:
                retry_after_seconds = charge_creation()
                if retry_after_seconds is not None:
                    return CreationRefused(retry_after_seconds=retry_after_seconds)

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
