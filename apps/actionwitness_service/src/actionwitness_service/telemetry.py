"""Structured request logging (spec §21.5; 009-T4).

§21.5 fixes both halves of this: what a log line carries — request/run/eval IDs,
event type, tool name, status, duration, classification — and what it must never
carry, which is sensitive payload values.

The design decision worth stating is that this module cannot log a payload *by
construction*, rather than by remembering not to. `RequestLog` is a frozen model
with a closed field set and `extra="forbid"`; there is no `**kwargs`, no `extra`
dict, and no free-text message field. A future caller who wants to attach a cart
body has to add a field to this class and defend it in review, which is exactly
where that argument belongs. The alternative — a `logger.info(f"...")` at each
call site — puts the decision in the hands of whoever is debugging at 2am.

Identifiers are logged; values are not. A `run_id` is a workspace-scoped opaque
identifier that means nothing without the database, so it is safe and it is the
only thing that makes a production log useful. A query string is *not* logged for
the same reason a body is not: `/api/v1/...?code=SAVE20` puts a value in it.

Redaction is not attempted here. §20.3's key list exists for evidence and
reports, where the payload has to be kept and shown; a log line has no such
obligation, so the safe thing is to not have the value at all. A redactor is a
thing that can have a bug; an absent field cannot.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

__all__ = [
    "LOGGER_NAME",
    "UNMATCHED_ROUTE",
    "RequestLog",
    "RequestLoggingMiddleware",
    "configure_logging",
    "route_template",
]

#: One named logger, so a deployment can route or silence harness request logs
#: without touching uvicorn's own.
LOGGER_NAME: Final = "actionwitness.request"

_logger = logging.getLogger(LOGGER_NAME)


class RequestLog(BaseModel):
    """One request's line. The field set is closed — see the module docstring.

    Every field is an identifier, a status, a duration, or a classification.
    None of them can hold a value submitted by a client, which is the property
    `tests/integration/test_structured_logging.py` asserts against real traffic
    rather than against this class.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: §21.5's "event type". One value today; the field exists because a log
    #: consumer needs to distinguish shapes before it can parse them.
    event: str = "http_request"
    method: str
    #: The matched *route template* where one exists — `/api/v1/runs/{run_id}` —
    #: falling back to the raw path. The template is what makes lines groupable,
    #: and it cannot contain an identifier by definition.
    route: str
    status: int
    duration_ms: int
    #: Workspace-scoped identifiers (§21.5). Absent on exempt and unmatched paths.
    workspace_id: str | None = None
    run_id: str | None = None
    eval_case_id: str | None = None
    tool_name: str | None = None
    #: The stable `ApiErrorCode` for a refusal — the "classification" §21.5 names.
    #: The error *message* is never logged: handlers build messages from spec
    #: text today, and a future one interpolating a value would leak it here.
    error_code: str | None = None

    def render(self) -> str:
        """Compact JSON, key order fixed by declaration, `None` fields dropped."""
        return json.dumps(self.model_dump(exclude_none=True), separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    """Attach a plain stream handler if the process has none.

    Deliberately conservative: a deployment that has configured logging already
    keeps its configuration, and a `docker run` with nothing configured still
    produces output on stdout, which is where the platform collects it.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        # Set only alongside the handler this function installed. Setting it
        # unconditionally made the "keeps its configuration" promise above only
        # half true: a deployment that had deliberately raised the root logger
        # to WARNING got quietly lowered back to INFO by building an app.
        root.setLevel(level)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Emits one `RequestLog` per request, after the response is built.

    Registered so it runs outermost: a request refused by the rate limiter or the
    origin policy never reaches a route, and those refusals are the ones an
    operator most needs to see. Duration is measured with a monotonic clock, so a
    system clock adjustment cannot produce a negative one.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception does not come back as a response here. The
            # `Exception` handler is installed on Starlette's ServerErrorMiddleware,
            # which sits *outside* every application middleware: it builds the 500
            # envelope and re-raises, so this layer sees the exception and never
            # the response. Without this branch the one request an operator most
            # needs a line for — the one that crashed — would be the only request
            # that produced none.
            #
            # The status and code are stated rather than read from the response
            # for the same reason: that response does not exist yet. They are what
            # `create_app`'s handler is about to send, and a test holds the two in
            # agreement.
            self._emit(
                request,
                status=UNHANDLED_STATUS,
                duration_ms=_elapsed_ms(started),
                error_code=UNHANDLED_ERROR_CODE,
            )
            raise

        self._emit(
            request,
            status=response.status_code,
            duration_ms=_elapsed_ms(started),
            error_code=getattr(request.state, "error_code", None),
        )
        return response

    def _emit(
        self, request: Request, *, status: int, duration_ms: int, error_code: str | None
    ) -> None:
        entry = RequestLog(
            method=request.method,
            route=route_template(request),
            status=status,
            duration_ms=duration_ms,
            workspace_id=getattr(request.state, "workspace_id", None),
            run_id=_path_param(request, "run_id"),
            # Both of these read the router's own parameter names. An earlier
            # version guessed at `case_id` and at a `request.state.tool_name`
            # nothing ever assigned, so two of §21.5's required fields logged
            # `None` on every request that should have carried them — silently,
            # because a field that is legitimately absent looks identical to a
            # field that is misspelled. The names below are the ones the eval and
            # invoke routes declare, and a test holds them in agreement.
            eval_case_id=_path_param(request, "eval_case_id"),
            tool_name=_path_param(request, "tool_name"),
            error_code=error_code,
        )
        _logger.info(entry.render())


def _elapsed_ms(started: float) -> int:
    """Monotonic, so a system clock adjustment cannot produce a negative one."""
    return int((time.perf_counter() - started) * 1000)


#: The longest identifier this line will carry. Every identifier the service
#: mints is far shorter (`ws_`/`run_`/`inv_` plus hex), and the longest name a
#: valid tool may have is `MAX_TOOL_NAME_CHARS`; the bound exists for the values
#: that never were valid — see `_path_param`.
MAX_LOGGED_IDENTIFIER_CHARS: Final = 64

#: Appended to a truncated identifier so a reader can tell a bounded value from
#: a complete one, rather than chasing an id that was never that short.
TRUNCATION_MARKER: Final = "..."

#: Logged in place of a path that matched no route.
#:
#: An unmatched path is entirely attacker-controlled and has no template to
#: reduce it to, so there is nothing to log that is guaranteed not to be a value.
#: The platform's own access log records raw paths for whoever is debugging 404s;
#: this line is the one that gets shipped to a log service, and it stays clean.
UNMATCHED_ROUTE: Final = "<unmatched>"

#: What `create_app`'s last-resort handler is about to send for an exception that
#: reached the top. Held here rather than imported so this module stays a leaf —
#: and asserted against the real response in the integration test, so the two
#: cannot drift apart silently.
UNHANDLED_STATUS: Final = 500
UNHANDLED_ERROR_CODE: Final = "HARNESS_ERROR"


def route_template(request: Request) -> str:
    """The matched route with its identifiers reduced back to `{name}`.

    Derived from the real path rather than read off `scope["route"]`. FastAPI
    keeps an included router nested rather than flattening it, so the route
    object carries a path relative to its own prefix — `/workspace`, not
    `/api/v1/workspace` — and `root_path` is empty, so the prefix cannot be
    recovered from the scope at all. Reducing the real path is both correct and
    independent of that internal detail.

    A route with no path parameters is *already* a template: every segment of it
    is a literal in the router, so it can carry nothing a client submitted.

    Substitution is by whole path segment, never by substring. A run whose
    identifier happened to be `report` must not turn `/runs/report/report` into
    `/runs/{run_id}/{run_id}`.
    """
    if request.scope.get("route") is None:
        return UNMATCHED_ROUTE

    params: dict[str, object] = request.scope.get("path_params") or {}
    path = request.url.path
    if not params:
        return path

    names_by_value = {str(value): name for name, value in params.items()}
    substituted = [
        f"{{{names_by_value[segment]}}}" if segment in names_by_value else segment
        for segment in path.split("/")
    ]
    template = "/".join(substituted)

    # A `:path` parameter spans several segments, so it never matches one. Those
    # are replaced once, by value, after the segment pass has done the rest.
    for value, name in names_by_value.items():
        if value and f"{{{name}}}" not in template:
            template = template.replace(value, f"{{{name}}}", 1)
    return template


def _path_param(request: Request, name: str) -> str | None:
    """A matched path parameter, or `None` when the route did not declare it.

    Read from `path_params` rather than parsed out of the URL: the router has
    already decided what is an identifier and what is a literal segment, and
    re-deriving that here would be a second parser to keep in agreement.

    Bounded, because the router populates `path_params` when the *pattern*
    matches, which is before the annotated types (`RunId`, `ToolName`) have
    rejected anything. A request that is about to be refused with a 422 still
    reaches this line, so an identifier here is only as short as the caller
    chose to make it. Truncating marks the value as unusable rather than
    letting one request decide how long a log line is.
    """
    value = request.scope.get("path_params", {}).get(name)
    if value is None:
        return None
    text = str(value)
    if len(text) <= MAX_LOGGED_IDENTIFIER_CHARS:
        return text
    return text[:MAX_LOGGED_IDENTIFIER_CHARS] + TRUNCATION_MARKER
