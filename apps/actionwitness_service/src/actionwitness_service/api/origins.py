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
"""

from __future__ import annotations

from typing import Final

from starlette.requests import Request

from actionwitness_service.api.errors import ApiError, ApiErrorCode

__all__ = ["MUTATING_METHODS", "OriginPolicy"]

#: The methods §20.1 calls "mutating". `GET`, `HEAD`, and `OPTIONS` are absent
#: because they must not change state; if one ever does, the fix is the handler.
MUTATING_METHODS: Final = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class OriginPolicy:
    """Decides whether a mutating request's `Origin` is acceptable."""

    def __init__(self, configured_origin: str | None = None) -> None:
        """`configured_origin` is the operator's `HARNESS_PUBLIC_ORIGIN`.

        When it is absent — the documented local-development case — the request's
        own origin is used instead. That is not a weaker rule than it looks: the
        harness is served same-origin (§20.1), so a legitimate page's `Origin`
        equals the URL it is posting to, and a cross-site page's does not.
        """
        self._configured = configured_origin

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

    def _allowed(self, request: Request) -> frozenset[str]:
        if self._configured is not None:
            return frozenset({_normalize(self._configured)})
        return frozenset({_normalize(f"{request.url.scheme}://{request.url.netloc}")})


def _normalize(origin: str) -> str:
    """Lower-case `scheme://host[:port]`, with nothing else kept.

    A value that will not parse normalizes to itself, which then fails the
    equality check — `null`, `*`, and an empty string all take that path rather
    than needing a special case each.
    """
    from urllib.parse import urlsplit

    parsed = urlsplit(origin.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return origin.strip().lower()
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"
