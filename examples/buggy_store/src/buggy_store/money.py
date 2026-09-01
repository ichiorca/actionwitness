"""Exact decimal money for the store (spec v1.9 §13.2).

"Currency shall use decimal strings or Python `Decimal`, never binary floating
point."

This duplicates a little of what `actionwitness_core.kernel` does, and the
duplication is deliberate. BUILD_ORDER invariant 2 requires the Buggy Store to
build and run with every assurance package absent, so it cannot import the
harness's helpers; `tests/architecture` enforces that. Sharing money handling
across that boundary would be the first crack in the one property this example
exists to demonstrate.

Every amount is quantized to two places on the way out. §13.2 shows `"25.00"`,
`"5.00"`, `"20.00"`, and a total that serialized as `"20.0"` on one run and
`"20.00"` on another would produce two different canonical hashes for one cart.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

__all__ = ["CENTS", "amount_of", "format_amount", "percentage_of"]

#: Money is carried to exactly two decimal places.
CENTS: Final = Decimal("0.01")


def amount_of(value: object) -> Decimal:
    """Coerce an exact amount, refusing every lossy source.

    `float` is rejected rather than converted: `Decimal(0.1)` is not `0.1`, and a
    total that is wrong before it is stored is worse than one that fails loudly.
    `bool` is rejected because it is an `int` in Python and would quietly become
    `1.00`.
    """
    if isinstance(value, bool):
        raise TypeError("a boolean is not an amount")
    if isinstance(value, float):
        raise TypeError("money must not arrive as float; use a decimal string such as '25.00'")
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int | str):
        try:
            candidate = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{value!r} is not an amount") from exc
    else:
        raise TypeError(f"{type(value).__name__} is not an amount")
    if not candidate.is_finite():
        raise ValueError("a non-finite amount has no canonical form")
    return candidate.quantize(CENTS, rounding=ROUND_HALF_UP)


def format_amount(value: Decimal) -> str:
    """Serialize an amount as a two-place decimal string.

    Always `f`-formatted rather than `str()`, which would render `Decimal('1E+2')`
    as `'1E+2'` and give one value two spellings.
    """
    return f"{value.quantize(CENTS, rounding=ROUND_HALF_UP):f}"


def percentage_of(amount: Decimal, percent: int) -> Decimal:
    """`percent`% of `amount`, rounded half-up to two places.

    The seeded catalog's prices make every `SAVE20` result exact, so the rounding
    rule never fires on the shipped fixtures. It is stated anyway: a rule that
    only exists implicitly is a rule that changes when someone adds a product
    priced at $12.34.
    """
    return (amount * Decimal(percent) / Decimal(100)).quantize(CENTS, rounding=ROUND_HALF_UP)
