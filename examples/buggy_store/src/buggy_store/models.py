"""Canonical store state — the exact §13.2 shape, with exact decimal money.

Spec v1.9 §13.2 (the canonical state document and "never binary floating
point"), §13.1 (`line_key` is the key beneath `target.cart.items`), §17.1
(`state_version` is monotonic), Appendix B (the failed-finding example that
shows an unchanged cart at an unchanged `state_version`).

This is the document the adapter will later mount under the `target` namespace,
so its shape *is* the contract-authoring surface: `target.cart.items.mug.quantity`
and `target.cart.total` are paths a human writes by hand, and renaming a field
here silently invalidates every contract and every hashed artifact that names it.
`canonical_document` therefore builds the document explicitly rather than by
`model_dump`, so the serialized shape is reviewable against §13.2 field by field.

Totals are recomputed rather than stored-and-trusted. `Cart` refuses to exist in
a state where its subtotal and total disagree with its lines, which means the
only way to produce a wrong total is to change what the *lines* say - and that is
exactly what the `discount_reported_but_not_applied` profile does, without ever
needing an inconsistent record.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from buggy_store.catalog import (
    CATALOG_BY_LINE_KEY,
    MAX_LINE_QUANTITY,
    discount_percent,
)
from buggy_store.money import amount_of, format_amount, percentage_of

__all__ = [
    "Cart",
    "CartDiscount",
    "CartLine",
    "Order",
    "Preferences",
    "StoreState",
    "TargetState",
    "build_cart",
    "empty_state",
]


class StoreModel(BaseModel):
    """Frozen, unknown-field-rejecting base for every stored record.

    The store validates its own inputs rather than trusting the harness to have
    done it: §15.5's surface is a public API that a person can call from a
    browser, and "the frontend already checked it" is on the constitution's list
    of excuses.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class CartLine(StoreModel):
    """One cart line, keyed in `Cart.items` by its product's `line_key` (§13.1)."""

    product_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    quantity: Annotated[int, Field(ge=1, le=MAX_LINE_QUANTITY)]
    unit_price: Decimal

    @field_validator("unit_price", mode="before")
    @classmethod
    def _exact_unit_price(cls, value: object) -> Decimal:
        return amount_of(value)

    @property
    def line_total(self) -> Decimal:
        return amount_of(self.unit_price * self.quantity)

    def canonical_document(self) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "quantity": self.quantity,
            "unit_price": format_amount(self.unit_price),
        }


class CartDiscount(StoreModel):
    """An applied discount and the amount it took off (§13.2)."""

    code: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    amount: Decimal

    @field_validator("amount", mode="before")
    @classmethod
    def _exact_amount(cls, value: object) -> Decimal:
        return amount_of(value)

    def canonical_document(self) -> dict[str, Any]:
        return {"code": self.code, "amount": format_amount(self.amount)}


class Cart(StoreModel):
    """The canonical cart: lines, an optional discount, and consistent totals."""

    items: Mapping[str, CartLine] = Field(default_factory=dict)
    discount: CartDiscount | None = None
    subtotal: Decimal
    total: Decimal

    @field_validator("subtotal", "total", mode="before")
    @classmethod
    def _exact_totals(cls, value: object) -> Decimal:
        return amount_of(value)

    @model_validator(mode="after")
    def _check_totals_agree_with_the_lines(self) -> Cart:
        """Refuse a cart whose totals do not follow from its own contents.

        A stored total that nothing recomputes is a total that can drift from the
        lines it claims to summarise, and the drift would look exactly like the
        defect this demo is built to exhibit. Keeping the record honest means the
        injected fault has to change the *cart*, not corrupt the arithmetic.
        """
        expected_subtotal = amount_of(
            sum((line.line_total for line in self.items.values()), Decimal(0))
        )
        if self.subtotal != expected_subtotal:
            raise ValueError(
                f"subtotal {format_amount(self.subtotal)} does not match the line total "
                f"{format_amount(expected_subtotal)}"
            )
        reduction = self.discount.amount if self.discount is not None else Decimal(0)
        expected_total = amount_of(expected_subtotal - reduction)
        if self.total != expected_total:
            raise ValueError(
                f"total {format_amount(self.total)} does not match subtotal minus discount "
                f"({format_amount(expected_total)})"
            )
        for line_key, line in self.items.items():
            product = CATALOG_BY_LINE_KEY.get(line_key)
            if product is None:
                raise ValueError(f"{line_key!r} is not a seeded catalog line key")
            if product.product_id != line.product_id:
                raise ValueError(
                    f"line {line_key!r} carries {line.product_id!r}, but that key belongs to "
                    f"{product.product_id!r}"
                )
        return self

    @property
    def item_count(self) -> int:
        """Distinct lines, which is what a `count_equals` on `items` measures."""
        return len(self.items)

    def canonical_document(self) -> dict[str, Any]:
        return {
            "items": {key: line.canonical_document() for key, line in sorted(self.items.items())},
            "discount": self.discount.canonical_document() if self.discount else None,
            "subtotal": format_amount(self.subtotal),
            "total": format_amount(self.total),
        }


def build_cart(items: Mapping[str, CartLine], discount_code: str | None = None) -> Cart:
    """Recompute a consistent cart from its lines and an optional discount code.

    The single place totals are derived. Every mutation path goes through here,
    so "apply a discount" and "change a quantity" cannot disagree about how a
    total is reached.
    """
    subtotal = amount_of(sum((line.line_total for line in items.values()), Decimal(0)))
    discount: CartDiscount | None = None
    if discount_code is not None:
        discount = CartDiscount(
            code=discount_code,
            amount=percentage_of(subtotal, discount_percent(discount_code)),
        )
    reduction = discount.amount if discount is not None else Decimal(0)
    return Cart(
        items=dict(items),
        discount=discount,
        subtotal=subtotal,
        total=amount_of(subtotal - reduction),
    )


class Order(StoreModel):
    """The simulated order (§13.2). No payment, no fulfilment, no real money."""

    created: bool = False
    order_id: str | None = None

    @model_validator(mode="after")
    def _check_order_identity(self) -> Order:
        if self.created is (self.order_id is None):
            raise ValueError("an order is created exactly when it has an order_id")
        return self

    def canonical_document(self) -> dict[str, Any]:
        return {"created": self.created, "order_id": self.order_id}


class Preferences(StoreModel):
    """Saved preferences (§13.2).

    Present from the first commit even though nothing writes them yet. §13.2 is
    explicit about why they exist: "so that a journey can change canonical state
    outside the paths a cart contract asserts. No built-in contract asserts on
    it, and the `undeclared_side_effect` profile writes `preferences.delivery_note`."
    Adding the field later would change the canonical shape after contracts had
    been hashed against it.
    """

    delivery_note: Annotated[str, StringConstraints(max_length=200)] = ""
    gift_wrap: bool = False

    def canonical_document(self) -> dict[str, Any]:
        return {"delivery_note": self.delivery_note, "gift_wrap": self.gift_wrap}


class TargetState(StoreModel):
    """The document mounted under the adapter's `target` namespace (§9.3, §13.2)."""

    cart: Cart
    order: Order = Order()
    preferences: Preferences = Preferences()

    def canonical_document(self) -> dict[str, Any]:
        return {
            "cart": self.cart.canonical_document(),
            "order": self.order.canonical_document(),
            "preferences": self.preferences.canonical_document(),
        }


class StoreState(StoreModel):
    """Canonical state plus its monotonic version (§13.2, §17.1).

    `state_version` sits beside the business payload rather than inside it. §9.3
    makes it observation metadata, so a contract must not be able to reach it as
    `target.state_version`; nesting it would turn a bookkeeping counter into an
    assertable business value.
    """

    state_version: Annotated[int, Field(ge=1)] = 1
    target_state: TargetState

    def canonical_document(self) -> dict[str, Any]:
        return {
            "state_version": self.state_version,
            "target_state": self.target_state.canonical_document(),
        }

    def with_target_state(self, target_state: TargetState) -> StoreState:
        """The next version of this state. Monotonic by construction (FR-034)."""
        return StoreState(state_version=self.state_version + 1, target_state=target_state)


def empty_state() -> StoreState:
    """A freshly seeded workspace: empty cart, no order, default preferences.

    This is the state every run is armed against, and §10.1's prebuilt contract
    asserts exactly it as a precondition - `target.cart.items` count 0 and
    `target.order.created` false.
    """
    return StoreState(state_version=1, target_state=TargetState(cart=build_cart({})))
