"""015-T3 — the external-audit contract pack (§12.17, FR-161, FR-162).

The load-bearing test here is
`test_no_contract_in_the_pack_can_dispatch_a_forbidden_tool`. FR-162 makes order
creation, payment, authentication and destructive operations "unavailable in
built-in external contract packs", and these packs run against a storefront a
real shopper uses. The difference between an audit and an incident is that
`proceed_to_checkout` is *enumerated* and never *called*.

So the assertion is not "we remembered not to add it" — it walks every contract
in the pack and every place a tool name can appear, because the way this breaks
is somebody adding a helpful cart-to-checkout journey six months from now and
every other test still passing.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.security.canonical import content_hash
from integrations.shopify.audit import TARGET_ID
from integrations.shopify.pack import (
    AUDIT_PACKS,
    NEVER_INVOKED_TOOLS,
    READ_ONLY_TOOLS,
    SHOPIFY_TOOL_NAMES,
    WRITE_TOOLS,
    match_pack,
    pack_for,
)

pytestmark = pytest.mark.contracts


# --- the ten documented tools -------------------------------------------------


def test_the_pack_names_the_ten_documented_tools() -> None:
    """Pinned as data so an upstream rename is a one-line diff with a date."""
    assert SHOPIFY_TOOL_NAMES == (
        "search_catalog",
        "browse_store",
        "get_product",
        "show_variant",
        "get_cart",
        "update_cart",
        "cancel_cart",
        "proceed_to_checkout",
        "manage_orders",
        "search_shop_policies_and_faqs",
    )


def test_every_documented_tool_is_classified_exactly_once() -> None:
    """A tool in no class is one nobody decided about.

    An unclassified name would be neither exercised nor reported, which is the
    quietest possible way to drop a consequential tool.
    """
    classified = READ_ONLY_TOOLS | WRITE_TOOLS | NEVER_INVOKED_TOOLS

    assert classified == set(SHOPIFY_TOOL_NAMES)
    assert set() == READ_ONLY_TOOLS & WRITE_TOOLS
    assert set() == READ_ONLY_TOOLS & NEVER_INVOKED_TOOLS
    assert set() == WRITE_TOOLS & NEVER_INVOKED_TOOLS


def test_the_consequential_tools_are_the_ones_fr_162_names() -> None:
    """FR-162: order creation, payment, authentication, destructive operations."""
    assert {"proceed_to_checkout", "manage_orders"} == NEVER_INVOKED_TOOLS


# --- the guarantee (FR-162) ---------------------------------------------------


@pytest.mark.parametrize("pack", AUDIT_PACKS, ids=lambda p: p.pack_id)
def test_no_contract_in_the_pack_can_dispatch_a_forbidden_tool(pack) -> None:
    """The difference between an audit and an incident.

    Checked at every place a tool name can reach a dispatcher, not just at
    `expected_tools`, because the way this breaks is a later session adding a
    helpful cart-to-checkout journey while every other test keeps passing.
    """
    contract = parse_contract(pack.document)

    expected = set(contract.expected_tools.calls) if contract.expected_tools else set()
    assert expected & NEVER_INVOKED_TOOLS == set(), (
        f"{pack.pack_id} expects a call FR-162 forbids against an external target"
    )

    # A confirmation policy names a tool the contract intends to *invoke* behind
    # consent. For an external target there is no consent that makes checkout
    # acceptable, so naming one here would be declaring an intent to dispatch.
    assert set(contract.confirmed_tools()) & NEVER_INVOKED_TOOLS == set()


@pytest.mark.parametrize("pack", AUDIT_PACKS, ids=lambda p: p.pack_id)
def test_every_pack_forbids_the_consequential_tools_outright(pack) -> None:
    """Belt and braces, and the braces are the point.

    Omitting a tool from `expected_tools` only means nobody *asked* for the
    call. A `forbidden_tool` policy fails the run if one happens anyway — which
    is the case that matters, since the agent driving an external surface is not
    ours and the tools are not ours either.
    """
    contract = parse_contract(pack.document)
    forbidden = {
        policy.tool for policy in contract.policies if policy.type.value == "forbidden_tool"
    }

    assert forbidden >= NEVER_INVOKED_TOOLS, (
        f"{pack.pack_id} does not forbid {sorted(NEVER_INVOKED_TOOLS - forbidden)}"
    )


@pytest.mark.parametrize("pack", AUDIT_PACKS, ids=lambda p: p.pack_id)
def test_a_pack_declares_only_write_paths_fr_162_permits(pack) -> None:
    """ "only those write paths its contract declares" — and cart is the limit."""
    contract = parse_contract(pack.document)
    expected = set(contract.expected_tools.calls) if contract.expected_tools else set()

    assert expected <= READ_ONLY_TOOLS | WRITE_TOOLS


def test_the_never_invoked_tools_are_still_reported() -> None:
    """Enumerated, not hidden.

    A site owner needs to know an agent can reach checkout from their store.
    Silently omitting the tool would make the report read as though it were not
    there, which is a more comfortable answer and a false one.
    """
    for pack in AUDIT_PACKS:
        assert set(pack.never_invoked) == NEVER_INVOKED_TOOLS


# --- the contracts are real contracts ------------------------------------------


@pytest.mark.parametrize("pack", AUDIT_PACKS, ids=lambda p: p.pack_id)
def test_every_pack_contract_parses_through_the_real_validator(pack) -> None:
    contract = parse_contract(pack.document)

    assert contract.target_id == TARGET_ID
    assert contract.assertions


@pytest.mark.parametrize("pack", AUDIT_PACKS, ids=lambda p: p.pack_id)
def test_every_pack_contract_is_written_in_canonical_form(pack) -> None:
    """The 013-T4 trap, which applies to any seeded contract document.

    Seeding hashes the document as written; §24.2 re-verifies by hashing the
    parsed contract's canonical form. A field left to its default breaks
    eval-case generation far from its cause.
    """
    document = dict(pack.document)

    assert content_hash(document) == content_hash(parse_contract(document).canonical_document())


@pytest.mark.parametrize("pack", AUDIT_PACKS, ids=lambda p: p.pack_id)
def test_every_pack_asserts_that_no_order_was_created(pack) -> None:
    """The one assertion every external audit must carry.

    FR-162 forbids order creation; this is how a run *demonstrates* none
    happened rather than assuming it.
    """
    contract = parse_contract(pack.document)
    paths = {str(assertion.path) for assertion in contract.assertions}

    assert "target.order.created" in paths


# --- offering a pack (FR-161) --------------------------------------------------


def test_a_matching_surface_is_offered_its_packs() -> None:
    offered = match_pack(["get_cart", "search_catalog", "update_cart", "browse_store"])

    assert [pack.pack_id for pack in offered] == ["shopify_read_only", "shopify_cart"]


def test_the_read_only_pass_is_offered_first() -> None:
    """The safe option should be the one a hurried operator takes by reflex."""
    offered = match_pack(list(SHOPIFY_TOOL_NAMES))

    assert offered[0].pack_id == "shopify_read_only"


def test_a_surface_missing_the_signature_is_offered_nothing() -> None:
    """An absence is an absence.

    Shopify renamed parts of this surface during 2026, and offering a cart pack
    to a store with no cart tool would produce a run that fails for a reason the
    merchant cannot act on.
    """
    assert match_pack(["search_catalog"]) == ()


def test_extra_tools_do_not_disqualify_a_surface() -> None:
    """A merchant may publish more than the documented set, and that is not a
    reason to refuse to audit them."""
    offered = match_pack(["get_cart", "search_catalog", "their_own_loyalty_tool"])

    assert [pack.pack_id for pack in offered] == ["shopify_read_only"]


def test_matching_never_offers_a_pack_because_of_a_forbidden_tool() -> None:
    """A signature must not be satisfiable by the tools nobody may call."""
    for pack in AUDIT_PACKS:
        assert set(pack.signature) & NEVER_INVOKED_TOOLS == set()


def test_an_unknown_pack_id_resolves_to_nothing() -> None:
    assert pack_for("shopify_read_only") is not None
    assert pack_for("checkout_everything") is None
