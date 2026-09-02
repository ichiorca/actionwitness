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

FR-009's creation allowance is spent inside that resolution rather than by the
rate limiter that runs above it — see `RateLimitMiddleware` for why the layer
that owns the buckets is the wrong layer to decide.
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
from actionwitness_service.application.workspaces import (
    WORKSPACE_COOKIE_NAME,
    CreationCharge,
    CreationRefused,
    WorkspaceStore,
)

__all__ = [
    "WORKSPACE_COOKIE_MAX_AGE_SECONDS",
    "OriginMiddleware",
    "RateLimitMiddleware",
    "WorkspaceCookieMiddleware",
    "creation_charge_of",
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

#: Static documents that cannot be matched by prefix, because their prefix is
#: everything.
#:
#: `/` and `/demo` are `FileResponse`s of the two bundles' `index.html` (§29.1
#: step 4) — static assets that happen to sit at the root of their mount. They
#: were inside the request bucket only because `startswith("/")` matches every
#: path, so no prefix could name them; the effect was that a burst of ordinary
#: navigations was answered with the JSON error envelope rendered as the page
#: body, which is not a refusal a person can read.
#:
#: Exempting them relaxes nothing that protects state. `/` still resolves a
#: workspace, so FR-009's stricter creation allowance is still spent there —
#: see `RateLimitMiddleware.dispatch`, which offers that charge independently of
#: this exemption for exactly this reason.
_EXEMPT_DOCUMENTS: Final = frozenset({"/", "/demo", "/demo/"})

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


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    """Whether `path` *is* one of these prefixes or sits beneath it.

    A bare `startswith` matched on characters rather than on path segments, so
    every exemption silently covered a family of neighbours nobody exempted:
    `/healthz-anything`, `/assetsX`, `/demo/assets.zip`. None of those resolve
    to a route, but the exemption is consulted before routing — so an
    unmetered, unauthenticated 404 was reachable by appending a character to a
    health check, which is precisely the free traffic FR-009's bucket exists to
    deny. Matching on the segment boundary keeps every real asset path exempt
    and gives the neighbours no exemption at all.
    """
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in prefixes)


def is_exempt_path(path: str) -> bool:
    """Whether this path is outside the rate limit (FR-009: health and static).

    Exempt from the **request** bucket only. Nothing here decides the creation
    allowance, which is offered separately so that exempting a document from
    per-minute metering can never become a way to mint workspaces for free.
    """
    return _under(path, _EXEMPT_PREFIXES) or path in _EXEMPT_DOCUMENTS


def is_workspace_exempt_path(path: str) -> bool:
    """Whether this path takes no harness workspace cookie."""
    return _under(path, _WORKSPACE_EXEMPT_PREFIXES)


def workspace_id_of(request: Request) -> str | None:
    """The workspace this request acts in, or `None` on an exempt path."""
    return getattr(request.state, "workspace_id", None)


def creation_charge_of(request: Request) -> CreationCharge | None:
    """The workspace-creation allowance `RateLimitMiddleware` left for this request.

    `None` when no rate limiter is in the stack, or on a path that takes no
    harness workspace. Absent means unmetered, which is correct rather than
    convenient: a deployment that composed no limiter has no allowance to spend,
    and inventing one here would put FR-009's policy in two places.
    """
    charge: CreationCharge | None = getattr(request.state, "workspace_creation_charge", None)
    return charge


class WorkspaceCookieMiddleware(BaseHTTPMiddleware):
    """Resolves the workspace cookie and issues one on first access.

    Also where FR-009's stricter creation allowance is actually spent. It is
    spent here, not in the limiter above, because this is the first layer that
    knows whether a workspace is about to exist: an unknown cookie mints a fresh
    workspace exactly like no cookie at all (`WorkspaceStore.resolve` never
    adopts a presented identifier), so no layer above can tell the two apart
    without asking the database the same question resolution is about to ask.
    """

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
        resolved = await self._store.resolve(presented, charge_creation=creation_charge_of(request))
        if isinstance(resolved, CreationRefused):
            # Refused before the `INSERT`, so there is no row and no handler ran
            # — FR-009's "never partially commit a mutation" holds here for the
            # same reason it holds in the limiter: there is nothing to commit.
            # Recorded for §21.5's classification, because a response built in
            # middleware never reaches an exception handler that could record it.
            request.state.error_code = ApiErrorCode.RATE_LIMIT_EXCEEDED.value
            return _too_many(resolved.retry_after_seconds)

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

    This layer spends the general per-minute bucket but **does not decide** the
    stricter creation bucket. Running first is what makes the refusal cheap, and
    it is also what makes the creation question unanswerable here: at this point
    the cookie has not been looked up, and a cookie is not evidence that a
    workspace exists. Deciding from the cookie's mere presence — the shape this
    middleware had until FR-009 was reread — meant a client could mint an
    unlimited number of workspaces by sending a different invented cookie value
    on every request, since each unknown value resolves to a brand-new workspace
    while looking, from up here, like a returning visitor.

    So the allowance is handed *down* as a charge for `WorkspaceCookieMiddleware`
    to spend at the moment of issuance. The alternative — asking the database up
    here whether the presented cookie is known — would answer the same question
    twice, one query apart, and the second answer is the one that acts. Two
    predictions of one fact is how the original defect happened.
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
        key = client_key(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            trusted_proxies=self._trusted_proxies,
        )
        request.state.client_key = key

        # The two buckets are decided independently, and that independence is
        # load-bearing rather than tidy. Returning early on an exempt path — as
        # this did — meant that exempting a path from per-minute metering also
        # withheld the creation charge from the layer below, so any static-looking
        # path that still resolved a workspace would have minted them unmetered.
        # `/` is exactly that path: an `index.html` nobody should be rate-limited
        # for loading, and the one request that issues the workspace cookie.
        if not is_exempt_path(request.url.path):
            exhausted = self._limiter.allow_request(key)
            if exhausted is not None:
                request.state.error_code = ApiErrorCode.RATE_LIMIT_EXCEEDED.value
                return _too_many(exhausted.retry_after_seconds())

        # The stricter workspace-creation bucket is offered, not spent. Only the
        # resolution below can tell a returning visitor from a request whose
        # cookie names nothing, and only a returning visitor must go uncharged —
        # otherwise one user refreshing a page exhausts an hour's allowance in a
        # minute. A storefront-only visitor is offered nothing at all:
        # `/demo/api/v1` takes no harness workspace, so ordinary use of the demo
        # target can never exhaust the allowance for creating harness workspaces.
        if not is_workspace_exempt_path(request.url.path):
            request.state.workspace_creation_charge = self._creation_charge(key)

        return await call_next(request)

    def _creation_charge(self, key: str) -> CreationCharge:
        """One client's creation allowance, as something the layer below can spend.

        A closure over the key rather than a method taking one, so nothing
        downstream can charge a client other than the one this request came
        from — the key is derived here, from the direct peer (§20.1), and never
        travels as an argument that a later layer could substitute.
        """

        def charge() -> int | None:
            exhausted = self._limiter.allow_workspace_creation(key)
            return None if exhausted is None else exhausted.retry_after_seconds()

        return charge


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
