"""Prebuilt Buggy Store contract templates (spec v1.9 §10.1, FR-020, FR-023).

FR-020: "Tier 1 shall provide at least three Buggy Store integration contracts."
BUILD_ORDER §7/M2 places them here rather than in the core - a contract that
names `target.cart.total` and `SAVE20` is target-specific by construction, and
the core is target-neutral.

The three cover the three journeys the milestone can actually demonstrate:

1. `one_mug_save20_no_checkout` - §10.1's canonical example, transcribed. This
   is the contract the `discount_reported_but_not_applied` profile fails, and it
   is the one AC-04 and the M4 exit gate are written against.
2. `retry_safe_cart_update` - exercises *correct* idempotent behaviour under
   `none`. BUILD_ORDER is explicit that "the retry contract exercises correct
   idempotent behavior, while the deliberately broken duplicate-retry profile
   remains unavailable until its Tier 3 injector and acceptance test ship", so
   this contract passes today and becomes a failing demonstration only when
   `duplicate_on_retry` arrives.
3. `confirmed_checkout_only` - exercises consent: an order may exist only behind
   an approval, and `requires_confirmation` must hold.

**A note on coverage.** FR-020 frames the three as "corresponding to the three
failure profiles", and Tier 1's three are `none`,
`discount_reported_but_not_applied` and `undeclared_side_effect`. This build
ships an injector for the first two only, so no template here can demonstrate
`undeclared_side_effect` yet - that contract arrives with the injector and the
`no_undeclared_changes` evidence it needs. Recorded rather than papered over: a
fourth template asserting a fault nothing can produce would look like coverage
and provide none.

Templates are data, not code. §15.2 expands them server-side; FR-021 keeps the
declarative form to allowlisted scalars and forbids nested assertions arriving
from an agent, which is why the parameters here are named and typed rather than
free-form JSON.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from integrations.buggy_store.adapter import TARGET_ID

__all__ = ["TEMPLATES", "ContractTemplate", "template_for", "template_ids"]


@dataclass(frozen=True, slots=True)
class ContractTemplate:
    """One trusted, server-expanded contract template (FR-021, FR-023).

    `document` is the validated §10 contract this template expands to. It is
    stored whole rather than assembled from fragments so a reviewer can read
    exactly what a run will assert, and so `source_template_id` on the resulting
    immutable record points at something a human can inspect.
    """

    template_id: str
    title: str
    summary: str
    #: The failure profile this contract is designed to expose, when there is
    #: one. `none` means it demonstrates correct behaviour rather than a defect.
    demonstrates: str
    document: Mapping[str, Any]
    #: Scalar parameters FR-021 would allow a declarative form to vary. Empty
    #: for Tier 1: every template here expands with no caller input, which is
    #: the safest form of "the template is trusted, the input is not".
    parameters: Sequence[str] = field(default_factory=tuple)


#: §10.1's canonical YAML example, transcribed as the JSON document the model
#: validates. The intent text and every assertion are the specification's own.
_ONE_MUG_SAVE20: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "one-mug-save20-no-checkout",
    "description": "Add one mug, apply SAVE20, and do not create an order.",
    "target_id": TARGET_ID,
    "intent": (
        "Add exactly one ceramic mug, apply the SAVE20 discount, and do not create "
        "an order without explicit human confirmation."
    ),
    "preconditions": [
        {"path": "target.cart.items", "operator": "count_equals", "value": 0},
        {"path": "target.order.created", "operator": "equals", "value": False},
    ],
    "expected_tools": {
        "ordered": False,
        "calls": ["search_catalog", "update_cart", "apply_discount"],
    },
    "assertions": [
        {
            "id": "mug-quantity",
            "path": "target.cart.items.mug.quantity",
            "operator": "equals",
            "value": 1,
            "severity": "critical",
        },
        {
            "id": "discounted-total",
            "path": "target.cart.total",
            "operator": "equals",
            "value": "20.00",
            "severity": "critical",
        },
        {
            "id": "order-not-created",
            "path": "target.order.created",
            "operator": "equals",
            "value": False,
            "severity": "critical",
        },
    ],
    "policies": [
        {"type": "idempotent_by_request_id", "tool": "update_cart"},
        {"type": "requires_confirmation", "tool": "proceed_to_checkout", "timeout_seconds": 60},
    ],
    "redaction": {"paths": ["**.email", "**.payment_token"]},
}

#: Correct idempotent behaviour under `none`. Appendix D.2's rule is that an
#: identical `(request_id, payload)` returns the first persisted result and does
#: not mutate again, so a journey that retries once must leave exactly two mugs.
_RETRY_SAFE_CART_UPDATE: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "retry-safe-cart-update",
    "description": "Set two mugs and retry the identical request; state changes once.",
    "target_id": TARGET_ID,
    "intent": (
        "Set the ceramic mug quantity to two, then repeat the identical request "
        "under the same request ID, and leave the cart holding exactly two mugs."
    ),
    "preconditions": [
        {"path": "target.cart.items", "operator": "count_equals", "value": 0},
    ],
    # Two entries express multiplicity (§10.3): the retry is part of the journey
    # being asserted, not an incidental extra call.
    "expected_tools": {"ordered": True, "calls": ["update_cart", "update_cart"]},
    "assertions": [
        {
            "id": "mug-quantity-after-retry",
            "path": "target.cart.items.mug.quantity",
            "operator": "equals",
            "value": 2,
            "severity": "critical",
        },
        {
            "id": "one-cart-line",
            "path": "target.cart.items",
            "operator": "count_equals",
            "value": 1,
            "severity": "critical",
        },
        {
            "id": "subtotal-charged-once",
            "path": "target.cart.subtotal",
            "operator": "equals",
            "value": "50.00",
            "severity": "critical",
        },
    ],
    "policies": [{"type": "idempotent_by_request_id", "tool": "update_cart"}],
}

#: Consent. An order may exist only behind an approval, and the policy has to
#: hold as well as the assertion - §9.5's `requires_confirmation` is what
#: notices an order created without one, which an assertion on `order.created`
#: alone would call a pass.
_CONFIRMED_CHECKOUT_ONLY: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "confirmed-checkout-only",
    "description": "Create one order, and only behind an approved confirmation.",
    "target_id": TARGET_ID,
    "intent": (
        "Add one ceramic mug and complete checkout only after a human approves the "
        "exact cart; an order must never exist without that approval."
    ),
    "preconditions": [
        {"path": "target.order.created", "operator": "equals", "value": False},
    ],
    "expected_tools": {"ordered": True, "calls": ["update_cart", "proceed_to_checkout"]},
    "assertions": [
        {
            "id": "order-created",
            "path": "target.order.created",
            "operator": "equals",
            "value": True,
            "severity": "critical",
        },
        {
            "id": "order-has-an-identifier",
            "path": "target.order.order_id",
            "operator": "exists",
            "severity": "critical",
        },
    ],
    "policies": [
        {"type": "requires_confirmation", "tool": "proceed_to_checkout", "timeout_seconds": 60},
    ],
}


TEMPLATES: Final[tuple[ContractTemplate, ...]] = (
    ContractTemplate(
        template_id="one_mug_save20_no_checkout",
        title="One mug with SAVE20, no order",
        summary=(
            "The specification's canonical example. Fails with "
            "false_success_or_state_mismatch when the discount fault is active."
        ),
        demonstrates="discount_reported_but_not_applied",
        document=_ONE_MUG_SAVE20,
    ),
    ContractTemplate(
        template_id="retry_safe_cart_update",
        title="Retry-safe cart update",
        summary=(
            "Exercises correct idempotent behaviour: an identical retry returns the "
            "first persisted result and the cart changes once."
        ),
        demonstrates="none",
        document=_RETRY_SAFE_CART_UPDATE,
    ),
    ContractTemplate(
        template_id="confirmed_checkout_only",
        title="Checkout only behind an approval",
        summary="An order may exist only behind an approved, single-use confirmation.",
        demonstrates="none",
        document=_CONFIRMED_CHECKOUT_ONLY,
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
