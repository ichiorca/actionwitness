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

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import httpx
from actionwitness_core.kernel import CoreError
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from actionwitness_service.api.composition import (
    mount_static_applications,
    register_demo_proxy,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode, error_from_core
from actionwitness_service.api.middleware import (
    OriginMiddleware,
    RateLimitMiddleware,
    WorkspaceCookieMiddleware,
)
from actionwitness_service.api.origins import OriginPolicy
from actionwitness_service.api.routes import audits as audit_routes
from actionwitness_service.api.routes import benchmarks as benchmark_routes
from actionwitness_service.api.routes import contracts as contract_routes
from actionwitness_service.api.routes import evals as eval_routes
from actionwitness_service.api.routes import runs as run_routes
from actionwitness_service.api.routes import workspace as workspace_routes
from actionwitness_service.api.security_headers import SecurityHeadersMiddleware
from actionwitness_service.application.adapter_registry import AdapterRegistry
from actionwitness_service.application.artifacts import ArtifactStore
from actionwitness_service.application.cleanup import WorkspaceCleaner
from actionwitness_service.application.contract_service import seed_templates
from actionwitness_service.application.limits import EventStreamSlots
from actionwitness_service.application.rate_limits import RateLimiter
from actionwitness_service.application.template_catalogue import (
    ExpansionRejected,
    TemplateCatalogue,
    TemplateExpansion,
)
from actionwitness_service.application.workspaces import WorkspaceStore
from actionwitness_service.config import ServiceSettings
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.locks import WorkspaceLocks
from actionwitness_service.telemetry import RequestLoggingMiddleware, configure_logging

__all__ = ["API_PREFIX", "create_app"]

#: §15: every harness route lives under one versioned prefix.
API_PREFIX = "/api/v1"

#: Separate from the structured request logger: this one carries a traceback, so
#: it must never be the channel §21.5 describes.
_unhandled_logger = logging.getLogger("actionwitness.unhandled")

#: Every phase of a target call — connect, read, write, pool. The target is a
#: local or operator-configured origin, so this is generous rather than tight:
#: it exists to stop a hung observation from pinning a run open forever, not to
#: police a slow one.
TARGET_TIMEOUT_SECONDS: Final = 5.0


def create_app(
    *,
    environ: Mapping[str, str] | None = None,
    database_path: str | Path | None = None,
    clock: Callable[[], datetime] | None = None,
    target_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """Build the application.

    `environ`, `database_path`, `clock`, and `target_client` are injectable so a test can
    construct the real application rather than a lookalike, and so evaluation
    and replay stay deterministic (constitution §1). Passing none of them reads
    the process environment once, here and nowhere else.
    """
    # The composition root is also where logging is composed. Conservative by
    # construction — it adds a handler only when the process has none — so a
    # deployment that configured its own keeps it, and the forty-odd tests that
    # build an application do not each install another handler.
    configure_logging()

    settings = ServiceSettings.from_env(os.environ if environ is None else environ)
    database = Database(database_path or settings.harness.database_path, clock=clock)
    workspaces = WorkspaceStore(database)
    limiter = RateLimiter(clock=clock)
    cleaner = WorkspaceCleaner(
        database,
        artifact_root=settings.harness.artifact_root,
        clock=clock,
        # The limiter's per-client buckets are the one piece of in-memory state
        # that grows with unique traffic rather than with stored data. Without
        # this the process keeps one entry per address it has ever seen, for as
        # long as it lives — `release_idle` exists for exactly this call.
        on_sweep=limiter.release_idle,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.schema_version = await database.initialize()

        # ADR-0001: one lifespan-owned client, injected into the adapters. A
        # client per request would lose connection reuse; a module-level one
        # would outlive the loop it was created on. A test that supplied its
        # own keeps it — closing somebody else's client is not this scope's
        # business.
        owned_client = target_client is None
        client = target_client or httpx.AsyncClient(
            base_url=settings.buggy_store.base_url if settings.buggy_store else "",
            # Stated rather than inherited. httpx's default happens to be 5s on
            # every phase, but a timeout the constitution requires to be
            # explicit is one a reader should be able to find without knowing
            # the library's defaults — and without a silent change of behaviour
            # if a future httpx alters them. An observation that never returns
            # is a run that never reaches a verdict.
            timeout=httpx.Timeout(TARGET_TIMEOUT_SECONDS),
        )
        app.state.adapters = AdapterRegistry(settings, client=client)
        # The `/demo/api/v1` proxy (§29.1) reaches the store over the same client
        # the adapter uses, so a test that injects an `ASGITransport` gets the
        # proxy pointed at its store too — the composed path is exercised rather
        # than approximated.
        app.state.target_client = client

        # §29.1: the built-in templates are seeded at startup, from the
        # integration that owns them. Idempotent, and skipped entirely when the
        # integration is not installed — §21.1 requires the harness to run
        # without it, and a startup that insisted on seeding its templates
        # would make that impossible.
        app.state.templates_seeded = await _seed_builtin_templates(database, app.state.adapters)
        # The same templates, kept in memory as well: seeding stores each
        # document, and instantiating needs what a document does not carry —
        # the scalars a template allowlists and the arithmetic that expands
        # one (FR-021). Composed here for the same reason seeding is, and
        # empty when the integration is absent.
        app.state.templates = _build_template_catalogue(app.state.adapters)

        # FR-009: "at startup and at least hourly". The startup sweep is awaited
        # so a deployment begins with expired data already gone; the hourly one
        # is a task this scope owns and cancels, because a background task
        # nobody owns outlives the application it was serving.
        stop = asyncio.Event()
        sweeper = asyncio.create_task(cleaner.run_until(stop))
        try:
            yield
        finally:
            # Cooperative, not a cancellation: cancelling mid-sweep would
            # interrupt an open transaction and leave the driver's worker
            # thread unwound. The cancel below is only the backstop for a
            # sweep that will not finish.
            stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(sweeper), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                sweeper.cancel()
                with suppress(asyncio.CancelledError):
                    await sweeper
            if owned_client:
                await client.aclose()

    app = FastAPI(title="ActionWitness", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.database = database
    app.state.workspaces = workspaces
    app.state.locks = WorkspaceLocks()
    app.state.limiter = limiter
    # FR-008's "two concurrent event-stream connections", counted per workspace.
    # Owned by the application rather than by the module that streams, so two
    # applications in one process — which the tests build routinely — do not
    # share a ceiling, and so the count dies with the connections it counts.
    # It needs no cleanup hook: an entry exists only while a stream is open.
    app.state.event_streams = EventStreamSlots()
    app.state.artifacts = ArtifactStore(settings.harness.artifact_root)
    app.state.cleaner = cleaner

    # Starlette runs middleware in reverse registration order, so the origin
    # check registered last runs first: a mutation from a disallowed origin is
    # refused before it can create a workspace (§20.1).
    app.add_middleware(
        WorkspaceCookieMiddleware,
        store=workspaces,
        secure=settings.harness.secure_cookies,
    )
    app.add_middleware(OriginMiddleware, policy=OriginPolicy(settings.harness.public_origin))
    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        trusted_proxies=settings.harness.trusted_proxies,
    )
    # Registered last, so they run first and therefore *outermost*. Both have to
    # see what the layers below refuse: a 429 from the rate limiter never reaches
    # a route, and it is exactly the response an operator needs logged and the
    # one a browser still needs the security headers on.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestLoggingMiddleware)

    @app.exception_handler(ApiError)
    async def deliberate_refusal(request: Request, exc: ApiError) -> JSONResponse:
        """Every refusal reaches the client as §15.8's one envelope.

        Registered here rather than left to each handler, because a handler
        that has to remember to build an envelope will eventually forget and
        the forgotten path becomes a 200 with a half-built body. The status and
        `retryable` both come from the code's registry entry, so a call site
        cannot widen either.
        """
        request.state.error_code = exc.code.value
        return JSONResponse(status_code=exc.http_status, content=exc.as_envelope())

    @app.exception_handler(RequestValidationError)
    async def malformed_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        """A rejected path, query, or body reaches the client as §15.8's envelope.

        FastAPI's own 422 body is `{"detail": [...]}`, a second error shape
        beside the one §15.8 defines. A client would then need two parsers to
        learn why a call failed, and the one it wrote first would silently
        mishandle the other.

        Only `loc` and `msg` are forwarded. Pydantic's `input` and `ctx` echo
        the submitted value back, which for a body carrying a credential would
        put it in a response and, from there, into whatever logs that response.
        """
        details = [
            {
                # `loc` starts with the source — "query", "body", "path" — which
                # is worth keeping: `limit` means something different in each.
                "path": ".".join(str(part) for part in error.get("loc", ())),
                "message": str(error.get("msg", "invalid value")),
            }
            for error in exc.errors()
        ]
        refusal = ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "The request was not in an acceptable shape.",
            details=details,
        )
        request.state.error_code = refusal.code.value
        return JSONResponse(status_code=refusal.http_status, content=refusal.as_envelope())

    @app.exception_handler(CoreError)
    async def core_failure(request: Request, exc: CoreError) -> JSONResponse:
        """A domain failure acquires its HTTP status here and nowhere else.

        The core carries no status of its own — it has to install alone — so
        this is the seam where `INVALID_STATE_TRANSITION` becomes §16's 409 and
        an unmapped code becomes a 500 with no text of its own.
        """
        translated = error_from_core(exc)
        request.state.error_code = translated.code.value
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
        request.state.error_code = refusal.code.value
        # The detail belongs in the server log, which is not the response. This
        # is the one place a traceback is wanted, and `exc_info` keeps it out of
        # the structured line's fields (§21.5) while still reaching stderr.
        _unhandled_logger.exception("unhandled request failure", exc_info=exc)
        return JSONResponse(status_code=refusal.http_status, content=refusal.as_envelope())

    @app.get("/healthz")
    async def healthz(response: Response) -> dict[str, object]:  # spec §29.1
        """Liveness, plus the two facts an operator cannot get any other way.

        `public_origin` is reported because it is the single most common
        deployment mistake this service has: it drives both the cookie's `Secure`
        attribute and the origin allowlist, and when it is wrong every mutation
        is refused with a correct-looking 403 from a service that is otherwise
        healthy. It is an operator-supplied *origin*, not a secret — §29.1
        forbids credentials in the health response, and there are none here.

        `assets_mounted` distinguishes "the image shipped without a frontend"
        from "the frontend failed to load", which look identical in a browser.

        `database` is read on every call rather than remembered from startup.
        A schema version captured once says the database opened an hour ago, not
        that it is readable now — so a deleted, corrupted, or unreadable file
        would leave this endpoint reporting `ok` while every real request failed,
        and both Render's health check and the Docker `HEALTHCHECK` would keep
        the instance in service.

        The probe reads a *table* rather than `SELECT 1`, because SQLite creates
        an empty database for a path that no longer exists: a constant select
        would answer just as happily from the empty file it had silently made,
        which is precisely the deleted-volume case this is here to catch. One
        indexed row at most, so it costs a connection and nothing else.
        """
        database_reachable = True
        try:
            async with database.reading() as work:
                await work.fetch_one("SELECT 1 FROM workspaces LIMIT 1")
        except Exception:
            # Never re-raised: an unreachable database is what this endpoint
            # exists to report, and a 500 here would report only that the health
            # check itself broke.
            database_reachable = False
            _unhandled_logger.exception("health check could not read the database")

        if not database_reachable:
            # 503 rather than a healthy-looking body with a bad field in it:
            # the platform reads the status code, and a health check that
            # answers 200 while the source of truth is gone is the failure mode
            # this branch was added to remove.
            response.status_code = 503
        return {
            "status": "ok" if database_reachable else "degraded",
            "schema_version": getattr(app.state, "schema_version", None),
            "public_origin": settings.harness.public_origin,
            "assets_mounted": app.state.assets_mounted,
            "database": "ok" if database_reachable else "unavailable",
        }

    app.include_router(workspace_routes.router, prefix=API_PREFIX)
    app.include_router(contract_routes.router, prefix=API_PREFIX)
    app.include_router(run_routes.router, prefix=API_PREFIX)
    app.include_router(eval_routes.router, prefix=API_PREFIX)
    # §12.17's audit surface. Mounted unconditionally so an unconfigured
    # deployment answers with a named refusal (§21.1) rather than a 404 that
    # reads as a wrong URL; the module state is what gates it, not the route.
    app.include_router(audit_routes.router, prefix=API_PREFIX)
    app.include_router(benchmark_routes.router, prefix=API_PREFIX)

    # §29.1's one-origin composition, registered after the harness API so that
    # `/api/v1` can never be shadowed by an asset mount. ADR-0006 records why the
    # store is proxied rather than imported.
    register_demo_proxy(app, enabled=settings.is_enabled("buggy_store"))
    app.state.assets_mounted = mount_static_applications(app, settings.harness.static_root)

    # TODO(M8): §15.2's from-candidates and published endpoints. Both depend on
    # proposal-mode runs and assertion candidates, which `run_service` still
    # refuses by name, so neither can be written before that lands.
    #
    # §15.3 is no longer listed here: run read, paged events, invocation,
    # confirmation decisions, verify, report and comparison are all implemented
    # in `routes/runs.py` and mounted above. The note outlived the work and read,
    # to anyone auditing this file, as a much larger hole than actually exists.
    return app


def _build_template_catalogue(registry: AdapterRegistry) -> TemplateCatalogue:
    """Compose the instantiable templates from each available integration.

    The translation of the integration's own rejection into `ExpansionRejected`
    happens here, at the composition root, so the service keeps one error type
    to catch and no generic module imports a commerce package. An absent
    integration contributes nothing and is not an error (§21.1) — the catalogue
    is simply smaller, and a request naming one of its templates is refused by
    name rather than by a crash.
    """
    if not registry.is_available("buggy_store"):
        return TemplateCatalogue()
    from integrations.buggy_store.templates import TEMPLATES, TemplateExpansionError
    from integrations.buggy_store.templates import expand as expand_buggy_store

    def _expander(template_id: str) -> Callable[[Mapping[str, Any]], Mapping[str, Any]]:
        def expand(parameters: Mapping[str, Any]) -> Mapping[str, Any]:
            try:
                return expand_buggy_store(template_id, parameters)
            except TemplateExpansionError as rejected:
                raise ExpansionRejected(rejected.details) from rejected

        return expand

    return TemplateCatalogue(
        TemplateExpansion(
            template_id=template.template_id,
            parameters=tuple(template.parameters),
            expand=_expander(template.template_id),
        )
        for template in TEMPLATES
    )


async def _seed_builtin_templates(database: Database, registry: AdapterRegistry) -> int:
    """Seed each available integration's built-in contract templates.

    An integration that is absent contributes nothing and is not an error
    (§21.1). The seeding runs in one transaction so a partial set is never
    visible, and it is idempotent so a restart writes nothing.
    """
    if not registry.is_available("buggy_store"):
        return 0
    from integrations.buggy_store import TEMPLATES

    async with database.transaction() as work:
        return await seed_templates(work, TEMPLATES)
