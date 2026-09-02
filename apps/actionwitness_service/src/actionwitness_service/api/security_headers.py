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

**`Content-Security-Policy`**, and the reason it took until now. The 009
deviations recorded it as an open item with a specific objection: a policy is
easy to write and easy to get subtly wrong "against a build whose output shape is
not asserted anywhere", and an untested CSP is a header that breaks the page on a
Friday. The objection was about the *absence of a gate*, not about the policy —
so the gate came first. `tests/architecture/test_bundle_shape.py` asserts what
`CONTENT_SECURITY_POLICY` assumes: no inline script, no inline style, no
`style={{}}` attribute, no CSS-in-JS, no `eval`, and no off-origin asset in
either frontend. The policy below is safe precisely as long as that test passes,
and it fails the moment somebody adds the thing that would have broken the page.

The policy itself starts from `default-src 'none'` and names what is actually
used, which today is very little: same-origin module scripts, same-origin fetch
and `EventSource`, and nothing else. `form-action` is `'self'` rather than
`'none'` because the declarative contract form is a real form element;
`frame-ancestors 'none'` restates `X-Frame-Options` for browsers that prefer it,
and the two must not disagree.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

__all__ = ["CONTENT_SECURITY_POLICY", "SECURITY_HEADERS", "SecurityHeadersMiddleware"]

#: A strict policy, written against a bundle whose shape is asserted.
#:
#: Every directive is either "the thing the page genuinely does" or `'none'`.
#: There is no `'unsafe-inline'` and no `'unsafe-eval'`: the frontends contain no
#: inline script, no inline style, no stylesheet at all, and no dynamic code, and
#: `tests/architecture/test_bundle_shape.py` is what keeps that true.
#:
#: `img-src` admits `data:` because a data-URI image cannot execute and a favicon
#: or an inlined icon is the one asset likely to arrive that way later. Every
#: other fetch — script, style, font, frame, worker, object — falls through to
#: `default-src 'none'`.
CONTENT_SECURITY_POLICY: Final[str] = "; ".join(
    (
        "default-src 'none'",
        # The Vite bundle: hashed module scripts under /assets and /demo/assets.
        "script-src 'self'",
        # A stylesheet if one is ever added. Never inline.
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        # /api/v1, /demo/api/v1, and the run timeline's EventSource. All same-origin
        # by construction (§29.1), so this needs no exception.
        "connect-src 'self'",
        # The declarative contract form is a real form; it must not be able to
        # post anywhere but here.
        "form-action 'self'",
        # Nothing may frame this, restating X-Frame-Options for browsers that
        # prefer the CSP form. The two must agree.
        "frame-ancestors 'none'",
        # No <base> injection can retarget a relative asset path.
        "base-uri 'none'",
        # Redundant under default-src 'none', stated because it is the directive
        # a reader looks for and older browsers do not all fall through.
        "object-src 'none'",
    )
)

SECURITY_HEADERS: Final[Mapping[str, str]] = {
    # Ordinary hardening rather than a §20.1 directive; see the module docstring
    # for why it ships now and did not ship in 009.
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
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
