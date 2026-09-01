"""Buggy Store catalog and canonical-state gates (spec v1.9 §13.1, §13.2; 003-T1).

§26.1 puts "Buggy Store decimal calculations" here rather than in
`actionwitness_core`, and that placement is the point: the store owns its own
money handling because BUILD_ORDER invariant 2 forbids it from importing the
harness's.

Two properties carry this module.

**The canonical document matches §13.2 exactly.** It is the contract-authoring
surface - a human writes `target.cart.items.mug.quantity` by hand - so a renamed
field silently invalidates every contract and every hashed artifact naming it.
The shape is asserted against the specification's own example rather than
against whatever the models happen to emit.

**Money is never a float.** Not "is usually a Decimal": the constructor refuses
one, because a total that is wrong before it is stored is worse than one that
fails loudly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from buggy_store.catalog import (
    CATALOG,
    CATALOG_BY_LINE_KEY,
    CATALOG_BY_PRODUCT_ID,
    DISCOUNTS,
    MAX_LINE_QUANTITY,
    search_catalog,
)
from buggy_store.models import (
    Cart,
    CartDiscount,
    CartLine,
    Order,
    Preferences,
    StoreState,
    TargetState,
    build_cart,
    empty_state,
)
from buggy_store.money import amount_of, format_amount, percentage_of
from pydantic import ValidationError

MUG = CATALOG_BY_LINE_KEY["mug"]


def _line(line_key: str, quantity: int) -> CartLine:
    product = CATALOG_BY_LINE_KEY[line_key]
    return CartLine(product_id=product.product_id, quantity=quantity, unit_price=product.price)


# --- the seeded catalog (§13.1) ---------------------------------------------


@pytest.mark.unit
def test_the_catalog_is_the_specs_table_verbatim() -> None:
    """A price or key changing here changes artifacts hashed months earlier."""
    assert [
        (p.product_id, p.line_key, p.name, format_amount(p.price), p.stock) for p in CATALOG
    ] == [
        ("mug-ceramic-001", "mug", "Ceramic Mug", "25.00", 20),
        ("notebook-001", "notebook", "Field Notebook", "12.00", 35),
        ("tote-001", "tote", "Canvas Tote", "18.00", 15),
    ]


@pytest.mark.unit
def test_line_keys_are_unique_and_stable_fixture_metadata() -> None:
    """§13.1: unique within the catalog, and never derived from a display name."""
    keys = [product.line_key for product in CATALOG]
    assert len(set(keys)) == len(keys)
    assert set(CATALOG_BY_LINE_KEY) == set(keys)
    # Derived from the name, "Ceramic Mug" would key as `ceramic_mug`; it does not.
    assert CATALOG_BY_LINE_KEY["mug"].name == "Ceramic Mug"


@pytest.mark.unit
def test_product_ids_are_unique_and_addressable() -> None:
    assert set(CATALOG_BY_PRODUCT_ID) == {"mug-ceramic-001", "notebook-001", "tote-001"}


@pytest.mark.unit
def test_the_only_supported_discount_is_save20_at_twenty_percent() -> None:
    assert DISCOUNTS == {"SAVE20": 20}


@pytest.mark.unit
def test_a_catalog_price_is_an_exact_decimal_not_a_float() -> None:
    assert all(isinstance(product.price, Decimal) for product in CATALOG)
    assert CATALOG_BY_LINE_KEY["mug"].price == Decimal("25.00")


@pytest.mark.unit
def test_the_catalog_is_immutable() -> None:
    with pytest.raises(ValidationError):
        CATALOG[0].price = Decimal("1.00")


# --- search (Appendix D.2) --------------------------------------------------


@pytest.mark.unit
def test_search_matches_on_name_words_case_insensitively() -> None:
    assert [p.line_key for p in search_catalog("mug")] == ["mug"]
    assert [p.line_key for p in search_catalog("CERAMIC")] == ["mug"]
    assert [p.line_key for p in search_catalog("notebook")] == ["notebook"]


@pytest.mark.unit
def test_search_returns_catalog_order_so_a_trajectory_is_reproducible() -> None:
    """Relevance ordering that varied between runs would break recorded replay."""
    assert [p.line_key for p in search_catalog("ceramic canvas")] == ["mug", "tote"]
    assert search_catalog("ceramic canvas") == search_catalog("canvas ceramic")


@pytest.mark.unit
def test_search_honours_its_result_bound() -> None:
    assert len(search_catalog("a", max_results=1)) <= 1
    assert len(search_catalog("mug notebook tote", max_results=5)) == 3


@pytest.mark.unit
def test_an_empty_query_matches_nothing_rather_than_everything() -> None:
    assert search_catalog("") == ()
    assert search_catalog("   ") == ()


@pytest.mark.unit
def test_an_unknown_query_returns_no_matches() -> None:
    assert search_catalog("bicycle") == ()


# --- money (§13.2) ----------------------------------------------------------


@pytest.mark.unit
def test_a_float_amount_is_refused_rather_than_rounded() -> None:
    """§13.2: "never binary floating point"."""
    with pytest.raises(TypeError, match="float"):
        amount_of(25.0)


@pytest.mark.unit
def test_a_boolean_is_not_an_amount() -> None:
    with pytest.raises(TypeError):
        amount_of(True)


@pytest.mark.unit
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_amount_is_refused(literal: str) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        amount_of(literal)


@pytest.mark.unit
def test_amounts_serialize_with_exactly_two_places() -> None:
    """One value must have one spelling, or one cart hashes two ways."""
    assert format_amount(Decimal("20")) == "20.00"
    assert format_amount(Decimal("20.0")) == "20.00"
    assert format_amount(Decimal("20.00")) == "20.00"
    assert format_amount(Decimal("1E+2")) == "100.00"


@pytest.mark.unit
def test_the_discount_arithmetic_matches_the_specs_example() -> None:
    """§13.2: a 25.00 subtotal with SAVE20 gives a 5.00 discount and a 20.00 total."""
    assert format_amount(percentage_of(Decimal("25.00"), 20)) == "5.00"


@pytest.mark.unit
def test_percentage_rounding_is_half_up_and_stated() -> None:
    """No shipped fixture reaches this, which is exactly why it is pinned."""
    assert format_amount(percentage_of(Decimal("12.34"), 20)) == "2.47"
    assert format_amount(percentage_of(Decimal("0.01"), 20)) == "0.00"


# --- the canonical document (§13.2) -----------------------------------------


@pytest.mark.unit
def test_the_canonical_document_matches_the_specs_example_exactly() -> None:
    """Transcribed from §13.2 rather than generated from the models."""
    state = StoreState(
        state_version=4,
        target_state=TargetState(cart=build_cart({"mug": _line("mug", 1)}, "SAVE20")),
    )
    assert state.canonical_document() == {
        "state_version": 4,
        "target_state": {
            "cart": {
                "items": {
                    "mug": {
                        "product_id": "mug-ceramic-001",
                        "quantity": 1,
                        "unit_price": "25.00",
                    }
                },
                "discount": {"code": "SAVE20", "amount": "5.00"},
                "subtotal": "25.00",
                "total": "20.00",
            },
            "order": {"created": False, "order_id": None},
            "preferences": {"delivery_note": "", "gift_wrap": False},
        },
    }


@pytest.mark.unit
def test_every_money_field_serializes_as_a_string() -> None:
    """A JSON number here would be a float by the time it reached a hash."""
    cart = build_cart({"mug": _line("mug", 2)}, "SAVE20").canonical_document()
    assert isinstance(cart["subtotal"], str)
    assert isinstance(cart["total"], str)
    assert isinstance(cart["discount"]["amount"], str)
    assert isinstance(cart["items"]["mug"]["unit_price"], str)


@pytest.mark.unit
def test_an_empty_cart_serializes_a_null_discount_rather_than_omitting_it() -> None:
    """A key that appears only sometimes makes `absent` mean two different things."""
    document = empty_state().canonical_document()["target_state"]["cart"]
    assert document == {"items": {}, "discount": None, "subtotal": "0.00", "total": "0.00"}


@pytest.mark.unit
def test_the_seeded_state_satisfies_the_prebuilt_contracts_preconditions() -> None:
    """§10.1 arms against `items` count 0 and `order.created` false."""
    target = empty_state().canonical_document()["target_state"]
    assert target["cart"]["items"] == {}
    assert target["order"]["created"] is False


@pytest.mark.unit
def test_state_version_is_metadata_and_not_reachable_as_a_business_path() -> None:
    """§9.3: a contract must not be able to assert on `target.state_version`."""
    document = empty_state().canonical_document()
    assert "state_version" in document
    assert "state_version" not in document["target_state"]


@pytest.mark.unit
def test_preferences_exist_from_the_first_commit() -> None:
    """§13.2 explains why: the undeclared-side-effect profile writes into them."""
    assert empty_state().canonical_document()["target_state"]["preferences"] == {
        "delivery_note": "",
        "gift_wrap": False,
    }


@pytest.mark.unit
def test_items_serialize_in_a_deterministic_key_order() -> None:
    items = {"tote": _line("tote", 1), "mug": _line("mug", 1)}
    document = build_cart(items).canonical_document()
    assert list(document["items"]) == ["mug", "tote"]


# --- consistency invariants -------------------------------------------------


@pytest.mark.unit
def test_totals_are_derived_from_the_lines() -> None:
    cart = build_cart({"mug": _line("mug", 2), "notebook": _line("notebook", 1)})
    assert format_amount(cart.subtotal) == "62.00"
    assert format_amount(cart.total) == "62.00"
    assert cart.discount is None


@pytest.mark.unit
def test_a_discount_reduces_the_total_but_not_the_subtotal() -> None:
    cart = build_cart({"mug": _line("mug", 1)}, "SAVE20")
    assert format_amount(cart.subtotal) == "25.00"
    assert format_amount(cart.discount.amount) == "5.00"
    assert format_amount(cart.total) == "20.00"


@pytest.mark.unit
def test_a_cart_whose_subtotal_contradicts_its_lines_cannot_be_constructed() -> None:
    """The injected fault must change the cart, never corrupt the arithmetic."""
    with pytest.raises(ValidationError, match="does not match the line total"):
        Cart(items={"mug": _line("mug", 1)}, subtotal="99.00", total="99.00")


@pytest.mark.unit
def test_a_cart_whose_total_ignores_its_discount_cannot_be_constructed() -> None:
    with pytest.raises(ValidationError, match="does not match subtotal minus discount"):
        Cart(
            items={"mug": _line("mug", 1)},
            discount=CartDiscount(code="SAVE20", amount="5.00"),
            subtotal="25.00",
            total="25.00",
        )


@pytest.mark.unit
def test_a_line_keyed_under_the_wrong_product_is_refused() -> None:
    """`items` is keyed by `line_key`; a mismatch would misdirect every contract path."""
    notebook = CATALOG_BY_LINE_KEY["notebook"]
    with pytest.raises(ValidationError, match="belongs to"):
        Cart(
            items={
                "mug": CartLine(
                    product_id=notebook.product_id, quantity=1, unit_price=notebook.price
                )
            },
            subtotal="12.00",
            total="12.00",
        )


@pytest.mark.unit
def test_a_line_key_outside_the_catalog_is_refused() -> None:
    with pytest.raises(ValidationError, match="not a seeded catalog line key"):
        Cart(
            items={"bicycle": _line("mug", 1)},
            subtotal="25.00",
            total="25.00",
        )


@pytest.mark.unit
@pytest.mark.parametrize("quantity", [0, -1, MAX_LINE_QUANTITY + 1])
def test_a_line_quantity_outside_the_schema_bound_is_refused(quantity: int) -> None:
    """Appendix D.2 bounds quantity at 1..5; zero removes the line rather than storing it."""
    with pytest.raises(ValidationError):
        CartLine(product_id=MUG.product_id, quantity=quantity, unit_price=MUG.price)


@pytest.mark.unit
def test_an_order_is_created_exactly_when_it_has_an_identifier() -> None:
    assert Order().created is False
    assert Order(created=True, order_id="order_1").order_id == "order_1"
    with pytest.raises(ValidationError, match="exactly when"):
        Order(created=True)
    with pytest.raises(ValidationError, match="exactly when"):
        Order(created=False, order_id="order_1")


@pytest.mark.unit
def test_state_version_advances_monotonically() -> None:
    state = empty_state()
    assert state.state_version == 1
    assert state.with_target_state(state.target_state).state_version == 2


@pytest.mark.unit
def test_stored_state_is_immutable() -> None:
    state = empty_state()
    with pytest.raises(ValidationError):
        state.state_version = 99


@pytest.mark.unit
def test_an_unknown_field_is_refused_rather_than_retained() -> None:
    """§15.5 is a public surface; "the frontend already checked it" is not a defence."""
    with pytest.raises(ValidationError):
        Preferences(delivery_note="", gift_wrap=False, surprise=True)


# --- the boundary this milestone exists to establish ------------------------


@pytest.mark.unit
def test_the_store_imports_no_assurance_package() -> None:
    """BUILD_ORDER invariant 2, asserted at runtime as well as by the AST gate."""
    import ast
    from pathlib import Path

    import buggy_store

    source_root = Path(buggy_store.__file__).parent
    for module in sorted(source_root.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        roots = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
        }
        assert roots & {"actionwitness_core", "actionwitness_service", "integrations"} == set(), (
            f"{module.name} imports an assurance package"
        )
