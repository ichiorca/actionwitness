"""The seeded catalog and the one allowlisted discount (spec v1.9 §13.1).

Small, stable, and immutable on purpose. Every prebuilt contract, every fixture,
and the recorded demo all address these three products by their seeded
identifiers, so a price or a key changing here changes the meaning of artifacts
that were hashed months earlier.

`line_key` is the load-bearing field. §13.1: "the seeded `line_key` is the
canonical object key beneath `target.cart.items`. It is stable fixture metadata,
unique within the catalog, and returned by `search_catalog`; contract authors
therefore address the mug quantity as `target.cart.items.mug.quantity` without
deriving keys from display names." A key derived from a product title would
change when marketing renamed the mug, and every contract asserting on it would
start failing for a reason no reader could see.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from buggy_store.money import amount_of

__all__ = [
    "CATALOG",
    "CATALOG_BY_LINE_KEY",
    "CATALOG_BY_PRODUCT_ID",
    "DISCOUNTS",
    "MAX_LINE_QUANTITY",
    "MAX_SEARCH_RESULTS",
    "Product",
    "discount_percent",
    "search_catalog",
]

#: Appendix D.2 caps `update_cart.quantity` at 5; zero removes the line.
MAX_LINE_QUANTITY: Final = 5

#: Appendix D.2 caps `search_catalog.max_results` at 5 and defaults it to 3.
MAX_SEARCH_RESULTS: Final = 5
DEFAULT_SEARCH_RESULTS: Final = 3


class Product(BaseModel):
    """One seeded catalog entry (§13.1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    product_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    line_key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=32)]
    name: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    price: Decimal
    stock: Annotated[int, Field(ge=0)]

    @field_validator("price", mode="before")
    @classmethod
    def _exact_price(cls, value: object) -> Decimal:
        return amount_of(value)


#: §13.1's table, transcribed.
CATALOG: Final[tuple[Product, ...]] = (
    Product(
        product_id="mug-ceramic-001",
        line_key="mug",
        name="Ceramic Mug",
        price="25.00",
        stock=20,
    ),
    Product(
        product_id="notebook-001",
        line_key="notebook",
        name="Field Notebook",
        price="12.00",
        stock=35,
    ),
    Product(product_id="tote-001", line_key="tote", name="Canvas Tote", price="18.00", stock=15),
)

CATALOG_BY_PRODUCT_ID: Final[Mapping[str, Product]] = {
    product.product_id: product for product in CATALOG
}

CATALOG_BY_LINE_KEY: Final[Mapping[str, Product]] = {
    product.line_key: product for product in CATALOG
}

#: §13.1's supported discount: "`SAVE20`: 20% off eligible cart subtotal."
#: Allowlisted rather than open: Appendix D.2's `apply_discount` schema enumerates
#: the code, so an unknown one is a rejected argument, not a zero-percent no-op.
DISCOUNTS: Final[Mapping[str, int]] = {"SAVE20": 20}


def discount_percent(code: str) -> int:
    """The percentage for an allowlisted code, or raise `KeyError`."""
    return DISCOUNTS[code]


def search_catalog(query: str, max_results: int = DEFAULT_SEARCH_RESULTS) -> Sequence[Product]:
    """Case-insensitive word search over product names (Appendix D.2).

    Deterministic: matches are returned in catalog order, never in relevance
    order. A search that reordered results between two otherwise identical runs
    would make a recorded trajectory unreproducible for no benefit at this size.
    """
    if not query.strip():
        return ()
    bounded = max(1, min(max_results, MAX_SEARCH_RESULTS))
    words = [word for word in query.lower().split() if word]
    matches = [
        product
        for product in CATALOG
        if any(word in product.name.lower() or word in product.line_key for word in words)
    ]
    return tuple(matches[:bounded])
