"""One-origin composition: harness assets, storefront assets, and the `/demo` proxy.

Spec §29.1: "One-origin event routing may expose the harness at `/`, the harness
API at `/api/v1`, and Buggy Store at `/demo` plus `/demo/api/v1`; **process
co-location shall not bypass the versioned target API or adapter boundary**."
That last clause is the whole design constraint, and it is recorded as ADR-0006.

The store is a separate process on loopback. Nothing here imports `buggy_store`,
and the only route from the harness process to the demo target is an HTTP call
to the same `/demo/api/v1` surface `integrations.buggy_store` uses. Importing the
store would have been simpler, faster, and a different product: the harness's
claim is that it observes the target through a channel the target's own tool
responses do not control, and an in-process function call is not that channel.

Three things live under `/demo` and only one of them is proxied:

* `/demo` and `/demo/assets/**` are **static files** — the storefront bundle,
  built with `--base=/demo/` and copied into its own directory (§29.1 step 4).
  They are served here because the store process has no frontend of its own; a
  blanket `/demo/**` proxy would forward asset requests to a process that has no
  assets and answer 404 for the storefront's own JavaScript.
* `/demo/api/v1/**` is **proxied** to the store process, unchanged.

`HARNESS_STATIC_ROOT` is absent in development, where Vite serves both UIs and
proxies the APIs itself. Absent means "mount nothing": a service that insisted on
finding a build directory could not be started from a source checkout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from actionwitness_service.api.errors import ApiError, ApiErrorCode

__all__ = [
    "DEMO_API_PREFIX",
    "DEMO_PREFIX",
    "MAX_PROXIED_BODY_BYTES",
    "PROXIED_REQUEST_HEADERS",
    "PROXIED_RESPONSE_HEADERS",
    "mount_static_applications",
    "register_demo_proxy",
]

#: §29.1's mount points.
DEMO_PREFIX: Final = "/demo"
DEMO_API_PREFIX: Final = "/demo/api/v1"

#: §20.2 bounds frontend-submitted payloads at 64 KiB. The storefront's largest
#: body is a cart mutation of a few hundred bytes, so this is a ceiling on abuse
#: rather than a limit anyone legitimate can reach. Enforced here because the
#: proxy buffers: without it, one request could hold an arbitrary amount of the
#: harness process's memory before the store ever sees a byte.
MAX_PROXIED_BODY_BYTES: Final = 64 * 1024

#: Forwarded to the store, by allowlist.
#:
#: An allowlist rather than a denylist of hop-by-hop names, because the risk here
#: is *addition*, not omission. `Cookie` is the one that matters: the harness
#: workspace cookie is `HttpOnly` ambient authority for `/api/v1`, and forwarding
#: it into a second application would hand the demo target a credential it has no
#: use for and no obligation to protect. `X-Workspace-Id` passes through
#: untouched — it is the storefront's own isolation identity (§15.5), the harness
#: neither reads nor rewrites it, and a proxy that "helpfully" substituted the
#: harness workspace would silently merge two different isolation scopes.
PROXIED_REQUEST_HEADERS: Final = frozenset({"accept", "content-type", "x-workspace-id"})

#: Returned to the caller, by allowlist. `Set-Cookie` is deliberately absent: the
#: store sets none, and a proxy that forwarded one would let the demo target
#: write a cookie onto the harness's origin.
PROXIED_RESPONSE_HEADERS: Final = frozenset({"content-type", "retry-after"})

#: The store's API is a JSON surface (§15.5). It never redirects, so a 3xx is
#: either a misconfiguration or something answering on that port that is not the
#: store — and following it, or passing its `Location` back, would leave the
#: configured origin. Refused instead.
_REDIRECT_STATUSES: Final = range(300, 400)


def mount_static_applications(app: FastAPI, static_root: str | Path | None) -> bool:
    """Serve both built frontends from their own directories. §29.1 step 4.

    Returns whether anything was mounted, which the health endpoint reports: an
    operator looking at a blank page needs to distinguish "assets missing from
    the image" from "asset request failing".
    """
    if static_root is None:
        return False
    root = Path(static_root)
    harness = root / "harness"
    demo = root / "demo"
    if not (harness / "index.html").is_file():
        # A missing bundle is not a reason to refuse to start: the API, the CLI,
        # and the whole manual path work without it, and a service that exited
        # here would turn a cosmetic packaging mistake into an outage.
        return False

    _mount_assets(app, "/assets", harness / "assets")
    _mount_assets(app, f"{DEMO_PREFIX}/assets", demo / "assets")

    @app.get("/", include_in_schema=False)
    async def harness_index() -> FileResponse:
        return FileResponse(harness / "index.html")

    if (demo / "index.html").is_file():

        @app.get(DEMO_PREFIX, include_in_schema=False)
        @app.get(f"{DEMO_PREFIX}/", include_in_schema=False)
        async def storefront_index() -> FileResponse:
            return FileResponse(demo / "index.html")

    favicon = harness / "favicon.ico"
    if favicon.is_file():

        @app.get("/favicon.ico", include_in_schema=False)
        async def icon() -> FileResponse:
            return FileResponse(favicon)

    return True


def _mount_assets(app: FastAPI, path: str, directory: Path) -> None:
    """Mount a hashed-asset directory, or nothing if the build produced none."""
    if directory.is_dir():
        app.mount(path, StaticFiles(directory=directory), name=f"assets{path}")


def register_demo_proxy(app: FastAPI, *, enabled: bool) -> None:
    """Expose the store's versioned API at its own path on the harness origin.

    Registered even when the Buggy Store module is disabled, so the path answers
    with the module's state rather than a 404 that reads as "wrong URL".
    """

    @app.api_route(
        f"{DEMO_API_PREFIX}/{{store_path:path}}",
        methods=["GET", "POST", "DELETE"],
        include_in_schema=False,
    )
    async def proxy_to_store(store_path: str, request: Request) -> Response:
        if not enabled:
            raise ApiError(
                ApiErrorCode.TARGET_UNAVAILABLE,
                "The Buggy Store module is not enabled in this deployment.",
            )

        client: httpx.AsyncClient | None = getattr(request.app.state, "target_client", None)
        if client is None:  # pragma: no cover - lifespan always sets it
            raise ApiError(
                ApiErrorCode.TARGET_UNAVAILABLE,
                "The Buggy Store client is not available.",
            )

        body = await request.body()
        if len(body) > MAX_PROXIED_BODY_BYTES:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                f"The request body exceeds {MAX_PROXIED_BODY_BYTES} bytes.",
            )

        try:
            upstream = await client.request(
                request.method,
                f"{DEMO_API_PREFIX}/{store_path}",
                params=dict(request.query_params),
                content=body,
                headers={
                    name: value
                    for name, value in request.headers.items()
                    if name.lower() in PROXIED_REQUEST_HEADERS
                },
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            # Non-retryable on purpose. A proxied mutation that failed in transit
            # has an *ambiguous* outcome — the store may already have applied it —
            # and §20.2 requires an ambiguous mutation outcome to be treated as
            # unsafe to retry automatically. The storefront's own request ID makes
            # a deliberate retry safe; a client looping on `retryable: true` would
            # not be deliberate.
            raise ApiError(
                ApiErrorCode.TARGET_UNAVAILABLE,
                "The demo target did not answer.",
            ) from exc

        if upstream.status_code in _REDIRECT_STATUSES:
            raise ApiError(
                ApiErrorCode.TARGET_UNAVAILABLE,
                "The demo target answered with a redirect, which is not part of its API.",
            )

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={
                name: value
                for name, value in upstream.headers.items()
                if name.lower() in PROXIED_RESPONSE_HEADERS
            },
        )
