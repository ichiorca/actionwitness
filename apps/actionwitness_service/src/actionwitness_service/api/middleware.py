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
from starlette.responses import JSONResponse, Response

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.api.origins import OriginPolicy
from actionwitness_service.application.rate_limits import RateLimiter, client_key
from actionwitness_service.application.workspaces import WORKSPACE_COOKIE_NAME, WorkspaceStore

__all__ = [
    "WORKSPACE_COOKIE_MAX_AGE_SECONDS",
    "OriginMiddleware",
    "RateLimitMiddleware",
    "WorkspaceCookieMiddleware",
    "is_exempt_path",
    "is_workspace_exempt_path",
    "workspace_id_of",
]

#: Seven days. Project-allocated: FR-005 fixes no lifetime, and FR-009's
#: stale-workspace cleanup is the real bound on how long a workspace lives.
#: A cookie outliving its workspace is handled — an unknown identifier mints a
#: fresh workspace rather than failing — so this only decides how long a
#: returning visitor keeps their evidence.
WORKSPACE_COOKIE_MAX_AGE_SECONDS: Final = 7 * 24 * 60 * 60

#: Paths exempt from both workspace creation and rate limiting.
#:
#: FR-009 excludes "health checks and static assets" from the limit; they are
#: excluded from workspace creation for the same underlying reason. A liveness
#: probe running every second would otherwise consume half a client's request
#: allowance and mint a workspace per check — taking a deployment down by
#: monitoring it, and filling the table with rows no human ever visits.
_EXEMPT_PREFIXES: Final = (
    "/healthz",
    "/assets",
    "/static",
    "/favicon.ico",
    # The storefront bundle, served from its own directory under the composed
    # one-origin deployment (§29.1). A static asset either side of `/demo` is
    # still a static asset.
    "/demo/assets",
)

#: The storefront's own paths, which take no harness workspace.
#:
#: `/demo/api/v1` is the Buggy Store's versioned surface and carries the store's
#: own `X-Workspace-Id` (§15.5). Minting a *harness* workspace for a visitor who
#: only ever opens the storefront would fill the table with rows no harness user
#: created, and would attach a `Secure; HttpOnly` credential for `/api/v1` to a
#: page that is required to work with no harness at all (AC-09).
#:
#: Rate limiting is deliberately NOT waived here — see `is_exempt_path`. The
#: store API is mutating and reachable from a browser, so it gets the same
#: per-peer bucket as everything else.
_WORKSPACE_EXEMPT_PREFIXES: Final = (*_EXEMPT_PREFIXES, "/demo")


def is_exempt_path(path: str) -> bool:
    """Whether this path is outside the rate limit (FR-009: health and static)."""
    return path.startswith(_EXEMPT_PREFIXES)


def is_workspace_exempt_path(path: str) -> bool:
    """Whether this path takes no harness workspace cookie."""
    return path.startswith(_WORKSPACE_EXEMPT_PREFIXES)


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
        if is_workspace_exempt_path(request.url.path):
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


class OriginMiddleware(BaseHTTPMiddleware):
    """Refuses a mutating request whose `Origin` is not the harness's (§20.1).

    Registered *after* the cookie middleware so that it runs *before* it —
    Starlette applies middleware in reverse registration order. That ordering
    is deliberate: a mutation from a disallowed origin should be refused
    without first minting a workspace for whoever sent it, or a hostile page
    could fill the table by being rejected repeatedly.

    The refusal is built here rather than raised, because an exception thrown
    from middleware travels outside the application's exception handlers and
    would surface as a bare 500 with no envelope.
    """

    def __init__(self, app: object, *, policy: OriginPolicy) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._policy = policy

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            self._policy.check(request)
        except ApiError as refusal:
            # Recorded for the structured log (§21.5's "classification"). A
            # refusal built here never reaches an exception handler, so this is
            # the only place that can classify it — and a refused mutation is
            # precisely the line an operator needs to see.
            request.state.error_code = refusal.code.value
            return JSONResponse(status_code=refusal.http_status, content=refusal.as_envelope())
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FR-009's per-peer token buckets, applied before anything is written.

    Registered last so it runs first (Starlette reverses registration order).
    That placement is what makes "shall never partially commit a mutation" true
    by construction rather than by a rollback somebody has to remember: a
    refused request has not reached a handler, so there is nothing to commit.

    Health checks and static assets are excluded, exactly as FR-009 says. A
    liveness probe running every second would otherwise consume half a
    workspace's request allowance and take the deployment down by monitoring it.
    """

    def __init__(
        self,
        app: object,
        *,
        limiter: RateLimiter,
        trusted_proxies: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._limiter = limiter
        self._trusted_proxies = trusted_proxies

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if is_exempt_path(request.url.path):
            return await call_next(request)

        key = client_key(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            trusted_proxies=self._trusted_proxies,
        )
        request.state.client_key = key

        exhausted = self._limiter.allow_request(key)
        if exhausted is not None:
            request.state.error_code = ApiErrorCode.RATE_LIMIT_EXCEEDED.value
            return _too_many(exhausted.retry_after_seconds())

        # The stricter workspace-creation bucket is spent only when a workspace
        # would actually be created. A returning visitor must not spend from it
        # — otherwise one user refreshing a page exhausts an hour's allowance in
        # a minute. Nor may a storefront-only visitor: `/demo/api/v1` takes no
        # harness workspace at all, so charging it for one would let ordinary use
        # of the demo target exhaust the allowance for creating harness
        # workspaces.
        if not is_workspace_exempt_path(request.url.path) and (
            WORKSPACE_COOKIE_NAME not in request.cookies
        ):
            creating = self._limiter.allow_workspace_creation(key)
            if creating is not None:
                request.state.error_code = ApiErrorCode.RATE_LIMIT_EXCEEDED.value
                return _too_many(creating.retry_after_seconds())

        return await call_next(request)


def _too_many(retry_after: int) -> JSONResponse:
    """FR-009's "stable 429", in §15.8's envelope.

    `Retry-After` is a whole number of seconds and never zero: a client told to
    retry immediately would fail immediately, turning one refusal into a loop.
    """
    refusal = ApiError(
        ApiErrorCode.RATE_LIMIT_EXCEEDED,
        "Too many requests from this client. Retry after the interval given.",
    )
    return JSONResponse(
        status_code=refusal.http_status,
        content=refusal.as_envelope(),
        headers={"Retry-After": str(retry_after)},
    )
