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
from actionwitness_service.api.app import API_PREFIX, create_app
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

#: External audit deliberately keeps a read-only status route and mutation
#: refusals mounted while no audit is authorized. Dedicated route tests assert
#: those writes fail with `AUDIT_NOT_AUTHORIZED` rather than disappearing.
VISIBLE_DISABLED_ROUTE_MODULES = frozenset({"external_audit"})

#: Only these two files may name Shopify when the module is disabled. They are
#: the typed API boundary and the panel that renders the server's disabled state;
#: the browser E2E suite asserts that state exposes no pairing action.
DISABLED_MODULE_FRONTEND_BOUNDARIES = frozenset(
    {
        ("apps/actionwitness_service/frontend/src/api/shopify.ts", "shopify"),
        (
            "apps/actionwitness_service/frontend/src/components/ShopifyPairingPanel.tsx",
            "shopify",
        ),
    }
)

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


async def test_disabled_module_routes_follow_their_declared_visibility_policy(
    judged: FastAPI,
) -> None:
    """Disabled modules are absent unless their contract is an explicit refusal surface.

    Inspect the public OpenAPI contract rather than FastAPI's internal route
    container: recent FastAPI versions keep included routers nested, so reading
    ``judged.routes`` can silently miss an endpoint that is actually served.
    """
    mounted = set(judged.openapi()["paths"])
    for name in _disabled_modules():
        prefix = MODULE_ROUTE_PREFIXES.get(name)
        if prefix is None:
            continue
        matching = sorted(path for path in mounted if path.startswith(prefix))
        if name in VISIBLE_DISABLED_ROUTE_MODULES:
            assert matching, f"module {name} declares a visible refusal surface but has none"
        else:
            assert matching == [], f"module {name} is disabled but still serves {matching}"


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

    **A line that reads the module's own reported state is exempt, and that is
    the rule rather than a hole in it.** §21.1 asks a cut feature to be "removed
    or *visibly disabled*", and `test_the_capability_surface_reports_every_module`
    below exists because this project chose visible. The UI cannot render "this
    module is off, and here is why" without naming the module it is reporting on,
    so a mention alongside `modules` — the map the server publishes for exactly
    this purpose — is the mechanism working. A mention anywhere else is still a
    control for something that is not there, which is what this test is for.
    """
    shipped = [
        path
        for path in FRONTEND_SRC.rglob("*.ts*")
        if ".test." not in path.name
        and "generated" not in path.parts
        and "test" not in path.relative_to(FRONTEND_SRC).parts
    ]
    assert shipped, "the source scan found no files, so it proves nothing"
    for relative_path, _module in DISABLED_MODULE_FRONTEND_BOUNDARIES:
        assert (REPO_ROOT / relative_path).is_file(), f"stale frontend exception: {relative_path}"

    offenders: list[str] = []
    for name in _disabled_modules():
        for path in shipped:
            relative_path = path.relative_to(REPO_ROOT).as_posix()
            if (relative_path, name) in DISABLED_MODULE_FRONTEND_BOUNDARIES:
                continue
            text = path.read_text(encoding="utf-8")
            # A comment explaining the module's absence is not a control.
            code = "\n".join(
                line
                for line in text.splitlines()
                if not line.strip().startswith(("*", "//", "/*"))
                # Reading the published module report is how the UI says a
                # feature is off. A name on such a line is a status lookup, not
                # an affordance; a name on any other line still fails below.
                and "modules" not in line
            )
            if re.search(rf"\b{re.escape(name)}\b", code):
                offenders.append(f"{relative_path} -> {name}")

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


# --- cut fault profiles (012-T8) ---------------------------------------------
#
# 009-T12's tests above are about optional *modules* — a Shopify integration
# that is off, an audit surface that is not mounted. M11 cuts a different kind
# of thing: a fault profile that is recognised, described, and not built.
#
# `checkout_without_confirmation` is 012's only cut (plan.md D1: the harness's
# confirmation gate and the `requires_confirmation` policy read the same
# contract policy, so no store-side fault can reach AC-07's classification).
# The hygiene question is the one this file already asks of a module — is it
# actually unavailable everywhere, or only unavailable where somebody
# remembered?
#
# The dangerous shape is specific. A cut profile silently downgraded to `none`
# would leave a run whose report named an active fault and whose store behaved
# honestly — the harness stating a defect was injected while nothing was. That
# is the demo lying about the one thing it exists to show, and it is worse than
# a refusal by exactly the margin that makes this product necessary.


def _unimplemented_profiles() -> list[str]:
    from buggy_store.failure_injection import IMPLEMENTED_PROFILES, FaultProfile

    return sorted(item.value for item in FaultProfile if item not in IMPLEMENTED_PROFILES)


@pytest.fixture
async def demo(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The harness composed with the demo store, as an operator would run it."""
    from buggy_store.api import create_app as create_store

    store = create_store(database_path=tmp_path / "store.sqlite3")
    async with (
        store.router.lifespan_context(store),
        httpx.ASGITransport(app=store) as asgi,
        httpx.AsyncClient(transport=asgi, base_url="http://buggy-store.test") as target_client,
    ):
        harness = create_app(
            environ={
                "HARNESS_ENV": "local",
                "BUGGY_STORE_ENABLED": "true",
                "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
            },
            database_path=tmp_path / "harness.sqlite3",
            target_client=target_client,
        )
        async with (
            harness.router.lifespan_context(harness),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=harness, raise_app_exceptions=False),
                base_url="https://harness.test",
            ) as client,
        ):
            yield client


def test_some_fault_profile_is_still_cut() -> None:
    """The guard on the checks below: if everything shipped, they prove nothing.

    This is expected to go vacuous eventually, and it fails loudly at that point
    rather than leaving several tests quietly asserting nothing about an empty
    set.
    """
    assert _unimplemented_profiles(), (
        "every fault profile is implemented, so the cut-profile tests below are "
        "vacuous — delete them or fix this guard"
    )


def test_every_cut_profile_is_still_named_and_described() -> None:
    """Removed *or visibly disabled*, and this project chose visible.

    §13.3 names six profiles. Deleting a cut one from the enum would make the
    demo look as though it never intended the behaviour, and would silently
    narrow the vocabulary reports are written in. It stays recognised and
    described, which is what lets the refusal below say something useful.
    """
    from buggy_store.failure_injection import PROFILE_DESCRIPTIONS, FaultProfile

    for value in _unimplemented_profiles():
        assert PROFILE_DESCRIPTIONS[FaultProfile(value)].strip(), (
            f"{value} is cut and describes nothing"
        )


async def _select_demo_contract(demo: httpx.AsyncClient) -> None:
    templates = (await demo.get(f"{API_PREFIX}/contracts/templates")).json()["templates"]
    chosen = next(
        item for item in templates if item["source_template_id"] == "one_mug_save20_no_checkout"
    )
    await demo.post(f"{API_PREFIX}/contracts/{chosen['contract_id']}/select")


@pytest.mark.parametrize("profile", _unimplemented_profiles())
async def test_selecting_a_cut_profile_is_refused_by_name(
    demo: httpx.AsyncClient, profile: str
) -> None:
    """Refused as soon as the answer is knowable, with a reason, and not as a 500.

    A 500 carries the same information — "it did not work" — as a fault in the
    harness rather than a deliberate limit of the build, and an operator would
    reasonably file a bug against the wrong thing.

    A contract is selected first because that is what selects the target, and
    only a target can say which faults it injects. The other order — profile
    before contract, which FR-011 allows — cannot be answered here and is
    refused at arming instead; `test_a_cut_profile_can_never_reach_a_run`
    covers it.
    """
    # Arrange
    await _select_demo_contract(demo)

    # Act
    refused = await demo.put(
        f"{API_PREFIX}/workspace/failure-profile", json={"failure_profile": profile}
    )

    # Assert
    assert 400 <= refused.status_code < 500, refused.text
    assert profile in refused.text


@pytest.mark.parametrize("profile", _unimplemented_profiles())
async def test_a_cut_profile_never_becomes_the_recorded_selection(
    demo: httpx.AsyncClient, profile: str
) -> None:
    """Refused, and not recorded either — the failure mode that matters.

    If the refusal came back but the workspace kept the selection, a later run
    would be armed against a fault nothing injects, and its report would name an
    active defect while the store behaved honestly. The harness would be making
    exactly the false claim it exists to catch.
    """
    # Arrange
    await _select_demo_contract(demo)
    before = (await demo.get(f"{API_PREFIX}/workspace")).json()["failure_profile"]

    # Act
    await demo.put(f"{API_PREFIX}/workspace/failure-profile", json={"failure_profile": profile})

    # Assert
    after = (await demo.get(f"{API_PREFIX}/workspace")).json()["failure_profile"]
    assert after == before
    assert after != profile


@pytest.mark.parametrize("profile", _unimplemented_profiles())
async def test_a_cut_profile_can_never_reach_a_run(demo: httpx.AsyncClient, profile: str) -> None:
    """The last gate, and the one that closes the order-dependent hole.

    FR-011 lets a profile be chosen before a contract, so the target that would
    have to inject it may not exist yet — and preparation, which is what asks
    the target, is skipped when there is nothing to prepare. That left a
    workspace holding a profile nothing could produce, and arming copied it
    straight into the run.

    A run is where a profile becomes evidence. Refused here, the report can
    never name an active defect the store did not inject.
    """
    # Arrange — select the profile first, then a contract, exactly as the
    # order-dependent path did.
    await demo.put(f"{API_PREFIX}/workspace/failure-profile", json={"failure_profile": profile})
    await _select_demo_contract(demo)

    # Act
    armed = await demo.post(f"{API_PREFIX}/runs")

    # Assert
    assert 400 <= armed.status_code < 500, armed.text
    assert profile in armed.text
    assert (await demo.get(f"{API_PREFIX}/workspace")).json()[
        "activeRun" if False else "active_run"
    ] is None


@pytest.mark.parametrize("profile", _unimplemented_profiles())
async def test_the_store_refuses_a_cut_profile_rather_than_running_the_honest_path(
    demo: httpx.AsyncClient, profile: str
) -> None:
    """The store's own promise, asked directly.

    The harness could otherwise be hiding a store that accepted the selection
    and quietly behaved correctly under it. A `200` here would mean the demo
    took a fault it does not inject.
    """
    # Arrange
    workspace_id = (await demo.get(f"{API_PREFIX}/workspace")).json()["workspace_id"]

    # Act
    answered = await demo.post(
        "/demo/api/v1/store/scenario",
        json={"scenario_mode": "pre_fix", "fault_profile": profile},
        headers={"X-Workspace-Id": workspace_id},
    )

    # Assert
    assert answered.status_code != 200, answered.text


def test_no_shipped_control_offers_a_cut_profile() -> None:
    """A control that lets somebody pick an unbuilt fault is the M11 failure.

    A comment explaining why the profile is absent is documentation; a string
    literal in a control is an offer, so only code is scanned.
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
    for profile in _unimplemented_profiles():
        for path in shipped:
            code = "\n".join(
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if not line.strip().startswith(("*", "//", "/*"))
            )
            if profile in code:
                offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()} -> {profile}")

    assert offenders == [], f"shipped UI code offers cut fault profiles: {offenders}"


def test_no_document_claims_a_cut_profile_is_demonstrable() -> None:
    """Constitution §8: product copy claims nothing unshipped.

    The README may *name* a cut profile — §13.3's vocabulary is public and
    hiding it would be its own dishonesty — but it must not read as though the
    demonstration exists. Any mention sits near a word saying it does not.
    """
    copy = README.read_text(encoding="utf-8")
    for profile in _unimplemented_profiles():
        for line in copy.splitlines():
            if profile not in line:
                continue
            assert any(
                marker in line.lower()
                for marker in ("not implemented", "not shipped", "cut", "unavailable", "tier 3")
            ), f"the README mentions {profile} without saying it is not shipped: {line.strip()!r}"
