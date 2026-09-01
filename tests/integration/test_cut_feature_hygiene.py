"""009-T12 — a feature that is not shipping is not half-shipped.

BUILD_ORDER's M11 rule and constitution §8: every cut feature's control and tool
registration is removed or visibly disabled, and product copy claims nothing
unshipped.

The failure this guards against is specific and common at the end of a project.
A Tier 3 module gets built far enough to have a route, a button, and a paragraph
in the README, then does not land. The route stays mounted and answers 500, the
button stays on screen and does nothing, and the README still says the feature
exists. Each of those is individually easy to miss and collectively reads as a
product that does not work.

The invariant asserted here is deliberately mechanical rather than a list of
which features were cut — that is an operator decision, and one that can change
up to the submission. What must hold either way: **a module the deployment
reports as unavailable must be unavailable everywhere.** No mounted route, no
frontend control, no claim in the copy. A module that ships flips its own state
and these tests follow it.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import create_app
from actionwitness_service.config import MODULE_NAMES, ModuleStatus, ServiceSettings
from fastapi import FastAPI

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
README = REPO_ROOT / "README.md"
FRONTEND_SRC = REPO_ROOT / "apps" / "actionwitness_service" / "frontend" / "src"

#: The environment the public judging deployment runs with: `render.yaml` sets
#: only `HARNESS_PUBLIC_ORIGIN` and leaves the Tier 3 variables unset
#: (`sync: false` with no value). Everything else takes its default.
JUDGING_ENV = {
    "HARNESS_ENV": "production",
    "HARNESS_PUBLIC_ORIGIN": "https://actionwitness.example",
}

#: Route prefixes owned by an optional module. A module that is off must not have
#: one mounted — §21.1 wants a named unavailable state, and a route that 500s
#: because its settings are `None` is not that.
MODULE_ROUTE_PREFIXES = {
    "shopify": "/api/v1/shopify",
    "external_audit": "/api/v1/audit",
}

#: Claims a README acquires while a feature is still expected to land, and that
#: nobody re-reads once it does not (constitution §8: no claim of unverified
#: adoption, harm, or protection).
#:
#: Patterns rather than substrings, and the first entry is why: a bare
#: `"trusted by" in copy` also matches "*un*trusted by construction", and a bare
#: "used by" matches "the fixture used by the import tests". Both are accurate
#: sentences in this repository's own README, and a gate that fails on accurate
#: prose is a gate somebody switches off.
UNVERIFIABLE_CLAIMS: dict[str, re.Pattern[str]] = {
    "adoption claim": re.compile(
        r"\btrusted by\b|\bused by (?:\d|thousands|hundreds|millions|many|"
        r"companies|teams|developers|engineers)"
    ),
    "harm-prevention claim": re.compile(r"\b(?:prevents|eliminates|stops) (?:all|every|any)\b"),
    "unverified maturity claim": re.compile(r"\bproduction[- ]ready\b|\bbattle[- ]tested\b"),
    "absolute guarantee": re.compile(r"\bguarantee(?:s|d)? (?:that )?(?:no|every|all)\b"),
}


def _claims_in(copy: str) -> list[str]:
    """Which kinds of unverifiable claim this text makes, if any."""
    lowered = copy.lower()
    return sorted(name for name, pattern in UNVERIFIABLE_CLAIMS.items() if pattern.search(lowered))


@pytest.fixture
async def judged(tmp_path: Path) -> AsyncIterator[FastAPI]:
    application = create_app(environ=JUDGING_ENV, database_path=tmp_path / "harness.sqlite3")
    async with application.router.lifespan_context(application):
        yield application


def _settings() -> ServiceSettings:
    return ServiceSettings.from_env(JUDGING_ENV)


def _disabled_modules() -> list[str]:
    settings = _settings()
    return [name for name in MODULE_NAMES if not settings.is_enabled(name)]


def test_the_judging_deployment_has_something_switched_off() -> None:
    """The guard on every test below: if everything shipped, they prove nothing."""
    assert _disabled_modules(), (
        "no module is disabled in the judging configuration, so the cut-hygiene "
        "tests below are vacuous — delete them or fix the fixture"
    )


def test_every_disabled_module_says_why_rather_than_going_quiet() -> None:
    """§21.1: an absent module produces setup guidance, never a silent absence.

    `misconfigured` stays distinct from `disabled` here too. An operator who
    mistyped a store origin needs to see a mistake, not a deliberate cut.
    """
    settings = _settings()
    for name in _disabled_modules():
        state = settings.module(name)
        assert state.status in {ModuleStatus.DISABLED, ModuleStatus.MISCONFIGURED}
        assert state.reason.strip(), f"module {name} is off and says nothing about why"


async def test_no_disabled_module_leaves_a_route_mounted(judged: FastAPI) -> None:
    """A mounted route for an absent module is the "half-shipped" failure itself.

    The Shopify router exists in the tree as an unmounted scaffold. That is the
    correct state for a feature that has not landed, and this is what keeps it
    from being mounted "just to see it" and then forgotten.
    """
    mounted = {getattr(route, "path", "") for route in judged.routes}
    for name in _disabled_modules():
        prefix = MODULE_ROUTE_PREFIXES.get(name)
        if prefix is None:
            continue
        offending = sorted(path for path in mounted if path.startswith(prefix))
        assert offending == [], f"module {name} is disabled but still serves {offending}"


async def test_the_capability_surface_reports_every_module(judged: FastAPI) -> None:
    """Visibly disabled, not invisibly absent.

    "Removed or visibly disabled" is an either/or, and the harness chose visible:
    a judge should be able to see that the Shopify module exists and is off,
    rather than wonder whether the feature was ever real.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=judged, raise_app_exceptions=False),
        base_url="https://actionwitness.example",
    ) as client:
        response = await client.get("/api/v1/workspace")

    reported = response.json()["modules"]
    assert set(reported) == set(MODULE_NAMES), (
        f"the module surface omits {sorted(set(MODULE_NAMES) - set(reported))}"
    )
    for name in _disabled_modules():
        assert reported[name]["status"] in {"disabled", "misconfigured"}
        assert reported[name]["reason"].strip(), f"{name} is off and says nothing about why"


async def test_the_module_surface_publishes_no_credential(judged: FastAPI) -> None:
    """A settings object shown to a client must be safe to show.

    It is, by construction rather than by filtering: `config` records the *name*
    of a credential variable and never its value. Asserted anyway, because that
    property is one careless `reason=f"...{value}"` away from being false, and the
    module surface is the place it would surface.
    """
    application = create_app(
        environ={
            **JUDGING_ENV,
            "LIVE_EVALUATOR_ENABLED": "true",
            "LIVE_EVALUATOR_PROVIDER": "anthropic",
            "LIVE_EVALUATOR_MODEL": "claude-opus-5",
            "LIVE_EVALUATOR_CREDENTIAL_VAR": "MODEL_API_KEY",
            "MODEL_API_KEY": "sk-not-a-real-credential-value",
        },
        database_path=Path(judged.state.settings.harness.database_path).parent / "second.sqlite3",
    )
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://actionwitness.example",
        ) as client,
    ):
        response = await client.get("/api/v1/workspace")

    assert "sk-not-a-real-credential-value" not in response.text


def test_no_frontend_control_registers_a_tool_for_a_disabled_module() -> None:
    """A button that calls a route nobody mounted is worse than no button.

    Scans shipped sources only. Tests and the generated name registry legitimately
    mention every module — the registry is the shared vocabulary of error codes and
    exists precisely so names cannot fork — and scanning them would flag the
    vocabulary as the violation.
    """
    shipped = [
        path
        for path in FRONTEND_SRC.rglob("*.ts*")
        if ".test." not in path.name
        and "generated" not in path.parts
        and "test" not in path.relative_to(FRONTEND_SRC).parts
    ]
    assert shipped, "the source scan found no files, so it proves nothing"

    offenders: list[str] = []
    for name in _disabled_modules():
        for path in shipped:
            text = path.read_text(encoding="utf-8")
            # A comment explaining the module's absence is not a control.
            code = "\n".join(
                line for line in text.splitlines() if not line.strip().startswith(("*", "//", "/*"))
            )
            if re.search(rf"\b{re.escape(name)}\b", code):
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {name}")

    assert offenders == [], f"shipped UI code references disabled modules: {offenders}"


def test_product_copy_claims_nothing_unshipped() -> None:
    """Constitution §8: no claim of unverified adoption, harm, or protection.

    The specific phrasings below are the ones a README acquires while a feature is
    still expected to land, and that nobody re-reads once it does not.
    """
    present = _claims_in(README.read_text(encoding="utf-8"))
    assert present == [], f"the README makes unverifiable claims: {present}"


@pytest.mark.parametrize(
    "claim",
    [
        "Trusted by 40 engineering teams.",
        "Used by thousands of developers.",
        "ActionWitness prevents all silent tool failures.",
        "A production-ready assurance harness.",
        "It guarantees no false success reaches your users.",
    ],
)
def test_the_claim_check_catches_a_real_marketing_claim(claim: str) -> None:
    """The test above passes trivially if its patterns match nothing.

    Each of these is a sentence a README acquires late and nobody re-reads, so the
    gate is exercised against text it must reject rather than only against text
    that happens to be clean today.
    """
    assert _claims_in(claim), f"the gate would not have caught: {claim!r}"


def test_the_claim_check_does_not_flag_accurate_prose() -> None:
    """The other half: a gate that cries wolf is a gate somebody deletes.

    Both phrases below are real sentences from this README. A substring check for
    "trusted by" matches "*un*trusted by construction", and one for "used by"
    matches a fixture credit — which is how a well-meaning gate ends up switched
    off.
    """
    accurate = (
        "Untrusted by construction: an imported report is validated before parsing. "
        "The checked-in fixture is used by the import and correlation tests."
    )
    assert _claims_in(accurate) == []


def test_the_readme_marks_every_tier_three_module_as_optional() -> None:
    """A reader must not conclude a disabled module is part of the demo path.

    Named modules are fine — hiding them would be its own kind of dishonesty — but
    each has to appear near the word that says it is off.
    """
    copy = README.read_text(encoding="utf-8")
    for name in _disabled_modules():
        for line in copy.splitlines():
            if name in line or name.replace("_", " ") in line.lower():
                break
        else:
            continue
        section = "\n".join(
            line for line in copy.splitlines() if name in line or name.replace("_", " ") in line
        ).lower()
        assert any(
            marker in section for marker in ("optional", "disabled", "not shipped", "tier 3", "off")
        ), f"the README names {name} without saying it is optional or off"
