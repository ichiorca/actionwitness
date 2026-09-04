"""`Origin` validation on mutating requests (§20.1, FR-005).

§20.1: "Serve frontend and FastAPI from the same origin" and "Validate the
`Origin` header on mutating API requests."

This is defence in depth, not the primary control. `SameSite=Strict` on the
workspace cookie already stops a cross-site page from sending it, so a
cross-site mutation arrives without a workspace and is refused before it reaches
a handler. `Origin` validation is the second lock, for the case where the first
one is weakened by a browser quirk, a proxy, or a future change to the cookie.

Three decisions, each easy to get subtly wrong:

**A mismatching `Origin` is always refused.** No prefix matching, no suffix
matching, no "starts with the configured host" — `https://harness.test` and
`https://harness.test.evil.example` differ, and a comparison that treats them as
related is the whole vulnerability. Origins are compared for equality after
normalization, using the same `_exact_origin` parsing the Shopify allowlist uses.

**A missing `Origin` is allowed.** Browsers send `Origin` on every mutating
request, same-origin ones included, so its absence means the request did not
come from a page — a CLI, a test, or an agent, none of which carry ambient
cookie authority the way a cross-site page does. Refusing them would break the
documented `actionwitness` CLI without closing anything: an attacker who can set
headers can also omit one, so a rule that trusts absence *less* than presence
only inconveniences honest clients.

**Reads are not checked.** §20.1 says "mutating API requests", and a GET that
changed state would be the bug to fix rather than a reason to widen this.

**One family of paths is served to a second origin, and only one.** §20.1's
Shopify clause: the bridge routes "allow CORS only from the single configured
development-store origin ... require the pairing bearer credential instead of the
harness cookie, omit credentialed-cookie CORS". A theme running on the store's
origin cannot present the harness origin, so a policy with one global allowlist
would refuse every bridge request — and the temptations then are to widen the
allowlist for everybody or to skip the check on those paths, both of which give
away the property this module exists to keep.

So the allowance is *scoped*: one extra origin, bound to one path prefix,
matched on segment boundaries. It widens nothing anywhere else, and the routes it
covers carry no ambient authority at all — they authorize by a bearer credential
the harness minted, never by the workspace cookie, so an origin allowance here
cannot be spent on somebody's session.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from starlette.requests import Request

from actionwitness_service.api.errors import ApiError, ApiErrorCode

__all__ = ["MUTATING_METHODS", "OriginPolicy"]

#: The methods §20.1 calls "mutating". `GET`, `HEAD`, and `OPTIONS` are absent
#: because they must not change state; if one ever does, the fix is the handler.
MUTATING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class OriginPolicy:
    """Decides whether a mutating request's `Origin` is acceptable."""

    def __init__(
        self,
        configured_origin: str | None = None,
        *,
        scoped_origins: Mapping[str, str] | None = None,
    ) -> None:
        """`configured_origin` is the operator's `HARNESS_PUBLIC_ORIGIN`.

        When it is absent — the documented local-development case — the request's
        own origin is used instead. That is not a weaker rule than it looks: the
        harness is served same-origin (§20.1), so a legitimate page's `Origin`
        equals the URL it is posting to, and a cross-site page's does not.

        `scoped_origins` maps a path prefix to the **one** additional origin
        permitted beneath it — §20.1's Shopify bridge clause, and nothing else.
        A mapping rather than a set because the pairing between path and origin is
        the whole safety property: an origin allowed everywhere would be an
        allowlist entry, and this is a door on one corridor.
        """
        self._configured = configured_origin
        self._scoped = {
            prefix.rstrip("/"): _normalize(origin)
            for prefix, origin in (scoped_origins or {}).items()
        }

    def check(self, request: Request) -> None:
        """Raise `ORIGIN_NOT_ALLOWED` if this mutation came from elsewhere."""
        if request.method not in MUTATING_METHODS:
            return

        presented = request.headers.get("origin")
        if presented is None:
            return

        if _normalize(presented) not in self._allowed(request):
            raise ApiError(
                ApiErrorCode.ORIGIN_NOT_ALLOWED,
                "This request did not come from an allowed origin.",
            )

    def scoped_origin_for(self, path: str) -> str | None:
        """The extra origin this path may be called from, if it has one.

        Exposed so a route can echo the same value in its CORS headers rather
        than deriving a second answer from the settings. Two derivations of one
        allowance is how a policy and the header advertising it come to disagree.
        """
        return next(
            (origin for prefix, origin in self._scoped.items() if _under(path, prefix)),
            None,
        )

    def scoped_cors_origin_for(self, request: Request) -> str | None:
        """Return the scoped origin only when this request presented it exactly."""
        scoped = self.scoped_origin_for(request.url.path)
        presented = request.headers.get("origin")
        if scoped is None or presented is None or _normalize(presented) != scoped:
            return None
        return scoped

    def _allowed(self, request: Request) -> frozenset[str]:
        base = (
            _normalize(self._configured)
            if self._configured is not None
            else _normalize(f"{request.url.scheme}://{request.url.netloc}")
        )
        scoped = self.scoped_origin_for(request.url.path)
        return frozenset({base} if scoped is None else {base, scoped})


def _under(path: str, prefix: str) -> bool:
    """Segment-boundary containment, never a bare `startswith`.

    The same trap `middleware._under` documents: a character-wise prefix test
    makes `/api/v1/shopifyX` and `/api/v1/shopify-admin` inherit an allowance
    nobody granted them.
    """
    return path == prefix or path.startswith(f"{prefix}/")


def _normalize(origin: str) -> str:
    """Lower-case `scheme://host[:port]`, with nothing else kept.

    A value that will not parse normalizes to itself, which then fails the
    equality check — `null`, `*`, and an empty string all take that path rather
    than needing a special case each.
    """
    from urllib.parse import urlsplit

    bare = origin.strip().lower()
    try:
        parsed = urlsplit(origin.strip())
        hostname, port_number = parsed.hostname, parsed.port
    except ValueError:
        # Both calls raise on input no browser sends but any client can write:
        # an unclosed IPv6 authority raises from `urlsplit` itself, while a
        # non-numeric or out-of-range port raises only when `.port` is *read*,
        # because splitting is lazy. This function's entire job is to decide a
        # refusal, so an unparseable origin takes the path the docstring already
        # describes — it normalizes to itself and fails the equality check —
        # rather than becoming a 500 from inside the middleware.
        return bare
    if parsed.scheme not in {"http", "https"} or not hostname:
        return bare
    port = f":{port_number}" if port_number else ""
    return f"{parsed.scheme.lower()}://{hostname.lower()}{port}"
