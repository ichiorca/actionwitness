"""The `shopify_exact_cart` contract (§13.5, FR-108, FR-114).

§13.5: "The checked-in Shopify contract template has `target_id` fixed to the
configured Shopify adapter and is parameterized with one configured test variant
ID, a stable line key `test_variant`, expected currency, and quantity one." It
"asserts the configured variant and exact quantity, internal arithmetic
consistency, expected currency, and absence of checkout navigation."

**It omits `expected_tools`, and that is the load-bearing omission.** FR-114:
"Because the standalone bridge cannot observe Shopify's internal tool
trajectory, this contract omits `expected_tools`; any tool-selection or ordering
constraints belong to a separately correlated evaluator scenario." AC-18 says
the same from the other side — model selection, observed trajectory, and tool
execution stay `not_evaluated`. A trajectory term here would be evaluated
against nothing and reported as satisfied, which is the self-report-as-proof
error this product exists to refuse, committed against a target that cannot be
re-run. The adapter enforces it structurally: it publishes no tool surface, so
§10.2 refuses any contract for this target that names a tool at all.

**What carries the checkout refusal instead.** Not a `forbidden_tool` policy —
that would be a claim about a channel nothing here watches. Two things that are
real: `target.page.checkout_navigation_observed`, which the bridge witnesses and
this contract asserts, and FR-114's rule that a missing final observation is "a
failed or incomplete trial, never a pass" — a shopper who navigates to checkout
leaves the storefront page the bridge lives on, and no final cart arrives.

**Nothing about orders is asserted.** Order state needs an Admin API credential
FR-118 forbids the cart path from holding, so the observation carries no `order`
path and no term here pretends to check one. Saying nothing is the honest form
of "we cannot see it".

## Why `expand` takes the variant and currency

Both are server-controlled (FR-110, project rules: "store origin, variant, and
currency remain server-controlled"). They are therefore **not** template
`parameters`: `parameters` is what §25.2's declarative form publishes as
controls, and a form control for the test variant would be the caller choosing
which variant counted as correct. The generic instantiate route passes only
`contract_name`, `quantity` and `discount_code`, so it cannot supply them and is
refused by name — which is the right outcome. The Shopify flow expands this
template with `ShopifyAdapter.contract_parameters()`, whose values came from
`ServiceSettings.shopify`.

The seeded `TEMPLATES` document therefore carries every term that does *not*
depend on the locked configuration, and expansion adds the two that do. A
placeholder variant id in the shipped document would be a contract term that is
false; an absent one is a term that is not yet made. Only the second is honest.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from integrations.shopify.adapter import TARGET_ID
from integrations.shopify.observation import TEST_VARIANT_KEY

__all__ = [
    "MAX_CONTRACT_NAME_CHARS",
    "SERVER_PARAMETERS",
    "TEMPLATES",
    "TEMPLATE_ID",
    "ContractTemplate",
    "TemplateExpansionError",
    "expand",
    "template_for",
    "template_ids",
]


@dataclass(frozen=True, slots=True)
class ContractTemplate:
    """One trusted, server-expanded contract template (FR-021, FR-023).

    The same shape `integrations.buggy_store` and `integrations.self_target`
    ship, so the composition root seeds and expands every pack through one code
    path rather than branching on which integration a template came from.
    """

    template_id: str
    title: str
    summary: str
    #: The failure profile this contract is designed to expose, when there is
    #: one. Always `none` here: the descriptor advertises `("none",)` because
    #: FR-162 forbids injecting a fault into an external target at all, so a
    #: template claiming to demonstrate one would name something nothing can
    #: produce.
    demonstrates: str
    document: Mapping[str, Any]
    #: Empty, and deliberately so — see the module docstring. The variant and
    #: currency this contract needs are server configuration, not form controls,
    #: and publishing them here would render them as fields a caller fills in.
    parameters: Sequence[str] = field(default_factory=tuple)


#: FR-114's journey and §13.5's assertions, minus the two terms that depend on
#: the locked configuration.
#:
#: The precondition is FR-116's: "The initial observation must satisfy the
#: empty-cart precondition." Without it a cart that already held the test
#: variant would pass a journey in which the agent did nothing at all.
_EXACT_CART: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "shopify-exact-cart",
    "description": "Leave exactly one configured test variant in the cart, and never checkout.",
    "target_id": TARGET_ID,
    "intent": (
        "Find the configured test product, choose its variant, and leave exactly one "
        "of it in the shopper's cart, without navigating to checkout."
    ),
    "preconditions": [
        {"path": "target.cart.items", "operator": "count_equals", "value": 0},
    ],
    # No `expected_tools`. See the module docstring: FR-114 removes it and the
    # adapter's empty tool surface makes its absence structural.
    "assertions": [
        {
            # One line, so a cart that also holds something unasked-for fails.
            # The projection keys the *configured* variant as `test_variant` and
            # every other variant by its own id, so an unexpected item is
            # visible here rather than quietly counted as the right one.
            "id": "exactly-one-cart-line",
            "path": "target.cart.items",
            "operator": "count_equals",
            "value": 1,
            "severity": "critical",
        },
        {
            "id": "test-variant-quantity-is-one",
            "path": f"target.cart.items.{TEST_VARIANT_KEY}.quantity",
            "operator": "equals",
            "value": 1,
            "severity": "critical",
        },
        {
            # Shopify's own count of units, which is not the same statement as
            # "one line": two of the variant on one line would satisfy the line
            # count and fail this.
            "id": "cart-holds-one-item",
            "path": "target.cart.item_count",
            "operator": "equals",
            "value": 1,
            "severity": "critical",
        },
        {
            # §13.5's "internal arithmetic consistency", computed in exact
            # decimals by the normalizer because §9.4's operators compare a path
            # to a literal and cannot compare a subtotal to the sum of lines.
            "id": "totals-are-internally-consistent",
            "path": "target.cart.totals_consistent",
            "operator": "equals",
            "value": True,
            "severity": "critical",
        },
        {
            "id": "no-checkout-navigation",
            "path": "target.page.checkout_navigation_observed",
            "operator": "equals",
            "value": False,
            "severity": "critical",
        },
    ],
    # No `policies` key at all rather than an empty list: seeding hashes the
    # document as written while §24.2 re-verifies it by hashing the parsed
    # contract's canonical form, and `canonical_document()` omits an empty
    # policy list. A key left in would break eval-case generation far from its
    # cause (the 013-T4 trap).
    "redaction": {"paths": ["**.email", "**.payment_token", "**.token"]},
}

TEMPLATE_ID: Final = "shopify_exact_cart"

TEMPLATES: Final[tuple[ContractTemplate, ...]] = (
    ContractTemplate(
        template_id=TEMPLATE_ID,
        title="One configured variant, no checkout",
        summary=(
            "Leaves exactly one configured test variant in an authorized development "
            "store's cart, verified from an independent same-session cart read rather "
            "than from what Shopify's tools reported. Says nothing about which tools "
            "were chosen: the bridge cannot see them."
        ),
        demonstrates="none",
        document=_EXACT_CART,
    ),
)

_BY_ID: Final[Mapping[str, ContractTemplate]] = {
    template.template_id: template for template in TEMPLATES
}


def template_ids() -> Sequence[str]:
    """Template identifiers in publication order."""
    return tuple(template.template_id for template in TEMPLATES)


def template_for(template_id: str) -> ContractTemplate | None:
    """One template by ID, or `None` when it is not published."""
    return _BY_ID.get(template_id)


# --- FR-021 template expansion ------------------------------------------------

#: §25.2's `contract_name` bound, matching the core's `MAX_NAME_LENGTH`.
MAX_CONTRACT_NAME_CHARS: Final = 80

#: Every scalar §25.2's declarative form may carry. This template allowlists
#: none of them, so each is a named rejection rather than a shrug — a caller
#: told their contract was created would otherwise believe they had constrained
#: a quantity this contract fixes at one for a reason (FR-021, FR-114).
_FORM_PARAMETERS: Final[tuple[str, ...]] = ("quantity", "discount_code")

#: The locked configuration `expand` requires. **Server-supplied**: these come
#: from `ServiceSettings.shopify` by way of `ShopifyAdapter.contract_parameters`,
#: never from a request body. Required rather than optional so a caller that
#: cannot supply them — the generic §25.2 form, for one — is refused instead of
#: receiving a contract that asserts nothing about the variant or the currency.
SERVER_PARAMETERS: Final[tuple[str, ...]] = ("variant_id", "expected_currency")


class TemplateExpansionError(ValueError):
    """A submission this template cannot accept.

    Carries `(field, message)` pairs so the boundary can return §15.8's
    field-level details rather than one opaque sentence.
    """

    def __init__(self, details: Sequence[tuple[str, str]]) -> None:
        self.details = tuple(details)
        super().__init__("; ".join(f"{field}: {message}" for field, message in self.details))


def expand(template_id: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Expand the Shopify template into a complete §10 contract document.

    Adds §13.5's two configuration-dependent assertions — the configured variant
    and the expected currency — to the shipped document. Everything else, every
    path and every operator, comes from the template; the only thing a *caller*
    can change is the display name, and only within §25.2's bound.

    Raises `TemplateExpansionError` naming every offending field. The result is
    still validated through the core's `parse_contract` before it is stored.
    """
    template = template_for(template_id)
    if template is None:
        raise TemplateExpansionError([("template_id", f"unknown template {template_id!r}")])

    details: list[tuple[str, str]] = [
        (name, f"template {template_id!r} does not accept {name!r}")
        for name in _FORM_PARAMETERS
        if parameters.get(name) is not None
    ]

    variant_id = _required_text(parameters, "variant_id", details)
    currency = _required_currency(parameters, details)
    chosen_name = _optional_name(parameters, details)

    if details:
        raise TemplateExpansionError(details)

    document = dict(template.document)
    document["assertions"] = [
        *template.document["assertions"],
        {
            # Which variant, stated as a term rather than left implicit in the
            # projection's key. The line key alone would make the contract read
            # the same however the store was configured, and a report has to be
            # able to say *which* variant was expected.
            "id": "the-configured-test-variant",
            "path": f"target.cart.items.{TEST_VARIANT_KEY}.variant_id",
            "operator": "equals",
            "value": variant_id,
            "severity": "critical",
        },
        {
            # §13.5's expected currency. `/cart.js` is locale-aware, so the same
            # journey run against a different market returns different money —
            # an assertion on totals with no currency term would compare
            # numbers from two currencies and call them equal.
            "id": "the-expected-currency",
            "path": "target.cart.currency",
            "operator": "equals",
            "value": currency,
            "severity": "critical",
        },
    ]
    if chosen_name is not None:
        document["name"] = chosen_name
    return document


def _required_text(
    parameters: Mapping[str, Any], name: str, details: list[tuple[str, str]]
) -> str | None:
    value = parameters.get(name)
    if value is None:
        details.append((name, f"{name} is required and comes from server configuration"))
        return None
    if not isinstance(value, str) or not value.strip():
        details.append((name, f"{name} must be a non-empty string"))
        return None
    return value.strip()


def _required_currency(parameters: Mapping[str, Any], details: list[tuple[str, str]]) -> str | None:
    """A three-letter code, upper-cased, matching what the normalizer records.

    `normalize_cart` upper-cases the currency it observes, so an expectation
    written in lower case would fail against a correct cart — a false accusation
    caused by spelling.
    """
    value = _required_text(parameters, "expected_currency", details)
    if value is None:
        return None
    if len(value) != 3 or not value.isalpha():
        details.append(("expected_currency", "expected_currency must be a three-letter code"))
        return None
    return value.upper()


def _optional_name(parameters: Mapping[str, Any], details: list[tuple[str, str]]) -> str | None:
    chosen = parameters.get("contract_name")
    if chosen is None:
        return None
    if not isinstance(chosen, str) or not chosen.strip():
        details.append(("contract_name", "contract_name must be a non-empty string"))
        return None
    if len(chosen) > MAX_CONTRACT_NAME_CHARS:
        details.append(
            ("contract_name", f"contract_name must be at most {MAX_CONTRACT_NAME_CHARS} characters")
        )
        return None
    return chosen
