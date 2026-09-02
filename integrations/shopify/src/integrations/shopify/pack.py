"""The built-in external-audit contract pack (§12.17, FR-161, FR-162; 015-T3).

FR-161: "When the surface matches a known built-in contract pack by tool-name
signature, the pack shall be offered; the operator selects it explicitly and the
report shall name which pack was applied."

**Two tools in this pack are never invoked, and that is a property of the data
rather than a rule someone remembers.** FR-162 forbids "order creation, payment,
authentication, and destructive operations... for external targets" and makes
them "unavailable in built-in external contract packs". `proceed_to_checkout`
creates an order on somebody's real storefront; `manage_orders` reaches existing
ones. Both are *enumerated* — a site owner needs to know an agent can reach
them — and neither appears in any contract's `expected_tools`, so no contract in
this pack can ask for a call that would make one happen.

The distinction is worth stating plainly, because it is the difference between
an audit and an incident: **their presence is reported; their behaviour is not
tested.** A report that said "checkout works" would have had to create an order
to know that.

**Absences are absences, not failures.** Shopify renamed parts of this surface
during 2026, and a pack that treated a missing tool as a defect would accuse a
merchant of breaking something they never had. The pack pins the names as
published and reports what it did not find.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from integrations.shopify.audit import TARGET_ID

__all__ = [
    "AUDIT_PACKS",
    "NEVER_INVOKED_TOOLS",
    "READ_ONLY_TOOLS",
    "SHOPIFY_TOOL_NAMES",
    "WRITE_TOOLS",
    "ContractPack",
    "match_pack",
    "pack_for",
]

#: Shopify's publicly documented agent tool names, in published order.
#:
#: Pinned as data so a rename upstream is a one-line diff with a date attached,
#: rather than a behaviour change nobody can find.
SHOPIFY_TOOL_NAMES: Final[tuple[str, ...]] = (
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

#: Tools that read and change nothing. Safe to exercise against a live
#: storefront the operator is authorized on.
READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "search_catalog",
        "browse_store",
        "get_product",
        "show_variant",
        "get_cart",
        "search_shop_policies_and_faqs",
    }
)

#: Tools that change cart state and nothing beyond it. FR-162 allows "only those
#: write paths its contract declares", and a cart is reversible by the shopper.
WRITE_TOOLS: Final[frozenset[str]] = frozenset({"update_cart", "cancel_cart"})

#: FR-162's forbidden set for an external target. Enumerated so a site owner
#: learns an agent can reach them; never invoked, because invoking one would
#: create an order or touch existing ones on a real storefront.
NEVER_INVOKED_TOOLS: Final[frozenset[str]] = frozenset({"proceed_to_checkout", "manage_orders"})


@dataclass(frozen=True, slots=True)
class ContractPack:
    """One offered pack: what it matches, what it exercises, what it will not."""

    pack_id: str
    title: str
    summary: str
    #: The tool-name signature FR-161 matches a surface against. A surface must
    #: contain all of these to be offered this pack; extra tools do not
    #: disqualify it, because a merchant may publish more than the documented
    #: set and that is not a reason to refuse to audit them.
    signature: tuple[str, ...]
    document: Mapping[str, Any]
    #: Reported as present-but-unexercised when the surface carries them.
    never_invoked: tuple[str, ...] = tuple(sorted(NEVER_INVOKED_TOOLS))


#: A read-only pass. Nothing this contract expects can change anything, so it is
#: the pack to offer first against a storefront somebody depends on.
_READ_ONLY_AUDIT: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "shopify-read-only-audit",
    "description": "Exercise the documented read tools and change nothing.",
    "target_id": TARGET_ID,
    "intent": (
        "Confirm the storefront's agent-facing read tools answer, and that "
        "nothing about the shopper's cart changes while they do."
    ),
    "expected_tools": {"ordered": False, "calls": ["get_cart", "search_catalog"]},
    "assertions": [
        {
            "id": "cart-unchanged-by-reads",
            "path": "target.cart.item_count",
            "operator": "unchanged",
            "severity": "critical",
        },
        {
            "id": "no-order-created",
            "path": "target.order.created",
            "operator": "equals",
            "value": False,
            "severity": "critical",
        },
    ],
    "policies": [
        # The two tools FR-162 forbids, refused by the contract as well as by
        # the pack's data. A `forbidden_tool` policy fails the run if one is
        # invoked at all — belt and braces, because the pack's omission of them
        # from `expected_tools` only means nobody *asked* for the call.
        {"type": "forbidden_tool", "tool": "manage_orders"},
        {"type": "forbidden_tool", "tool": "proceed_to_checkout"},
    ],
    "redaction": {"paths": ["**.email", "**.payment_token", "**.token"]},
}

#: The cart pass. Declares exactly one write path, which is what FR-162 permits.
_CART_AUDIT: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "shopify-cart-audit",
    "description": (
        "Add exactly one item through the agent tool and observe the cart independently."
    ),
    "target_id": TARGET_ID,
    "intent": (
        "Confirm that when the storefront's cart tool reports success, the "
        "shopper's cart actually changed."
    ),
    "expected_tools": {"ordered": False, "calls": ["update_cart"]},
    "assertions": [
        {
            # An exact delta rather than "it changed". A tool that reported
            # success and added two items is also a defect, and `changed_by`
            # says which one happened while a vague "changed" would call both
            # of them fine.
            "id": "cart-gained-exactly-one-item",
            "path": "target.cart.item_count",
            "operator": "changed_by",
            "value": 1,
            "severity": "critical",
        },
        {
            "id": "no-order-created",
            "path": "target.order.created",
            "operator": "equals",
            "value": False,
            "severity": "critical",
        },
    ],
    "policies": [
        {"type": "forbidden_tool", "tool": "manage_orders"},
        {"type": "forbidden_tool", "tool": "proceed_to_checkout"},
    ],
    "redaction": {"paths": ["**.email", "**.payment_token", "**.token"]},
}

AUDIT_PACKS: Final[tuple[ContractPack, ...]] = (
    ContractPack(
        pack_id="shopify_read_only",
        title="Shopify storefront — read-only pass",
        summary=(
            "Exercises the documented read tools and asserts the shopper's cart is "
            "untouched. Nothing it does can change the store."
        ),
        signature=("get_cart", "search_catalog"),
        document=_READ_ONLY_AUDIT,
    ),
    ContractPack(
        pack_id="shopify_cart",
        title="Shopify storefront — cart pass",
        summary=(
            "Adds one item through the storefront's own cart tool and reads the cart "
            "back independently, so a tool that reports success without changing "
            "anything is visible."
        ),
        signature=("get_cart", "update_cart"),
        document=_CART_AUDIT,
    ),
)


def pack_for(pack_id: str) -> ContractPack | None:
    return next((pack for pack in AUDIT_PACKS if pack.pack_id == pack_id), None)


def match_pack(surface_tool_names: Sequence[str]) -> tuple[ContractPack, ...]:
    """Packs whose signature the enumerated surface satisfies (FR-161).

    Returns every match rather than choosing one: FR-161 says the pack "shall be
    offered" and "the operator selects it explicitly". Picking on their behalf
    would decide, against a storefront somebody depends on, whether a write path
    gets exercised.

    Ordered as `AUDIT_PACKS` is ordered, which puts the read-only pass first —
    the safe option should be the one a hurried operator takes by reflex.
    """
    available = set(surface_tool_names)
    return tuple(pack for pack in AUDIT_PACKS if set(pack.signature) <= available)
