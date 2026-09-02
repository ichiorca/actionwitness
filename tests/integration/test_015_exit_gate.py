"""015's exit gate — auditing a storefront somebody else built (015-T7).

The centerpiece is `test_gate_2_the_audit_names_the_tool_that_lied`: a fixture
storefront whose read tools answer perfectly and whose cart tool reports success
while the cart never moves, producing a report that says so in a shop owner's
language.

That is the third shape this product shows. 013 caught a state change nobody
declared; 014 caught tools swapped underneath a running agent; this one catches
a surface that was *given* agent tools and never told they were broken — the
population §12.17 exists for, who did not choose any of this and cannot see it
failing.

Every guardrail is exercised as part of the gate rather than trusted:
unconfigured means unreachable, a non-allowlisted origin is refused at every
layer, and the two consequential tools are reported without being touched. No
test here contacts anything; FR-160a puts the reads in the operator's browser
and the fixture is one we own.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.application.audit_evidence import ToolOutcome, audit_findings
from actionwitness_service.application.audit_report import compose_audit_report
from fastapi import FastAPI
from integrations.shopify.audit import PROVENANCE, AuditObservationError, ExternalAuditAdapter
from integrations.shopify.pack import NEVER_INVOKED_TOOLS, match_pack, pack_for

pytestmark = [pytest.mark.integration]

AUDITS = f"{API_PREFIX}/audits"
ORIGIN = "https://shop.example"
STRANGER = "https://someone-elses-brand.example"

CONFIGURED = {
    "HARNESS_ENV": "local",
    "BUGGY_STORE_ENABLED": "false",
    "EXTERNAL_AUDIT_ENABLED": "true",
    "EXTERNAL_AUDIT_ALLOWED_ORIGINS": ORIGIN,
}
UNCONFIGURED = {"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"}

#: What the fixture storefront publishes: read tools, a lying cart tool, and
#: checkout sitting there reachable.
ENUMERATED = ["search_catalog", "get_cart", "update_cart", "proceed_to_checkout"]

EMPTY_CART: dict[str, Any] = {
    "items": [],
    "item_count": 0,
    "total_price": 0,
    "items_subtotal_price": 0,
    "currency": "USD",
}


@pytest.fixture
async def configured(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = create_app(environ=CONFIGURED, database_path=tmp_path / "audit.sqlite3")
    async with app.router.lifespan_context(app):
        yield app


@pytest.fixture
async def unconfigured(tmp_path: Path) -> AsyncIterator[FastAPI]:
    app = create_app(environ=UNCONFIGURED, database_path=tmp_path / "plain.sqlite3")
    async with app.router.lifespan_context(app):
        yield app


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


def _audit_the_fixture(*, cart_tool_lies: bool) -> dict[str, Any]:
    """One audit of the fixture storefront, end to end on the server side.

    The browser half is tested in `halfBrokenStorefront.test.ts`; this drives
    the same evidence through the adapter, the classifier, and the report.
    """
    adapter = ExternalAuditAdapter(ORIGIN)
    before = adapter.normalize(EMPTY_CART, PROVENANCE)
    after_payload = (
        EMPTY_CART
        if cart_tool_lies
        else {
            **EMPTY_CART,
            "items": [{"variant_id": 111, "quantity": 1, "price": 2599, "line_price": 2599}],
            "item_count": 1,
            "total_price": 2599,
            "items_subtotal_price": 2599,
        }
    )
    after = adapter.normalize(after_payload, PROVENANCE)

    pack = pack_for("shopify_cart")
    assert pack is not None
    findings = audit_findings(
        enumerated=ENUMERATED,
        expected=list(pack.document["expected_tools"]["calls"]),
        # The identical cheerful answer either way — which is the point.
        reports={
            "update_cart": {
                "summary": '{"status":"success","message":"Added to cart"}',
                "expects_state_change": True,
            }
        },
        observed_before=before.payload,
        observed_after=after.payload,
        never_invoked=pack.never_invoked,
    )
    return compose_audit_report(
        authorized_origin=ORIGIN,
        pack_id=pack.pack_id,
        pack_title=pack.title,
        findings=findings,
    )


# --- criterion 1: unconfigured means unreachable ------------------------------


async def test_gate_1_an_unconfigured_module_reaches_nothing(unconfigured: FastAPI) -> None:
    """§29.1 ships the public deployment with the audit off, so an anonymous
    visitor can never direct it at a third party."""
    async with client(unconfigured) as visitor:
        authorize = await visitor.post(
            AUDITS,
            json={"origin": ORIGIN, "asserted_by": "operator", "authorized": True},
            headers={"Origin": "https://harness.test"},
        )
        current = await visitor.get(f"{AUDITS}/current")
        modules = (await visitor.get(f"{API_PREFIX}/workspace")).json()["modules"]

    assert authorize.status_code == 403
    assert current.json() == {"audit": None}
    assert modules["external_audit"]["status"] == "disabled"


# --- criterion 2: the centerpiece ---------------------------------------------


def test_gate_2_the_audit_names_the_tool_that_lied() -> None:
    """The merchant report: working tools green, the silent failure red.

    The tool's answer is byte-identical to a working one — a call-level
    evaluator sees nothing — and the independent cart read is what disagrees.
    """
    report = _audit_the_fixture(cart_tool_lies=True)

    assert "update_cart" in report["summary"]["headline"]
    assert "said they worked when they had not" in report["summary"]["headline"]

    entry = next(t for t in report["summary"]["tools"] if t["tool"] == "update_cart")
    assert entry["what_to_do"].startswith("Fix this first")


def test_gate_2_the_evidence_shows_the_claim_beside_the_observation() -> None:
    """A merchant has to be able to see the disagreement, not be told about it."""
    report = _audit_the_fixture(cart_tool_lies=True)

    evidence = next(e for e in report["evidence"] if e["tool_name"] == "update_cart")
    assert "success" in str(evidence["reported"])
    assert evidence["observed_before"] == evidence["observed_after"]


def test_gate_2_an_honest_storefront_is_not_accused() -> None:
    """The falsifiability half.

    Without it, a harness that called every surface broken would pass the
    headline test and be useless — and would libel every merchant who ran it.
    """
    report = _audit_the_fixture(cart_tool_lies=False)

    assert report["summary"]["headline"] == "Every tool that was tried did what it said it did."


def test_gate_2_the_never_invoked_tools_are_reported_not_exercised() -> None:
    """Present, reachable, and deliberately untouched.

    A site owner needs to know an agent can reach checkout from their store; the
    audit needed to create an order to say more than that, and did not.
    """
    report = _audit_the_fixture(cart_tool_lies=True)

    entry = next(t for t in report["summary"]["tools"] if t["tool"] == "proceed_to_checkout")
    assert entry["says"].startswith("is available to agents but was not tried")
    assert "proceed_to_checkout" in report["summary"]["not_checked"]


async def test_gate_2_the_whole_journey_is_reachable_through_the_api(
    configured: FastAPI,
) -> None:
    """Criterion 2 as a *journey*, not as an arithmetic check.

    The tests above compose the adapter, the classifier, and the report composer
    directly, which proves the server-side reasoning and — on its own — proved
    it about code no client could reach. The feature shipped with authorization
    and nothing to call after it: the classifier and the report composer were
    imported only by tests, and an authorized audit sat live until its workspace
    aged out. So the gate now also asserts the plain thing a shop owner does:
    authorize, submit what their browser saw, read the report.
    """
    # Arrange
    async with client(configured) as visitor:
        authorized = await visitor.post(
            f"{API_PREFIX}/audits",
            json={"origin": ORIGIN, "asserted_by": "the shop owner", "authorized": True},
        )

        # Act
        submitted = await visitor.post(
            f"{API_PREFIX}/audits/current/evidence",
            json={
                "pack_id": "shopify_cart",
                "enumerated": ENUMERATED,
                "reports": {
                    "update_cart": {
                        "summary": '{"status":"success","message":"Added to cart"}',
                        "expects_state_change": True,
                    }
                },
                "observed_before": EMPTY_CART,
                "observed_after": EMPTY_CART,
            },
        )
        fetched = await visitor.get(f"{API_PREFIX}/audits/current/report")

    # Assert — the same verdict the composed path reaches, arrived at by HTTP.
    assert authorized.status_code == 201, authorized.text
    assert submitted.status_code == 201, submitted.text
    report = submitted.json()["report"]
    assert "update_cart" in report["summary"]["headline"]
    assert "said they worked when they had not" in report["summary"]["headline"]
    assert fetched.status_code == 200
    assert fetched.json()["report"] == report


# --- criterion 3: refused at every layer --------------------------------------


async def test_gate_3_a_non_allowlisted_origin_is_refused_at_the_api(
    configured: FastAPI,
) -> None:
    async with client(configured) as visitor:
        response = await visitor.post(
            AUDITS,
            json={"origin": STRANGER, "asserted_by": "operator", "authorized": True},
            headers={"Origin": "https://harness.test"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"


def test_gate_3_a_non_allowlisted_origin_is_refused_at_the_adapter() -> None:
    """The second layer. §12.17 forbids following "a redirect, a link, or a
    navigation beyond" the authorized origin, so the adapter refuses an
    observation that claims to come from anywhere else."""
    with pytest.raises(AuditObservationError, match="origin"):
        ExternalAuditAdapter(ORIGIN).validate_origin(STRANGER)


def test_gate_3_no_audit_module_can_reach_the_network() -> None:
    """The third layer, and the one that makes the other two structural.

    A module that cannot open a socket cannot be talked into scanning anybody,
    whatever a later change to the calling code decides. Checked here on the
    module that actually holds the operator-supplied origin;
    `tests/architecture/test_audit_guardrails.py` walks the whole set.
    """
    import ast

    root = Path(__file__).resolve().parent.parent.parent
    service = root / "apps/actionwitness_service/src/actionwitness_service/application"
    tree = ast.parse((service / "audit_service.py").read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert imported & {"httpx", "requests", "socket", "urllib3", "aiohttp"} == set()


# --- criterion 4: readable without the harness's vocabulary -------------------


def test_gate_4_the_report_uses_no_harness_vocabulary() -> None:
    """Criterion 4. A merchant has a storefront and customers, not contracts."""
    import json

    from actionwitness_service.application.audit_report import SUMMARY_FORBIDDEN_WORDS

    summary = json.dumps(_audit_the_fixture(cart_tool_lies=True)["summary"]).lower()

    assert [word for word in SUMMARY_FORBIDDEN_WORDS if word in summary] == []


def test_gate_4_the_report_states_its_own_limits() -> None:
    """A merchant who believes a clean result is a guarantee stops looking."""
    limits = _audit_the_fixture(cart_tool_lies=False)["summary"]["limits"]

    assert any("not a guarantee" in line for line in limits)
    assert any("one journey" in line for line in limits)


def test_gate_4_the_report_needs_no_browser_to_render() -> None:
    """It is data, not a component.

    The whole report is produced by a pure function over recorded evidence, so
    it renders in a terminal, an email, or a page with no WebMCP.
    """
    report = _audit_the_fixture(cart_tool_lies=True)

    assert isinstance(report, dict)
    assert report["audited_site"] == ORIGIN


# --- the guarantees the criteria above rest on (FR-161, FR-162) ---------------


def test_gate_2_the_pack_offered_is_the_pack_named_in_the_report() -> None:
    """FR-161: the operator selects explicitly and the report names what ran.

    A reader who cannot tell which journey was tried cannot tell what the result
    covers — and a report that named a pack nobody selected would be describing
    a run that never happened.
    """
    offered = match_pack(ENUMERATED)
    assert [pack.pack_id for pack in offered] == ["shopify_read_only", "shopify_cart"]

    report = _audit_the_fixture(cart_tool_lies=True)
    assert report["checked_using_id"] == "shopify_cart"
    assert report["checked_using"] == "Shopify storefront — cart pass"


def test_gate_3_no_offered_pack_can_dispatch_a_consequential_tool() -> None:
    """FR-162, held at the point where a pack is chosen."""
    for pack in match_pack(ENUMERATED):
        calls = set(pack.document["expected_tools"]["calls"])
        assert calls & NEVER_INVOKED_TOOLS == set()


def test_gate_4_every_outcome_the_classifier_can_produce_has_merchant_copy() -> None:
    """A verdict with no sentence attached would render as a blank line."""
    from actionwitness_service.application.audit_report import _ADVICE, _HEADLINE

    for outcome in ToolOutcome:
        assert outcome in _HEADLINE
        assert outcome in _ADVICE
