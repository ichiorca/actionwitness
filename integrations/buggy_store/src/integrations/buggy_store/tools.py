"""The five allowlisted Buggy Store tools and their deterministic effect map.

Spec v1.9 Appendix D.2 (the tool names, descriptions, and input schemas,
transcribed), §13.4 (the declared target-effect map), §9.1 (`TargetToolSpec`
carries "the allowlisted tool name, input schema, side-effect class, and retry
semantics"), §11.4 (tool-context budgets), FR-015 ("every executable target
adapter shall publish allowlisted `TargetToolSpec` records and a deterministic
tool-effect map").

Allowlisting is the security property, not a convenience. §20.2 and FR-015 make
this the set of things the harness may ask the store to do; a tool absent from
here is refused by the adapter before any HTTP request is formed, so an agent
that invents a tool name reaches nothing.

The effect map is what buys causal attribution. §12.2: "missing effect metadata
disables only causal false-success attribution; it shall not disable generic
assertion evaluation or cause the harness to infer an effect." Publishing it is
what lets FR-055 name `apply_discount` as the action that claimed the total it
did not change - without it the same run reports a plain `assertion_mismatch`
and nobody learns which call lied.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from actionwitness_core.ports.enums import RetrySemantics, SideEffectClass
from actionwitness_core.ports.models import TargetToolSpec

__all__ = [
    "APPLY_DISCOUNT",
    "EFFECT_MAP",
    "GET_CART",
    "PROCEED_TO_CHECKOUT",
    "SEARCH_CATALOG",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "UPDATE_CART",
    "spec_for",
]

SEARCH_CATALOG: Final = "search_catalog"
GET_CART: Final = "get_cart"
UPDATE_CART: Final = "update_cart"
APPLY_DISCOUNT: Final = "apply_discount"
PROCEED_TO_CHECKOUT: Final = "proceed_to_checkout"

#: Appendix D.2's `search_catalog` schema, transcribed.
_SEARCH_CATALOG_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "description": "Product words to search for.",
        },
        "max_results": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "default": 3,
            "description": "Maximum catalog matches to return.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

_GET_CART_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_UPDATE_CART_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "product_id": {
            "type": "string",
            "enum": ["mug-ceramic-001", "notebook-001", "tote-001"],
            "description": "Stable seeded product identifier.",
        },
        "quantity": {
            "type": "integer",
            "minimum": 0,
            "maximum": 5,
            "description": "Absolute cart quantity; zero removes the product.",
        },
        "request_id": {
            "type": "string",
            "minLength": 8,
            "maxLength": 80,
            "description": "Caller-generated idempotency key reused only for an identical payload.",
        },
    },
    "required": ["product_id", "quantity", "request_id"],
    "additionalProperties": False,
}

_APPLY_DISCOUNT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "enum": ["SAVE20"],
            "description": "Allowlisted demo discount code.",
        }
    },
    "required": ["code"],
    "additionalProperties": False,
}

_PROCEED_TO_CHECKOUT_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "request_id": {
            "type": "string",
            "minLength": 8,
            "maxLength": 80,
            "description": "Caller-generated idempotency key for this checkout attempt.",
        }
    },
    "required": ["request_id"],
    "additionalProperties": False,
}


#: Appendix D.2's five tools, with §13.4's effect prefixes attached to each.
#:
#: Retry semantics are read off Appendix D.2's closing paragraph rather than
#: guessed: `update_cart` and `proceed_to_checkout` both return their first
#: persisted result for an identical `(request_id, payload)`; `apply_discount`
#: has no request ID and is a successful no-op on repeat; the two reads change
#: nothing at all.
TOOL_SPECS: Final[tuple[TargetToolSpec, ...]] = (
    TargetToolSpec(
        name=SEARCH_CATALOG,
        description=(
            "Search the seeded demo catalog by product name and return stable product "
            "IDs and line keys. This tool does not change store state."
        ),
        input_schema=_SEARCH_CATALOG_SCHEMA,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetrySemantics.READ_ONLY_SAFE,
    ),
    TargetToolSpec(
        name=GET_CART,
        description=(
            "Return bounded canonical cart contents, discount, subtotal, total, and "
            "state version. This tool does not change store state."
        ),
        input_schema=_GET_CART_SCHEMA,
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetrySemantics.READ_ONLY_SAFE,
    ),
    TargetToolSpec(
        name=UPDATE_CART,
        description=(
            "Set one seeded product to an absolute cart quantity using a retry-safe "
            "request ID. Quantity zero removes the line; positive values replace its "
            "quantity."
        ),
        input_schema=_UPDATE_CART_SCHEMA,
        side_effect=SideEffectClass.MUTATING,
        retry=RetrySemantics.IDEMPOTENT_BY_REQUEST_ID,
        effect_paths=("target.cart.items", "target.cart.subtotal", "target.cart.total"),
    ),
    TargetToolSpec(
        name=APPLY_DISCOUNT,
        description=(
            "Apply one allowlisted discount to the canonical cart. Reapplying the "
            "active code returns already_applied and does not change state."
        ),
        input_schema=_APPLY_DISCOUNT_SCHEMA,
        side_effect=SideEffectClass.MUTATING,
        retry=RetrySemantics.NATURALLY_IDEMPOTENT,
        effect_paths=("target.cart.discount", "target.cart.total"),
    ),
    TargetToolSpec(
        name=PROCEED_TO_CHECKOUT,
        description=(
            "Request visible human approval for the exact cart and create one "
            "simulated order only after a valid single-use confirmation."
        ),
        input_schema=_PROCEED_TO_CHECKOUT_SCHEMA,
        side_effect=SideEffectClass.PROTECTED_MUTATING,
        retry=RetrySemantics.IDEMPOTENT_BY_REQUEST_ID,
        effect_paths=("target.order",),
    ),
)

#: The allowlist, as a set. A name absent from here never becomes an HTTP request.
TOOL_NAMES: Final[frozenset[str]] = frozenset(spec.name for spec in TOOL_SPECS)

_BY_NAME: Final[Mapping[str, TargetToolSpec]] = {spec.name: spec for spec in TOOL_SPECS}

#: §13.4's table, derived from the specs so the two cannot disagree.
#:
#: Derived rather than transcribed a second time on purpose: a hand-written copy
#: beside `TOOL_SPECS` would eventually declare an effect the spec record does
#: not, and the harness would attribute a failure to a path the tool never
#: claimed.
EFFECT_MAP: Final[Mapping[str, tuple[str, ...]]] = {
    spec.name: tuple(str(path) for path in spec.effect_paths) for spec in TOOL_SPECS
}


def spec_for(tool_name: str) -> TargetToolSpec | None:
    """The published spec for `tool_name`, or `None` when it is not allowlisted."""
    return _BY_NAME.get(tool_name)


def published_names() -> Sequence[str]:
    """Tool names in publication order, for error messages and templates."""
    return tuple(spec.name for spec in TOOL_SPECS)
