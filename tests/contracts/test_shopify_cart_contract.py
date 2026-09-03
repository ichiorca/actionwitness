"""011-T5 — the `shopify_exact_cart` contract (§13.5, FR-108, FR-114, AC-18).

The load-bearing test here is `test_the_contract_omits_expected_tools`. FR-114:
"Because the standalone bridge cannot observe Shopify's internal tool
trajectory, this contract omits `expected_tools`; any tool-selection or ordering
constraints belong to a separately correlated evaluator scenario." AC-18 says
the same from the other side — model selection, observed trajectory, and tool
execution stay `not_evaluated`.

So the assertion is not "we remembered not to add one". It walks every place a
tool name can appear in a contract, because the way this breaks is somebody
adding a helpful `forbidden_tool: proceed_to_checkout` six months from now,
believing they made the contract safer, and shipping a policy that reports
"satisfied" about a channel nothing watched.

The rest of the module evaluates the expanded contract against real normalized
observations, so the refusals FR-114 names — an unexpected variant, the wrong
currency, checkout navigation — are demonstrated as failing verdicts rather than
asserted as intentions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.engine.assertions import evaluate_assertions, evaluate_preconditions
from actionwitness_core.engine.enums import CheckStatus
from actionwitness_core.kernel import ContractError
from actionwitness_core.security.canonical import content_hash
from integrations.shopify.adapter import TARGET_ID, ShopifyAdapter
from integrations.shopify.audit import PROVENANCE
from integrations.shopify.templates import (
    TEMPLATES,
    TemplateExpansionError,
    expand,
    template_for,
    template_ids,
)

pytestmark = pytest.mark.contracts

ORIGIN = "https://dev-store.myshopify.com"
VARIANT = "1234567890"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

SERVER_CONFIG = {"variant_id": VARIANT, "expected_currency": "USD"}


def adapter() -> ShopifyAdapter:
    return ShopifyAdapter(
        store_origin=ORIGIN,
        test_variant_id=VARIANT,
        expected_currency="USD",
        clock=lambda: EPOCH,
    )


def cart(**over: Any) -> dict[str, Any]:
    return {
        "items": [
            {"variant_id": int(VARIANT), "quantity": 1, "price": 2500, "line_price": 2500},
        ],
        "item_count": 1,
        "items_subtotal_price": 2500,
        "total_price": 2500,
        "total_discount": 0,
        "currency": "USD",
        "page": {"checkout_navigation_observed": False},
        **over,
    }


def empty_cart() -> dict[str, Any]:
    return {
        "items": [],
        "item_count": 0,
        "items_subtotal_price": 0,
        "total_price": 0,
        "total_discount": 0,
        "currency": "USD",
        "page": {"checkout_navigation_observed": False},
    }


def verdict(final_payload: dict[str, Any]) -> dict[str, CheckStatus]:
    """Evaluate the expanded contract's assertions against one observed cart."""
    contract = parse_contract(expand("shopify_exact_cart", SERVER_CONFIG))
    initial = adapter().normalize(empty_cart(), PROVENANCE).as_context()
    final = adapter().normalize(final_payload, PROVENANCE).as_context()
    findings = evaluate_assertions(contract.assertions, initial=initial, final=final)
    return {finding.check_id: finding.status for finding in findings}


# --- the shipped template -----------------------------------------------------


def test_the_pack_publishes_the_one_contract_the_spec_names() -> None:
    assert template_ids() == ("shopify_exact_cart",)
    assert template_for("shopify_exact_cart") is not None
    assert template_for("checkout_everything") is None


def test_the_template_targets_the_configured_shopify_adapter() -> None:
    """§13.5: "target_id fixed to the configured Shopify adapter"."""
    contract = parse_contract(TEMPLATES[0].document)

    assert contract.target_id == TARGET_ID == "shopify-development-store"


def test_the_shipped_template_is_written_in_canonical_form() -> None:
    """The 013-T4 trap, which applies to any seeded contract document.

    Seeding hashes the document as written; §24.2 re-verifies by hashing the
    parsed contract's canonical form. A field left to its default — an empty
    `policies` list, say — breaks eval-case generation far from its cause.
    """
    document = dict(TEMPLATES[0].document)

    assert content_hash(document) == content_hash(parse_contract(document).canonical_document())


def test_the_expanded_contract_is_written_in_canonical_form() -> None:
    document = dict(expand("shopify_exact_cart", SERVER_CONFIG))

    assert content_hash(document) == content_hash(parse_contract(document).canonical_document())


def test_the_template_allowlists_no_form_parameter() -> None:
    """The variant and currency are configuration, not controls (FR-110).

    `parameters` is what §25.2's declarative form renders. A control for the
    test variant would be the caller choosing which variant counted as correct.
    """
    assert tuple(TEMPLATES[0].parameters) == ()


def test_the_template_demonstrates_no_fault() -> None:
    """FR-162 forbids injecting one into an external target at all.

    A template claiming to demonstrate a defect would name something nothing in
    the world can produce.
    """
    assert TEMPLATES[0].demonstrates == "none"


# --- the load-bearing omission (FR-114, AC-18) --------------------------------


def test_the_contract_omits_expected_tools() -> None:
    """FR-114: the standalone bridge cannot observe Shopify's tool trajectory."""
    for document in (TEMPLATES[0].document, expand("shopify_exact_cart", SERVER_CONFIG)):
        contract = parse_contract(document)

        assert contract.expected_tools is None


def test_the_contract_names_no_tool_anywhere() -> None:
    """Checked at every place a tool name can reach an evaluator.

    Not just `expected_tools`: a `forbidden_tool`, `idempotent_by_request_id`, or
    `requires_confirmation` policy names a tool too, and each would be reported
    as evaluated against a channel the bridge never watched. AC-18 requires
    trajectory and tool execution to stay `not_evaluated`, and a satisfied
    policy is an evaluated claim.
    """
    for document in (TEMPLATES[0].document, expand("shopify_exact_cart", SERVER_CONFIG)):
        contract = parse_contract(document)

        assert contract.referenced_tools() == frozenset()
        assert contract.confirmed_tools() == frozenset()


def test_the_adapter_would_refuse_a_contract_that_named_a_tool() -> None:
    """The structural half of the rule, not the remembered half.

    §10.2 rejects a contract naming a tool the selected adapter does not
    publish, and this adapter publishes none — so no Shopify contract can carry
    a trajectory term, however well-intentioned.
    """
    document = dict(expand("shopify_exact_cart", SERVER_CONFIG))
    document["expected_tools"] = {"ordered": False, "calls": ["update_cart"]}
    contract = parse_contract(document)

    with pytest.raises(ContractError, match="not valid for target"):
        contract.validate_against_target(target_id=TARGET_ID, tool_names=adapter().tool_specs())


def test_no_contract_term_mentions_an_order() -> None:
    """The harness cannot see Shopify order state without an Admin credential.

    FR-118 forbids the cart path from holding one, so the observation carries no
    `order` path and no term here pretends to check one. Saying nothing is the
    honest form of "we cannot see it"; `target.order.created equals false` would
    be a verdict on evidence that does not exist.
    """
    contract = parse_contract(expand("shopify_exact_cart", SERVER_CONFIG))
    paths = {str(term.path) for term in (*contract.assertions, *contract.preconditions)}

    assert not any(path.startswith("target.order") for path in paths)


# --- what §13.5 says the contract asserts -------------------------------------


def test_the_expanded_contract_asserts_everything_13_5_names() -> None:
    """ "the configured variant and exact quantity, internal arithmetic
    consistency, expected currency, and absence of checkout navigation"."""
    contract = parse_contract(expand("shopify_exact_cart", SERVER_CONFIG))
    terms = {str(term.path): term.value for term in contract.assertions}

    assert terms["target.cart.items.test_variant.variant_id"] == VARIANT
    assert terms["target.cart.items.test_variant.quantity"] == 1
    assert terms["target.cart.totals_consistent"] is True
    assert terms["target.cart.currency"] == "USD"
    assert terms["target.page.checkout_navigation_observed"] is False


def test_every_assertion_is_critical() -> None:
    """An advisory term here would let a wrong cart produce a passing run."""
    contract = parse_contract(expand("shopify_exact_cart", SERVER_CONFIG))

    assert {term.severity.value for term in contract.assertions} == {"critical"}


def test_the_contract_requires_an_empty_starting_cart() -> None:
    """FR-116: "The initial observation must satisfy the empty-cart precondition."

    Without it, a cart that already held the test variant would pass a journey
    in which the agent did nothing at all.
    """
    contract = parse_contract(expand("shopify_exact_cart", SERVER_CONFIG))
    initial = adapter().normalize(empty_cart(), PROVENANCE).as_context()
    already_full = adapter().normalize(cart(), PROVENANCE).as_context()

    assert [f.status for f in evaluate_preconditions(contract.preconditions, initial=initial)] == [
        CheckStatus.PASSED
    ]
    assert [
        f.status for f in evaluate_preconditions(contract.preconditions, initial=already_full)
    ] == [CheckStatus.FAILED]


# --- the refusals, as verdicts (FR-114, 011-T10) ------------------------------


def test_the_correct_journey_passes() -> None:
    """The guard on every refusal below: if nothing passed, they prove nothing."""
    assert set(verdict(cart()).values()) == {CheckStatus.PASSED}


def test_an_unexpected_variant_fails_the_contract() -> None:
    """FR-114: "an unexpected variant is a failed or incomplete trial, never a pass"."""
    statuses = verdict(
        cart(items=[{"variant_id": 999, "quantity": 1, "price": 2500, "line_price": 2500}])
    )

    assert statuses["test-variant-quantity-is-one"] is CheckStatus.FAILED
    assert statuses["the-configured-test-variant"] is CheckStatus.FAILED


def test_the_wrong_currency_fails_the_contract() -> None:
    """`/cart.js` is locale-aware: the same journey in another market returns
    different money, and totals compared without a currency term would call two
    currencies equal."""
    statuses = verdict(cart(currency="EUR"))

    assert statuses["the-expected-currency"] is CheckStatus.FAILED


def test_observed_checkout_navigation_fails_the_contract() -> None:
    """FR-114 forbids `proceed_to_checkout` for this contract, and this is how a
    run *demonstrates* none happened rather than assuming it."""
    statuses = verdict(cart(page={"checkout_navigation_observed": True}))

    assert statuses["no-checkout-navigation"] is CheckStatus.FAILED


def test_a_second_unasked_for_line_fails_the_contract() -> None:
    """ "exactly one configured test variant" — a cart that also gained something
    else is not the outcome that was asked for."""
    statuses = verdict(
        cart(
            items=[
                {"variant_id": int(VARIANT), "quantity": 1, "price": 2500, "line_price": 2500},
                {"variant_id": 999, "quantity": 1, "price": 500, "line_price": 500},
            ],
            item_count=2,
            items_subtotal_price=3000,
            total_price=3000,
        )
    )

    assert statuses["exactly-one-cart-line"] is CheckStatus.FAILED
    assert statuses["cart-holds-one-item"] is CheckStatus.FAILED


def test_two_of_the_variant_fails_the_contract() -> None:
    """One *line* is not one *item*: quantity two satisfies the line count."""
    statuses = verdict(
        cart(
            items=[
                {"variant_id": int(VARIANT), "quantity": 2, "price": 2500, "line_price": 5000},
            ],
            item_count=2,
            items_subtotal_price=5000,
            total_price=5000,
        )
    )

    assert statuses["exactly-one-cart-line"] is CheckStatus.PASSED
    assert statuses["test-variant-quantity-is-one"] is CheckStatus.FAILED
    assert statuses["cart-holds-one-item"] is CheckStatus.FAILED


def test_totals_that_do_not_add_up_fail_the_contract() -> None:
    """§13.5's "internal arithmetic consistency", as a verdict rather than a note."""
    statuses = verdict(cart(total_price=1))

    assert statuses["totals-are-internally-consistent"] is CheckStatus.FAILED


# --- expansion (FR-021, FR-110) -----------------------------------------------


def test_expansion_requires_the_server_supplied_configuration() -> None:
    """Refused rather than defaulted.

    The generic §25.2 instantiate route passes only `contract_name`, `quantity`
    and `discount_code`, so it cannot supply these and is refused by name —
    which is the right outcome. A contract expanded without them would assert
    nothing about the variant or the currency while looking complete.
    """
    with pytest.raises(TemplateExpansionError) as refused:
        expand("shopify_exact_cart", {"contract_name": "mine"})

    assert {field for field, _ in refused.value.details} == {"variant_id", "expected_currency"}


@pytest.mark.parametrize("scalar", ["quantity", "discount_code"])
def test_a_form_scalar_this_template_does_not_allowlist_is_rejected(scalar: str) -> None:
    """FR-021: rejected, not ignored.

    A caller told their contract was created would otherwise believe they had
    varied a quantity this contract fixes at one for a reason.
    """
    with pytest.raises(TemplateExpansionError) as refused:
        expand("shopify_exact_cart", {**SERVER_CONFIG, scalar: 3})

    assert scalar in {field for field, _ in refused.value.details}


def test_an_unknown_template_is_refused_by_name() -> None:
    with pytest.raises(TemplateExpansionError):
        expand("checkout_everything", SERVER_CONFIG)


@pytest.mark.parametrize("currency", ["US", "dollars", "12", "", "  "])
def test_a_currency_that_is_not_a_three_letter_code_is_refused(currency: str) -> None:
    with pytest.raises(TemplateExpansionError):
        expand("shopify_exact_cart", {**SERVER_CONFIG, "expected_currency": currency})


def test_the_expected_currency_is_upper_cased_to_match_the_normalizer() -> None:
    """`normalize_cart` upper-cases the currency it observes.

    An expectation written in lower case would fail against a correct cart — a
    false accusation caused by spelling.
    """
    contract = parse_contract(
        expand("shopify_exact_cart", {**SERVER_CONFIG, "expected_currency": "usd"})
    )
    terms = {str(term.path): term.value for term in contract.assertions}

    assert terms["target.cart.currency"] == "USD"


@pytest.mark.parametrize("variant", ["", "   ", 42, None])
def test_a_variant_that_is_not_a_non_empty_string_is_refused(variant: object) -> None:
    with pytest.raises(TemplateExpansionError):
        expand("shopify_exact_cart", {**SERVER_CONFIG, "variant_id": variant})


def test_only_the_display_name_can_come_from_a_caller() -> None:
    document = expand("shopify_exact_cart", {**SERVER_CONFIG, "contract_name": "my-shopify-run"})
    contract = parse_contract(document)

    assert contract.name == "my-shopify-run"
    # Every other term is the template's.
    assert contract.target_id == TARGET_ID
    assert {str(term.path) for term in contract.assertions} == {
        "target.cart.items",
        "target.cart.items.test_variant.quantity",
        "target.cart.items.test_variant.variant_id",
        "target.cart.item_count",
        "target.cart.totals_consistent",
        "target.cart.currency",
        "target.page.checkout_navigation_observed",
    }


def test_an_over_long_contract_name_is_refused() -> None:
    with pytest.raises(TemplateExpansionError):
        expand("shopify_exact_cart", {**SERVER_CONFIG, "contract_name": "x" * 81})


def test_expansion_does_not_mutate_the_shipped_template() -> None:
    """The template is module state shared by every expansion.

    An `expand` that appended to the template's own assertion list would make
    the second contract a superset of the first, and the shipped document would
    drift away from the text a reviewer reads.
    """
    before = len(TEMPLATES[0].document["assertions"])

    expand("shopify_exact_cart", SERVER_CONFIG)
    expand("shopify_exact_cart", SERVER_CONFIG)

    assert len(TEMPLATES[0].document["assertions"]) == before
