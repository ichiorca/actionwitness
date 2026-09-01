"""FastAPI application factory.

Composition root: the one place that reads the environment, opens the database,
and wires the middleware. Everything below it receives what it needs, which is
what lets the tests build a whole application against a temporary file without
touching process state.

Migrations run in the lifespan, once, before the first request (ADR-0003:
"invoked once at startup", never lazily from repository code). A request that
arrived first and found no tables would be indistinguishable from a request
against a corrupted database.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from actionwitness_core.kernel import CoreError
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from actionwitness_service.api.errors import ApiError, ApiErrorCode, error_from_core
from actionwitness_service.api.middleware import OriginMiddleware, WorkspaceCookieMiddleware
from actionwitness_service.api.origins import OriginPolicy
from actionwitness_service.application.workspaces import WorkspaceStore
from actionwitness_service.config import ServiceSettings
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.locks import WorkspaceLocks

__all__ = ["create_app"]


def create_app(
    *,
    environ: Mapping[str, str] | None = None,
    database_path: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the application.

    `environ`, `database_path`, and `clock` are injectable so a test can
    construct the real application rather than a lookalike, and so evaluation
    and replay stay deterministic (constitution §1). Passing none of them reads
    the process environment once, here and nowhere else.
    """
    settings = ServiceSettings.from_env(os.environ if environ is None else environ)
    database = Database(database_path or settings.harness.database_path, clock=clock)
    workspaces = WorkspaceStore(database)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.schema_version = await database.initialize()
        yield

    app = FastAPI(title="ActionWitness", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.workspaces = workspaces
    app.state.locks = WorkspaceLocks()

    # Starlette runs middleware in reverse registration order, so the origin
    # check registered last runs first: a mutation from a disallowed origin is
    # refused before it can create a workspace (§20.1).
    app.add_middleware(
        WorkspaceCookieMiddleware,
        store=workspaces,
        secure=settings.harness.secure_cookies,
    )
    app.add_middleware(OriginMiddleware, policy=OriginPolicy(settings.harness.public_origin))

    @app.exception_handler(ApiError)
    async def deliberate_refusal(request: Request, exc: ApiError) -> JSONResponse:
        """Every refusal reaches the client as §15.8's one envelope.

        Registered here rather than left to each handler, because a handler
        that has to remember to build an envelope will eventually forget and
        the forgotten path becomes a 200 with a half-built body. The status and
        `retryable` both come from the code's registry entry, so a call site
        cannot widen either.
        """
        return JSONResponse(status_code=exc.http_status, content=exc.as_envelope())

    @app.exception_handler(CoreError)
    async def core_failure(request: Request, exc: CoreError) -> JSONResponse:
        """A domain failure acquires its HTTP status here and nowhere else.

        The core carries no status of its own — it has to install alone — so
        this is the seam where `INVALID_STATE_TRANSITION` becomes §16's 409 and
        an unmapped code becomes a 500 with no text of its own.
        """
        translated = error_from_core(exc)
        return JSONResponse(status_code=translated.http_status, content=translated.as_envelope())

    @app.exception_handler(Exception)
    async def unhandled_failure(request: Request, exc: Exception) -> JSONResponse:
        """The last line: no traceback, no exception text, no class name.

        §15.8 gives one envelope and §20 forbids leaking internals. An
        unhandled exception is precisely the case where the message is most
        likely to name a table, a path, or a value, so none of it is forwarded
        — the detail belongs in the server log, which is not this response.
        """
        refusal = ApiError(
            ApiErrorCode.HARNESS_ERROR, "The harness could not complete the request."
        )
        return JSONResponse(status_code=refusal.http_status, content=refusal.as_envelope())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # spec §29.1
        return {"status": "ok"}

    # TODO(004-T11/T12): include routers from actionwitness_service.api.routes (§15.1–15.2)
    # TODO(004-T7): Origin validation and the §15.8 error envelope
    # TODO(004-T9): per-peer rate limiting and stale-workspace cleanup (§29.1)
    # TODO(M4): mount compiled frontend assets; /demo composition per §29.1
    return app
