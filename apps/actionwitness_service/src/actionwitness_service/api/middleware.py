"""The anonymous-workspace cookie middleware (FR-005, §20.1).

§20.1: "Use a cryptographically random anonymous workspace cookie with `Secure`,
`HttpOnly`, and `SameSite=Strict` in production." FR-005 adds that "documented
local HTTP development may omit **only** the `Secure` attribute" — so `HttpOnly`
and `SameSite=Strict` are unconditional, and the environment decides one thing.

`HttpOnly` is what keeps the identifier out of `document.cookie`, and this is a
product whose UI runs agent-supplied tools in the same page. `SameSite=Strict`
is what stops another origin from spending a visitor's workspace, which matters
because the cookie is the *only* thing authorizing a mutation (FR-006) — there
is no second factor to fall back on.

Not every request gets a workspace. FR-009 excludes health checks and static
assets from rate limiting for the same reason they are excluded here: a
liveness probe that minted a workspace every few seconds would fill the table
with rows no human ever visits.

The workspace is resolved in its own short transaction *before* the handler
runs, and that transaction is closed before `call_next` (ADR-0003: nothing is
held across a wait). The handler then opens its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from actionwitness_service.application.workspaces import WORKSPACE_COOKIE_NAME, WorkspaceStore

__all__ = ["WORKSPACE_COOKIE_MAX_AGE_SECONDS", "WorkspaceCookieMiddleware", "workspace_id_of"]

#: Seven days. Project-allocated: FR-005 fixes no lifetime, and FR-009's
#: stale-workspace cleanup is the real bound on how long a workspace lives.
#: A cookie outliving its workspace is handled — an unknown identifier mints a
#: fresh workspace rather than failing — so this only decides how long a
#: returning visitor keeps their evidence.
WORKSPACE_COOKIE_MAX_AGE_SECONDS: Final = 7 * 24 * 60 * 60

#: Paths that never create or touch a workspace.
_EXEMPT_PREFIXES: Final = ("/healthz", "/assets", "/static", "/favicon.ico")


def workspace_id_of(request: Request) -> str | None:
    """The workspace this request acts in, or `None` on an exempt path."""
    return getattr(request.state, "workspace_id", None)


class WorkspaceCookieMiddleware(BaseHTTPMiddleware):
    """Resolves the workspace cookie and issues one on first access."""

    def __init__(self, app: object, *, store: WorkspaceStore, secure: bool) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._store = store
        self._secure = secure

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if self._is_exempt(request.url.path):
            return await call_next(request)

        presented = request.cookies.get(WORKSPACE_COOKIE_NAME)
        resolved = await self._store.resolve(presented)
        request.state.workspace_id = resolved.workspace_id

        response = await call_next(request)

        if resolved.issued:
            response.set_cookie(
                WORKSPACE_COOKIE_NAME,
                resolved.workspace_id,
                max_age=WORKSPACE_COOKIE_MAX_AGE_SECONDS,
                path="/",
                httponly=True,
                samesite="strict",
                secure=self._secure,
            )
        return response

    @staticmethod
    def _is_exempt(path: str) -> bool:
        return path.startswith(_EXEMPT_PREFIXES)
