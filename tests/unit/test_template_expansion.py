"""012-T5 — FR-021's template expansion.

FR-021: "A human or agent shall be able to submit the visible flat form
containing `template_id`, optional `contract_name`, and only the scalar
parameters allowlisted by that template. FastAPI shall expand the trusted
template; the declarative form shall never accept nested assertions, policies,
paths, or arbitrary JSON."

The load-bearing word is **allowlisted**. Expansion is the point where a value
a caller chose meets a document that decides what "correct" means, and the
whole safety of the declarative form is that the caller picks from a fixed set
of bounded scalars while every assertion, policy, path and target comes from
the template. These tests pin both halves: what a caller may vary, and what
they may not reach at all.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.models import parse_contract
from integrations.buggy_store.templates import (
    MAX_CONTRACT_NAME_CHARS,
    MAX_QUANTITY,
    TEMPLATES,
    TemplateExpansionError,
    expand,
    template_for,
)

pytestmark = pytest.mark.unit

CANONICAL = "one_mug_save20_no_checkout"
RETRY = "retry_safe_cart_update"
CHECKOUT = "confirmed_checkout_only"


def _assertion(document: dict, assertion_id: str) -> dict:
    return next(item for item in document["assertions"] if item["id"] == assertion_id)


# --- the default is the template itself --------------------------------------


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_expanding_with_no_parameters_reproduces_the_seeded_template(template) -> None:
    """A form submitted with nothing filled in must create what was on offer.

    `retry_safe_cart_update` asserts two mugs and the others assert one, so a
    default written as a literal would silently rewrite one of them. The
    expansion derives each default from the assertion it is about to replace,
    which makes this identity structural rather than a coincidence somebody
    has to maintain.
    """
    # Arrange / Act
    expanded = expand(template.template_id, {})

    # Assert
    assert expanded == dict(template.document)


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_every_expansion_is_a_valid_contract(template) -> None:
    """Expansion may not produce a document the contract boundary rejects.

    A caller reaching `parse_contract`'s error through the form would be told
    their *input* was invalid when the template's arithmetic was.
    """
    # Arrange
    quantity = MAX_QUANTITY if "quantity" in template.parameters else None
    parameters = {} if quantity is None else {"quantity": quantity}

    # Act
    contract = parse_contract(expand(template.template_id, parameters))

    # Assert
    assert contract.target_id == template.document["target_id"]


# --- what a caller may vary ---------------------------------------------------


def test_quantity_rewrites_the_assertion_and_the_price_it_implies() -> None:
    """Three mugs at 25.00 with SAVE20 is 60.00, and the contract must say so.

    Rewriting the quantity without the total would leave a contract asserting
    three mugs at the price of one — and it would *fail* a correct journey,
    which is the worst way for an assurance harness to be wrong.
    """
    # Arrange / Act
    document = expand(CANONICAL, {"quantity": 3})

    # Assert
    assert _assertion(document, "mug-quantity")["value"] == 3
    assert _assertion(document, "discounted-total")["value"] == "60.00"


def test_the_prose_moves_with_the_assertions() -> None:
    """The description is what a person reads to know what the run asserts.

    A contract whose text says "one mug" while its terms say three is exactly
    the disagreement between claim and state this product exists to catch; it
    must not be manufactured by the tool that creates contracts.
    """
    # Arrange / Act
    document = expand(CANONICAL, {"quantity": 4})

    # Assert
    assert "four mugs" in document["description"]
    assert "four ceramic mugs" in document["intent"]
    assert "one ceramic mug" not in document["intent"]


def test_a_chosen_name_survives_the_expansion() -> None:
    """`contract_name` is applied last, so the generated name never wins."""
    # Arrange / Act
    document = expand(CANONICAL, {"quantity": 2, "contract_name": "Rehearsal contract"})

    # Assert
    assert document["name"] == "Rehearsal contract"


def test_the_retry_template_scales_its_subtotal() -> None:
    """The retry contract's evidence is that the subtotal was charged once."""
    # Arrange / Act
    document = expand(RETRY, {"quantity": 5})

    # Assert
    assert _assertion(document, "mug-quantity-after-retry")["value"] == 5
    assert _assertion(document, "subtotal-charged-once")["value"] == "125.00"


# --- what a caller may not reach ---------------------------------------------


def test_a_scalar_the_template_does_not_allowlist_is_rejected() -> None:
    """FR-021's allowlist, and why silence would be the wrong answer.

    `confirmed_checkout_only` says nothing about quantity. Ignoring the field
    would hand back a contract the caller believes constrains a cart size it
    never mentions — a false sense of coverage produced by the harness itself.
    """
    # Arrange / Act
    with pytest.raises(TemplateExpansionError) as raised:
        expand(CHECKOUT, {"quantity": 2})

    # Assert
    assert [field for field, _ in raised.value.details] == ["quantity"]


def test_a_discount_code_is_rejected_by_a_template_with_no_discount_term() -> None:
    """The same rule for the other scalar, on a template that does take one."""
    # Arrange / Act
    with pytest.raises(TemplateExpansionError) as raised:
        expand(RETRY, {"discount_code": "SAVE20"})

    # Assert
    assert [field for field, _ in raised.value.details] == ["discount_code"]


@pytest.mark.parametrize("quantity", [0, MAX_QUANTITY + 1, -1])
def test_a_quantity_outside_the_bounds_is_refused(quantity: int) -> None:
    """Appendix G's 1..5. An unbounded quantity is unbounded work downstream."""
    # Arrange / Act / Assert
    with pytest.raises(TemplateExpansionError):
        expand(CANONICAL, {"quantity": quantity})


@pytest.mark.parametrize("quantity", ["2", 2.0, True, None.__class__])
def test_a_quantity_that_is_not_a_whole_number_is_refused(quantity: object) -> None:
    """`True` is the one worth naming: it is an `int`, and `1 <= True <= 5`.

    Without the explicit `bool` check it would sail through the bounds and
    expand to a one-mug contract, which is a contract nobody asked for.
    """
    # Arrange / Act / Assert
    with pytest.raises(TemplateExpansionError):
        expand(CANONICAL, {"quantity": quantity})


def test_an_unknown_discount_code_is_refused_rather_than_ignored() -> None:
    """§13.1 allowlists codes. An unknown one is a rejected argument, never a
    zero-percent no-op that quietly asserts the undiscounted total."""
    # Arrange / Act / Assert
    with pytest.raises(TemplateExpansionError):
        expand(CANONICAL, {"discount_code": "SAVE99"})


def test_an_unknown_template_is_refused() -> None:
    """The `template_id` control is a choice from a published set, not a key."""
    # Arrange / Act / Assert
    with pytest.raises(TemplateExpansionError):
        expand("../../etc/passwd", {})


def test_an_over_long_name_is_refused() -> None:
    """The name reaches a column, a listing and an error message (§20.2)."""
    # Arrange / Act / Assert
    with pytest.raises(TemplateExpansionError):
        expand(CANONICAL, {"contract_name": "x" * (MAX_CONTRACT_NAME_CHARS + 1)})


def test_every_offending_field_is_named_at_once() -> None:
    """One round trip per mistake makes a form miserable to fill in."""
    # Arrange / Act
    with pytest.raises(TemplateExpansionError) as raised:
        expand(CANONICAL, {"quantity": 99, "discount_code": "NOPE", "contract_name": ""})

    # Assert
    assert {field for field, _ in raised.value.details} == {
        "quantity",
        "discount_code",
        "contract_name",
    }


def test_nothing_a_caller_sends_becomes_a_contract_term() -> None:
    """The form "shall never accept nested assertions, policies, paths, or
    arbitrary JSON" (FR-021).

    The strongest version of that claim: hand expansion a payload full of
    contract-shaped keys and show the result is the template's own terms
    untouched. Extra keys are not merged, not stored, and not reachable.
    """
    # Arrange
    hostile = {
        "assertions": [{"id": "x", "path": "target.cart.total", "operator": "exists"}],
        "policies": [{"type": "forbidden_tool", "tool": "proceed_to_checkout"}],
        "target_id": "some-other-target",
        "redaction": {"paths": []},
        "schema_version": "9.9",
    }

    # Act
    document = expand(CANONICAL, hostile)

    # Assert
    template = template_for(CANONICAL)
    assert template is not None
    assert document == dict(template.document)
