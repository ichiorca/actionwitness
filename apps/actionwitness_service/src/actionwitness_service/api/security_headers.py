"""Response headers required of a deployed harness (spec §20.1; 009-T5).

Two of these are named in the specification verbatim and the rest are the
ordinary hardening that a public deployment of a page which runs agent-supplied
tools should not ship without.

**`Permissions-Policy: tools=(self)`** (§20.1). The harness page registers WebMCP
tools; this says only this origin may. Without it, an embedded third-party frame
inherits the capability, and §20.1 defers cross-origin iframe support entirely —
so the policy states the deferral rather than relying on nobody having added a
frame yet.

**`Origin-Agent-Cluster: ?1`** (§20.1: "Do not disable origin isolation with
`?0`; explicitly use `?1` if supported"). Explicit, because the default is a
browser decision that can change.

**No CORS.** §20.1 allows cross-origin access for exactly one thing — the Shopify
bridge routes, from the single configured development-store origin — and that
module is not mounted in this deployment (see the Tier 3 cut, 009-T12). Every
route this service exposes is same-origin by construction (§29.1 serves the
frontend and the API from one origin), so there is no `CORSMiddleware` here and
adding one "just in case" would be handing out access nobody asked for. A
cross-origin caller gets the browser's default refusal, which is the correct
answer.

`Content-Security-Policy` is deliberately absent for now: the workspace UI is a
Vite bundle with hashed assets and no inline script, so a policy would be easy to
write and easy to get subtly wrong against a build whose output shape is not
asserted anywhere. Recorded as an open item in the 009 deviations rather than
shipped untested.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["SECURITY_HEADERS", "SecurityHeadersMiddleware"]

SECURITY_HEADERS: Final[Mapping[str, str]] = {
    # §20.1, verbatim.
    "Permissions-Policy": "tools=(self)",
    # §20.1: explicitly `?1`, never `?0`.
    "Origin-Agent-Cluster": "?1",
    # A JSON error envelope sniffed as HTML is an XSS vector; the harness renders
    # untrusted evaluator reports and tool output, so content type is not a hint.
    "X-Content-Type-Options": "nosniff",
    # §20.1 defers iframe embedding. Until an `allow="tools"` policy exists, the
    # honest setting is that nothing may frame this.
    "X-Frame-Options": "DENY",
    # Workspace and run identifiers appear in paths. They are not secrets, but
    # they are also nobody else's business.
    "Referrer-Policy": "same-origin",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Applies `SECURITY_HEADERS` to every response, including error envelopes.

    Set here rather than per route, because the responses most likely to be
    forgotten are the ones built by exception handlers and by the middleware
    above this one — a 429 from the rate limiter never reaches a route function.

    Existing values are not overwritten. Nothing sets these today; if a route
    ever needs a narrower policy of its own, the narrower one should win rather
    than be silently replaced by the default.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name not in response.headers:
                response.headers[name] = value
        return response
