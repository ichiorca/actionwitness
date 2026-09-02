"""015-T6 — the audit guardrails, as structure rather than as promises.

The 015 guardrails are non-negotiable and short: "Operator-asserted authorized
origin only. Read/cart-only. Never checkout, never an order, no crawling, no
scanning of unowned brands." FR-160a adds the mechanism that makes the last two
true by construction: "the harness introduces no headless browser and makes no
outbound request to the audited origin".

A promise in a docstring is not a guardrail. These tests read the audit modules
and assert the *shape* that makes the promise unbreakable:

* no audit module holds an HTTP client, so there is nothing to point at a
  stranger even if a later change wanted to;
* nothing accepts more than one origin, because a list of origins is a scan
  queue with a friendlier name;
* the contract packs cannot dispatch checkout or order management.

Static, so they run in the Python lane with no service and no browser. A test
that needed a running deployment to prove "we never crawl" would be the wrong
shape for the claim.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: Every module that participates in an audit. Named explicitly rather than
#: globbed: a new audit module should have to be added here, which is the moment
#: somebody asks whether it needs a network client.
AUDIT_MODULES: tuple[Path, ...] = (
    REPO_ROOT / "apps/actionwitness_service/src/actionwitness_service/application/audit_service.py",
    REPO_ROOT
    / "apps/actionwitness_service/src/actionwitness_service/application/audit_evidence.py",
    REPO_ROOT / "apps/actionwitness_service/src/actionwitness_service/application/audit_report.py",
    REPO_ROOT / "apps/actionwitness_service/src/actionwitness_service/api/routes/audits.py",
    REPO_ROOT / "integrations/shopify/src/integrations/shopify/audit.py",
    REPO_ROOT / "integrations/shopify/src/integrations/shopify/pack.py",
)

#: Anything that could originate a request to an address the harness was given.
#: `httpx` is the project's client; the rest are the stdlib ways somebody
#: reaches for when the linter complains about the first.
_NETWORK_MODULES: frozenset[str] = frozenset(
    {
        "httpx",
        "requests",
        "urllib.request",
        "urllib3",
        "http.client",
        "socket",
        "aiohttp",
        "ftplib",
        "telnetlib",
        "webbrowser",
        "subprocess",
    }
)


def _imports(path: Path) -> set[str]:
    """Every module name imported by `path`, dotted roots included."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.architecture
def test_every_audit_module_is_present_to_be_checked() -> None:
    """The guard on every test below: a missing file would prove nothing."""
    missing = [path.name for path in AUDIT_MODULES if not path.is_file()]

    assert missing == [], f"audit modules moved or were deleted: {missing}"


@pytest.mark.architecture
@pytest.mark.parametrize("module", AUDIT_MODULES, ids=lambda p: p.name)
def test_no_audit_module_can_reach_the_network(module: Path) -> None:
    """FR-160a: "the harness... makes no outbound request to the audited origin".

    Enforced as an absence of capability rather than as a rule about behaviour.
    The audited origin is a string an operator supplied; a module holding both
    that string and an HTTP client is one edit away from being a scanner, and no
    amount of care in the calling code changes that.

    The observation arrives from the operator's own browser, already
    authenticated as itself, which is also what removes the server-side
    request-forgery class this feature would otherwise create.
    """
    reachable = sorted(
        name
        for name in _imports(module)
        if name in _NETWORK_MODULES or name.split(".")[0] in _NETWORK_MODULES
    )

    assert reachable == [], (
        f"{module.name} imports {reachable}; an audit module holding a network "
        "client is one edit away from a crawler"
    )


@pytest.mark.architecture
def test_the_audit_api_accepts_one_origin_and_never_a_list() -> None:
    """No crawling affordance exists, as a shape rather than a policy.

    A field accepting many origins is a scan queue whatever it is called, and
    "we only use the first one" is a comment somebody deletes.
    """
    route = REPO_ROOT / "apps/actionwitness_service/src/actionwitness_service/api/routes/audits.py"
    tree = ast.parse(route.read_text(encoding="utf-8"))

    plural: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                name = statement.target.id
                if name in {"origins", "targets", "hosts", "urls", "sites"}:
                    plural.append(f"{node.name}.{name}")

    assert plural == [], f"an audit request model accepts a collection of targets: {plural}"


@pytest.mark.architecture
def test_the_contract_packs_cannot_dispatch_checkout_or_order_management() -> None:
    """FR-162, checked from the architecture lane as well as the contracts lane.

    Duplicated deliberately. The contracts lane proves the shipped packs are
    safe; this proves the *guardrail itself* still exists, so deleting the
    contracts-lane test does not silently remove the only check.
    """
    from integrations.shopify.pack import AUDIT_PACKS, NEVER_INVOKED_TOOLS

    assert {"proceed_to_checkout", "manage_orders"} == NEVER_INVOKED_TOOLS
    for pack in AUDIT_PACKS:
        calls = set(pack.document.get("expected_tools", {}).get("calls", ()))
        assert calls & NEVER_INVOKED_TOOLS == set(), (
            f"{pack.pack_id} would dispatch a tool FR-162 forbids"
        )


@pytest.mark.architecture
def test_the_deployment_ships_with_the_audit_disabled() -> None:
    """§29.1: "The public judging deployment ships with it disabled, so an
    anonymous workspace can never assert authorization for an origin the
    deployment did not configure."

    Read from the settings resolver's own default rather than from
    documentation, because the default is the thing that ships.
    """
    from actionwitness_service.config import ServiceSettings

    settings = ServiceSettings.from_env({})

    assert settings.external_audit is None
    assert settings.module("external_audit").status.value == "disabled"


@pytest.mark.architecture
def test_the_allowlist_is_server_controlled_and_not_a_request_field() -> None:
    """An allowlist a client could extend is not an allowlist.

    §29.1 puts `EXTERNAL_AUDIT_ALLOWED_ORIGINS` in startup configuration; this
    asserts no audit request model has grown a field that would let a caller
    supply or widen it.
    """
    route = REPO_ROOT / "apps/actionwitness_service/src/actionwitness_service/api/routes/audits.py"
    source = route.read_text(encoding="utf-8")

    for smell in ("allowed_origins", "allowlist", "allow_origins"):
        assert smell not in source, f"the audit API exposes {smell!r} to a caller"
