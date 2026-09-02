"""009-T5 — the security posture a deployed harness must actually have.

Spec §20.1's browser rules, checked against real responses from a real
application rather than against the settings object that is supposed to produce
them. The distinction matters: every one of these has a correct-looking
configuration path that silently does nothing at runtime, and a test that asserted
`settings.harness.secure_cookies is True` would pass for all of them.

BUILD_ORDER §7/M8 lists what this milestone verifies in the deployed
configuration: "production cookie attributes, origin policy, `Permissions-Policy`,
CORS, trusted-proxy handling, quotas, cleanup, and rollback behavior". Rollback is
an operator gate (009-T9); the rest are here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import create_app
from actionwitness_service.api.security_headers import SECURITY_HEADERS
from actionwitness_service.application.rate_limits import client_key
from fastapi import FastAPI

# `asyncio_mode = "auto"` marks the coroutine tests; an explicit `asyncio` mark
# here would also land on the two synchronous ones below and fail them.
pytestmark = [pytest.mark.integration]

DEPLOYED_ORIGIN = "https://actionwitness.example"

#: What Render's dashboard supplies (render.yaml: `HARNESS_PUBLIC_ORIGIN`,
#: `sync: false`), plus the environment marker that makes the cookie `Secure`.
PRODUCTION_ENV = {
    "HARNESS_ENV": "production",
    "HARNESS_PUBLIC_ORIGIN": DEPLOYED_ORIGIN,
    "BUGGY_STORE_ENABLED": "false",
}


@pytest.fixture
async def deployed(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(environ=PRODUCTION_ENV, database_path=tmp_path / "harness.sqlite3")
    async with application.router.lifespan_context(application):
        yield application


def visitor(app: FastAPI, *, origin: str = DEPLOYED_ORIGIN) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url=origin,
    )


# --- §20.1 headers ----------------------------------------------------------


@pytest.mark.parametrize("header,expected", sorted(SECURITY_HEADERS.items()))
async def test_every_response_carries_the_security_headers(
    deployed: FastAPI, header: str, expected: str
) -> None:
    async with visitor(deployed) as client:
        response = await client.get("/api/v1/workspace")

    assert response.headers.get(header) == expected


async def test_the_permissions_policy_is_the_specs_directive_verbatim(
    deployed: FastAPI,
) -> None:
    """§20.1: "Set `Permissions-Policy: tools=(self)`."

    Spelled out separately from the parametrized sweep because this one is a
    quoted requirement rather than ordinary hardening, and a change to it should
    read as a change to the specification.
    """
    async with visitor(deployed) as client:
        response = await client.get("/healthz")

    assert response.headers["Permissions-Policy"] == "tools=(self)"
    assert response.headers["Origin-Agent-Cluster"] == "?1", (
        "§20.1 forbids disabling origin isolation and asks for an explicit ?1"
    )


def _directives(policy: str) -> dict[str, list[str]]:
    """The policy as `{name: sources}`, so tests assert meaning rather than bytes."""
    parsed: dict[str, list[str]] = {}
    for directive in policy.split(";"):
        name, *sources = directive.split()
        parsed[name] = sources
    return parsed


async def test_the_content_security_policy_admits_no_inline_or_dynamic_code(
    deployed: FastAPI,
) -> None:
    """The two keywords whose absence is the whole point of shipping a policy.

    `'unsafe-inline'` re-admits injected `<script>` and injected `style=`;
    `'unsafe-eval'` re-admits a string compiled into code. Either would make the
    header decorative. Asserted against the served response rather than the
    constant, because a middleware that never runs also serves no policy.

    They are safe to forbid only because `tests/architecture/test_bundle_shape.py`
    holds the bundle to a shape that needs neither.
    """
    async with visitor(deployed) as client:
        response = await client.get("/api/v1/workspace")

    policy = response.headers["Content-Security-Policy"]

    assert "unsafe-inline" not in policy
    assert "unsafe-eval" not in policy
    assert _directives(policy)["default-src"] == ["'none'"], (
        "the policy no longer starts closed; a directive nobody listed is now allowed"
    )


async def test_the_content_security_policy_allows_only_this_origin(
    deployed: FastAPI,
) -> None:
    """Everything the page fetches is same-origin by construction (§29.1).

    A wildcard or a host name creeping into any of these is the moment the
    deployment starts trusting somebody else's server, so the check is that each
    directive names `'self'` and nothing but `'self'` — `data:` for images
    excepted, which cannot execute.
    """
    async with visitor(deployed) as client:
        response = await client.get("/api/v1/workspace")

    directives = _directives(response.headers["Content-Security-Policy"])

    assert directives["script-src"] == ["'self'"]
    assert directives["connect-src"] == ["'self'"]
    assert directives["style-src"] == ["'self'"]
    assert directives["font-src"] == ["'self'"]
    assert directives["img-src"] == ["'self'", "data:"]
    assert "*" not in response.headers["Content-Security-Policy"]


async def test_the_framing_rules_do_not_contradict_each_other(
    deployed: FastAPI,
) -> None:
    """`frame-ancestors` and `X-Frame-Options` both answer "who may frame this".

    A browser that honours the CSP form ignores the header form, so two rules
    that disagreed would mean the answer depended on the browser — and §20.1
    defers iframe embedding entirely, which is one answer, not two.
    """
    async with visitor(deployed) as client:
        response = await client.get("/healthz")

    assert _directives(response.headers["Content-Security-Policy"])["frame-ancestors"] == ["'none'"]
    assert response.headers["X-Frame-Options"] == "DENY"


async def test_a_refusal_built_in_middleware_still_carries_the_headers(
    deployed: FastAPI,
) -> None:
    """A 403 from the origin policy never reaches a route function.

    Responses built above the router are exactly the ones a per-route decorator
    would miss, and they are still rendered in a browser.
    """
    async with visitor(deployed) as client:
        response = await client.post(
            "/api/v1/contracts",
            headers={"Origin": "https://actionwitness.example.evil.test"},
            json={},
        )

    assert response.status_code == 403
    assert response.headers["Permissions-Policy"] == "tools=(self)"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


async def test_no_cors_headers_are_offered_to_a_cross_origin_caller(
    deployed: FastAPI,
) -> None:
    """§20.1 allows CORS for the Shopify bridge alone, and that module is cut.

    Asserted rather than assumed: adding `CORSMiddleware` "so the frontend works"
    is a one-line change that would hand every origin read access to a workspace's
    evidence, and nothing else in the suite would notice.
    """
    async with visitor(deployed) as client:
        response = await client.get(
            "/api/v1/workspace", headers={"Origin": "https://somewhere.else.test"}
        )

    offered = {
        name.lower() for name in response.headers if name.lower().startswith("access-control-")
    }
    assert offered == set(), f"the service offered CORS headers: {sorted(offered)}"


# --- §20.1 cookie attributes -------------------------------------------------


async def test_the_production_workspace_cookie_is_secure_httponly_and_strict(
    deployed: FastAPI,
) -> None:
    async with visitor(deployed) as client:
        response = await client.get("/api/v1/workspace")

    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


async def test_local_development_omits_only_the_secure_attribute(
    tmp_path: Path,
) -> None:
    """FR-005: documented local HTTP development may omit **only** `Secure`."""
    application = create_app(
        environ={"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"},
        database_path=tmp_path / "harness.sqlite3",
    )
    async with (
        application.router.lifespan_context(application),
        visitor(application, origin="http://localhost:8000") as client,
    ):
        response = await client.get("/api/v1/workspace")

    cookie = response.headers["set-cookie"].lower()
    assert "secure" not in cookie
    assert "httponly" in cookie, "HttpOnly is unconditional"
    assert "samesite=strict" in cookie, "SameSite=Strict is unconditional"


# --- §20.1 origin policy, keyed off the deployed origin ----------------------


async def test_a_mutation_from_the_deployed_origin_is_allowed(deployed: FastAPI) -> None:
    async with visitor(deployed) as client:
        response = await client.post(
            "/api/v1/contracts", headers={"Origin": DEPLOYED_ORIGIN}, json={}
        )

    assert response.status_code != 403


async def test_a_mistyped_public_origin_falls_back_to_the_request_origin(
    tmp_path: Path,
) -> None:
    """A value that will not parse is dropped, never half-accepted.

    This is the deployment mistake the health endpoint reports, and the reason it
    reports it: the service comes up healthy and refuses nothing obvious, so the
    only visible symptom is mutations failing from the real site.
    """
    application = create_app(
        environ={**PRODUCTION_ENV, "HARNESS_PUBLIC_ORIGIN": "not an origin"},
        database_path=tmp_path / "harness.sqlite3",
    )
    async with application.router.lifespan_context(application), visitor(application) as client:
        health = await client.get("/healthz")
        refused = await client.post(
            "/api/v1/contracts", headers={"Origin": "https://elsewhere.test"}, json={}
        )

    assert health.json()["public_origin"] is None, (
        "an unparseable origin must be reported as absent, not echoed back"
    )
    assert refused.status_code == 403


async def test_a_production_deployment_without_a_public_origin_is_not_ready(
    tmp_path: Path,
) -> None:
    """§20.1: in production, an unset origin is a weakened policy, not a default.

    With no `HARNESS_PUBLIC_ORIGIN` the policy compares each mutation against the
    origin the *request itself* presents — correct for documented local
    development, and in a deployed service equivalent to accepting whatever the
    caller claims. Reported through readiness rather than a refusal to start, so
    a bad value holds the new deploy back instead of taking the service down.
    """
    # Arrange — production, with the one variable that matters left out.
    environ = dict(PRODUCTION_ENV)
    environ.pop("HARNESS_PUBLIC_ORIGIN", None)
    application = create_app(environ=environ, database_path=tmp_path / "harness.sqlite3")

    # Act
    async with application.router.lifespan_context(application), visitor(application) as client:
        response = await client.get("/healthz")

    # Assert
    assert response.status_code == 503, "a deployed service with no origin policy is not ready"
    assert response.json()["origin_policy"] == "unconfigured"
    assert response.json()["status"] == "degraded"


async def test_a_local_deployment_without_a_public_origin_stays_ready(tmp_path: Path) -> None:
    """The counterpart, and the reason the check is environment-scoped.

    Local development is documented as running without a configured origin. If
    the check were unconditional, every developer's first run would report an
    unhealthy service.
    """
    # Arrange / Act
    application = create_app(
        environ={"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"},
        database_path=tmp_path / "harness.sqlite3",
    )
    async with application.router.lifespan_context(application), visitor(application) as client:
        response = await client.get("/healthz")

    # Assert
    assert response.status_code == 200
    assert response.json()["origin_policy"] == "configured"


async def test_the_health_check_reports_an_unreadable_database_as_degraded(
    tmp_path: Path,
) -> None:
    """§29.1's health check has to mean something after startup.

    The schema version is captured once at startup, so an endpoint built only
    from it answers `ok` for the life of the process — including after the
    database file has been deleted underneath it. Both Render's health check and
    the Docker `HEALTHCHECK` read this endpoint, so an instance whose source of
    truth had vanished would stay in service, serving 500s.
    """
    # Arrange — a service that started healthy, then lost its schema.
    application = create_app(environ=PRODUCTION_ENV, database_path=tmp_path / "harness.sqlite3")
    async with application.router.lifespan_context(application), visitor(application) as client:
        healthy = await client.get("/healthz")

        # Act — the harness schema stops being readable while the process keeps
        # running. Dropping the table rather than deleting the file: SQLite
        # silently creates an empty database for a missing path, so a deleted
        # file and a wrecked one arrive at the same place, and this one is the
        # form the assertion can actually observe on every platform.
        async with application.state.database.transaction() as work:
            await work.execute("DROP TABLE workspaces")
        degraded = await client.get("/healthz")

    # Assert
    assert healthy.status_code == 200
    assert healthy.json()["database"] == "ok"
    assert degraded.status_code == 503, "a dead database must not read as a healthy instance"
    assert degraded.json()["database"] == "unavailable"
    assert degraded.json()["status"] == "degraded"


# --- §20.1 trusted-proxy handling --------------------------------------------
#
# The only two checks in this file that call a function rather than send a
# request, and deliberately so: `ASGITransport` supplies no client address, so
# every in-process request keys as the same anonymous peer. Driving these through
# HTTP would produce two tests that passed without ever distinguishing a peer
# from a forwarded address — which is the entire behaviour under test.


def test_an_untrusted_forwarding_header_is_ignored() -> None:
    """§20.1: "never trust an arbitrary client-supplied forwarding header".

    A rate limiter that believed `X-Forwarded-For` from any peer would let one
    client rotate the header and spend everybody's allowance — the limit would
    look configured and enforce nothing.
    """
    spoofed = client_key("203.0.113.9", "198.51.100.1", trusted_proxies=frozenset())
    direct = client_key("203.0.113.9", None, trusted_proxies=frozenset())

    assert spoofed == direct


def test_a_trusted_proxys_forwarding_header_is_believed() -> None:
    """The platform sits in front of this service, so the peer is always the proxy."""
    forwarded = client_key("10.0.0.7", "198.51.100.1", trusted_proxies=frozenset({"10.0.0.7"}))
    other = client_key("10.0.0.7", "198.51.100.2", trusted_proxies=frozenset({"10.0.0.7"}))

    assert forwarded != other, "two clients behind one trusted proxy must not share a bucket"


# --- health ------------------------------------------------------------------


async def test_health_reports_the_configured_origin_and_no_secret(
    tmp_path: Path,
) -> None:
    """§29.1: credentials are never in the health response.

    The origin is operator-supplied configuration, not a credential, and it is the
    one value whose misconfiguration is otherwise invisible. Everything that *is*
    a secret is checked for absence here, including a Shopify credential the
    module would have consumed.
    """
    application = create_app(
        environ={
            **PRODUCTION_ENV,
            "LIVE_EVALUATOR_ENABLED": "true",
            "LIVE_EVALUATOR_PROVIDER": "anthropic",
            "LIVE_EVALUATOR_MODEL": "claude-opus-5",
            "LIVE_EVALUATOR_CREDENTIAL_VAR": "MODEL_API_KEY",
            "MODEL_API_KEY": "sk-not-a-real-credential",
        },
        database_path=tmp_path / "harness.sqlite3",
    )
    async with application.router.lifespan_context(application), visitor(application) as client:
        response = await client.get("/healthz")

    body = response.text
    assert response.json()["status"] == "ok"
    assert response.json()["public_origin"] == DEPLOYED_ORIGIN
    assert "sk-not-a-real-credential" not in body
    assert "MODEL_API_KEY" not in body
