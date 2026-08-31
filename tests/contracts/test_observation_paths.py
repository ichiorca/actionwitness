"""Restricted-path gates (spec v1.9 §10.2, §10.4, §13.4, FR-051; 002-T3).

A path is untrusted input that selects data, so most of what matters here is what
the parser *refuses*. The hostile-input table is the substance of this module: a
wildcard, a JSONPath expression, or a bracket index that slipped through would
let a contract assert on a value its author never named, and an attribute reach
would let one read Python objects rather than observed state.

The resolver's contract is the other half. §9.4 makes "present and null" and
"absent" different answers, so `Resolution.found` is asserted separately from
`Resolution.value` throughout.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.limits import MAX_OBSERVATION_PATH_LENGTH
from actionwitness_core.contracts.paths import MISSING, ObservationPath, resolve
from actionwitness_core.kernel import CoreErrorCode, PathError

CONTEXT = {
    "target": {
        "cart": {
            "items": {"mug": {"quantity": 1}},
            "lines": [{"sku": "mug"}, {"sku": "pen"}],
            "total": "20.00",
            "discount": None,
        },
        "cartridge": {"quantity": 99},
        "name": "demo",
        "order": {"created": False},
    }
}


def _parse(text: str) -> ObservationPath:
    return ObservationPath.parse(text)


# --- accepted grammar -------------------------------------------------------


@pytest.mark.contracts
def test_the_specs_own_example_path_parses() -> None:
    path = _parse("target.cart.items.mug.quantity")
    assert path.segments == ("target", "cart", "items", "mug", "quantity")
    assert str(path) == "target.cart.items.mug.quantity"
    assert path.namespace == "target"


@pytest.mark.contracts
@pytest.mark.parametrize(
    "text",
    [
        "target",
        "target.cart",
        "target.ticket.status",
        "target.cart.line_items",
        "target.cart.line-items",
        "target._internal",
        "target.lines.0.sku",
        "target.lines.10",
    ],
)
def test_identifier_and_integer_segments_are_accepted(text: str) -> None:
    assert str(_parse(text)) == text


# --- hostile input ----------------------------------------------------------


@pytest.mark.contracts
@pytest.mark.parametrize(
    "text",
    [
        "",
        ".",
        ".target",
        "target.",
        "target..cart",
        "target.*",
        "**.email",
        "$.target.cart",
        "target.cart[0]",
        "target.cart['total']",
        'target.cart["total"]',
        "target.cart.items[?(@.sku=='mug')]",
        "target.cart.total()",
        "target/cart/total",
        "target.cart.items[0:2]",
        "target cart",
        "target.\tcart",
        "target.ca\x00rt",
        "target.01",
        "target.-leading",
        "target.1abc",
        "target.café",
        "target.cart\\total",
    ],
)
def test_expression_language_and_malformed_paths_are_refused(text: str) -> None:
    with pytest.raises(PathError) as excinfo:
        _parse(text)
    assert excinfo.value.code is CoreErrorCode.INVALID_OBSERVATION_PATH
    assert excinfo.value.details, "a rejection must name the offending input"


@pytest.mark.contracts
def test_a_path_longer_than_the_limit_is_refused() -> None:
    """§10.4 bounds a path at 200 characters; the bound is also a depth bound."""
    at_limit = ".".join(["a"] * 99 + ["bb"])
    assert len(at_limit) == MAX_OBSERVATION_PATH_LENGTH
    assert _parse(at_limit)

    with pytest.raises(PathError, match="at most 200 characters"):
        _parse(at_limit + "c")


@pytest.mark.contracts
@pytest.mark.parametrize("value", [None, 42, ["target", "cart"], b"target.cart"])
def test_a_non_string_path_is_refused_rather_than_coerced(value: object) -> None:
    with pytest.raises(PathError):
        ObservationPath.parse(value)


# --- resolution -------------------------------------------------------------


@pytest.mark.contracts
def test_a_present_value_resolves_exactly() -> None:
    assert resolve(_parse("target.cart.items.mug.quantity"), CONTEXT) == (
        resolve(_parse("target.cart.items.mug.quantity"), CONTEXT)
    )
    resolution = resolve(_parse("target.cart.total"), CONTEXT)
    assert resolution.found is True
    assert resolution.value == "20.00"


@pytest.mark.contracts
def test_a_present_null_is_found_and_is_not_absent() -> None:
    """§9.4: `exists` passes on a present null; `absent` fails on one."""
    resolution = resolve(_parse("target.cart.discount"), CONTEXT)
    assert resolution.found is True
    assert resolution.value is None


@pytest.mark.contracts
def test_a_missing_path_resolves_to_missing_rather_than_raising() -> None:
    """FR-051: a missing path is a structured mismatch, not an exception."""
    assert resolve(_parse("target.cart.shipping"), CONTEXT) is MISSING
    assert resolve(_parse("target.cart.items.pen.quantity"), CONTEXT) is MISSING
    assert resolve(_parse("nosuchnamespace.cart"), CONTEXT) is MISSING


@pytest.mark.contracts
def test_a_sequence_is_indexed_only_by_an_integer_segment_in_range() -> None:
    assert resolve(_parse("target.cart.lines.1.sku"), CONTEXT).value == "pen"
    assert resolve(_parse("target.cart.lines.2"), CONTEXT) is MISSING
    assert resolve(_parse("target.cart.lines.sku"), CONTEXT) is MISSING


@pytest.mark.contracts
def test_a_string_is_never_indexed_as_a_sequence() -> None:
    """`target.name.0` must not quietly yield 'd'; a character is not observed state."""
    assert resolve(_parse("target.name.0"), CONTEXT) is MISSING
    assert resolve(_parse("target.name.length"), CONTEXT) is MISSING


@pytest.mark.contracts
def test_resolution_walks_data_and_never_reaches_a_python_attribute() -> None:
    """A segment naming a dunder is an ordinary key that simply is not present."""
    for text in ("target.__class__", "target.cart.__dict__", "target.cart.keys"):
        assert resolve(_parse(text), CONTEXT) is MISSING


@pytest.mark.contracts
def test_resolving_through_a_scalar_terminates_as_missing() -> None:
    assert resolve(_parse("target.order.created.value"), CONTEXT) is MISSING


@pytest.mark.contracts
def test_resolution_does_not_mutate_the_context() -> None:
    """Evaluation is pure; a resolver that inserted defaults would corrupt evidence."""
    import copy

    before = copy.deepcopy(CONTEXT)
    resolve(_parse("target.cart.shipping.address"), CONTEXT)
    assert before == CONTEXT


# --- ancestry and overlap (§13.4) -------------------------------------------


@pytest.mark.contracts
def test_ancestry_is_evaluated_at_a_dotted_key_boundary() -> None:
    """`target.cart` textually prefixes `target.cartridge` but does not contain it."""
    cart = _parse("target.cart")
    cartridge = _parse("target.cartridge")
    assert cart.is_ancestor_of(_parse("target.cart.total"))
    assert not cart.is_ancestor_of(cartridge)
    assert not cart.overlaps(cartridge)


@pytest.mark.contracts
def test_a_path_is_its_own_ancestor_so_an_exact_match_overlaps() -> None:
    total = _parse("target.cart.total")
    assert total.is_ancestor_of(total)
    assert total.overlaps(total)


@pytest.mark.contracts
def test_overlap_is_symmetric_in_both_directions() -> None:
    """§13.4: overlap holds when *either* path is an ancestor of the other."""
    prefix = _parse("target.cart")
    deep = _parse("target.cart.items.mug.quantity")
    assert prefix.overlaps(deep)
    assert deep.overlaps(prefix)


@pytest.mark.contracts
def test_paths_sort_lexicographically_by_segment() -> None:
    """§17.2/§23 order findings and candidates by canonical path."""
    unsorted = [
        _parse("target.cart.total"),
        _parse("target.cart.items"),
        _parse("target.cartridge"),
        _parse("target.cart"),
    ]
    assert [str(path) for path in sorted(unsorted)] == [
        "target.cart",
        "target.cart.items",
        "target.cart.total",
        "target.cartridge",
    ]


@pytest.mark.contracts
def test_paths_are_immutable_and_hashable() -> None:
    """A path is used as a mapping key in effect maps and exemption sets."""
    path = _parse("target.cart.total")
    assert {path: "effect"}[_parse("target.cart.total")] == "effect"
    with pytest.raises(AttributeError):
        path.segments = ("hacked",)
