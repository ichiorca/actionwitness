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

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from integrations.buggy_store.adapter import TARGET_ID

__all__ = [
    "ALLOWED_DISCOUNT_CODES",
    "FORM_PARAMETERS",
    "MAX_CONTRACT_NAME_CHARS",
    "MAX_QUANTITY",
    "MIN_QUANTITY",
    "TEMPLATES",
    "ContractTemplate",
    "TemplateExpansionError",
    "expand",
    "template_for",
    "template_ids",
]

#: Two decimal places, the quantum every stored amount already uses (§13.2).
_CENTS: Final[Decimal] = Decimal("0.01")


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
    #: The scalar parameters FR-021 allows the declarative form to vary for
    #: this template, and *only* those: a scalar absent from here is rejected
    #: rather than ignored, so a caller cannot believe they constrained
    #: something the contract never mentions. Empty means the template
    #: expands with no caller input beyond an optional display name, which is
    #: `confirmed_checkout_only`'s case — quantity and discount say nothing
    #: about whether an order needed an approval.
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


#: The blast-radius contract (§9.10, FR-159; 013-T5).
#:
#: Every assertion here is about the cart, and under `undeclared_side_effect`
#: every one of them passes: the mutation is correct. What fails is
#: `no_undeclared_changes`, because the same journey also rewrote
#: `preferences.delivery_note` — a path this contract never mentions and no
#: executed tool declares an effect on (§13.4 gives `update_cart` only
#: `target.cart.*`).
#:
#: That is the argument the whole feature exists to make. A reviewer reading the
#: assertion list would call this run clean. The contract is safe by default
#: instead: it constrains what the journey may touch rather than enumerating
#: every value that must hold, and it catches the path nobody thought to name.
#:
#: `allow_paths` is deliberately empty. A waiver here would be the obvious way to
#: make the demonstration pass, and it would delete the demonstration.
_NO_SIDE_EFFECTS: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "one-mug-no-side-effects",
    "description": "Add one mug and change nothing the contract does not name.",
    "target_id": TARGET_ID,
    "intent": (
        "Add exactly one ceramic mug to the cart, and leave every other part of "
        "the shopper's saved state alone."
    ),
    # Only the cart. `target.preferences` is deliberately absent from every term
    # in this contract — precondition and assertion alike — because §9.10(a)
    # makes a precondition path *declared*. Naming the preference here to
    # document the intent would exempt the exact path the demonstration depends
    # on, and the run would pass.
    "preconditions": [
        {"path": "target.cart.items", "operator": "count_equals", "value": 0},
    ],
    "expected_tools": {"ordered": False, "calls": ["update_cart"]},
    "assertions": [
        {
            "id": "mug-quantity",
            "path": "target.cart.items.mug.quantity",
            "operator": "equals",
            "value": 1,
            "severity": "critical",
        },
        {
            "id": "cart-has-one-line",
            "path": "target.cart.items",
            "operator": "count_equals",
            "value": 1,
            "severity": "critical",
        },
        {
            "id": "cart-total-is-list-price",
            "path": "target.cart.total",
            "operator": "equals",
            "value": "25.00",
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
        # `allow_paths` is spelled out even though it is empty, and both facts
        # matter. Empty is the point of the contract — a waiver here would be the
        # obvious way to make the demonstration pass, and it would delete the
        # demonstration. Spelled out because a template is seeded by hashing the
        # document *as written*, while §24.2 re-verifies it by hashing the parsed
        # contract's canonical form: a field left to its default hashes
        # differently in the two places, and case generation refuses the run with
        # "the source contract does not match its stored hash".
        {"type": "no_undeclared_changes", "allow_paths": []},
    ],
    "redaction": {"paths": ["**.email", "**.payment_token"]},
}

#: The surface contract (§9.5, §9.11, FR-169; 014-T6).
#:
#: Every assertion is about the cart, and under `tool_surface_poisoned` every one
#: of them passes: the journey does exactly what was asked. What fails is
#: `stable_tool_surface`, because a look-alike tool was registered mid-run under
#: a name the agent had already chosen from a different definition.
#:
#: That is the argument. A contract can be green everywhere it looks while the
#: tools it looked *through* were swapped underneath it, and no assertion an
#: author could write would notice — the state is correct.
#:
#: `failing_delta_kinds` is spelled out rather than left to its default, and in
#: sorted order. A template is seeded by hashing the document as written while
#: §24.2 re-verifies it by hashing the parsed contract's canonical form, so a
#: field left to its default hashes differently in the two places and eval-case
#: generation refuses the run. `description_change` is deliberately absent:
#: §9.5 makes it a warning "because benign copy edits should not fail a run",
#: and the look-alike changes its schema too.
_ONE_MUG_STABLE_SURFACE: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "one-mug-stable-surface",
    "description": "Add one mug while the target tool surface stays what it was.",
    "target_id": TARGET_ID,
    "intent": (
        "Add exactly one ceramic mug, using the same tools that were published "
        "when the run was armed."
    ),
    "preconditions": [
        {"path": "target.cart.items", "operator": "count_equals", "value": 0},
    ],
    "expected_tools": {"ordered": False, "calls": ["update_cart"]},
    "assertions": [
        {
            "id": "mug-quantity",
            "path": "target.cart.items.mug.quantity",
            "operator": "equals",
            "value": 1,
            "severity": "critical",
        },
        {
            "id": "cart-total-is-list-price",
            "path": "target.cart.total",
            "operator": "equals",
            "value": "25.00",
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
        {
            "type": "stable_tool_surface",
            "failing_delta_kinds": ["added", "hint_change", "removed", "schema_change"],
        },
    ],
    "redaction": {"paths": ["**.email", "**.payment_token"]},
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
        parameters=("quantity", "discount_code"),
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
        parameters=("quantity",),
    ),
    ContractTemplate(
        template_id="confirmed_checkout_only",
        title="Checkout only behind an approval",
        summary="An order may exist only behind an approved, single-use confirmation.",
        demonstrates="none",
        document=_CONFIRMED_CHECKOUT_ONLY,
    ),
    ContractTemplate(
        template_id="one_mug_no_side_effects",
        title="One mug, and nothing else touched",
        summary=(
            "Every cart assertion passes and the run still fails: the same journey "
            "rewrote a saved preference no contract term names. Demonstrates that a "
            "contract is safe by default rather than only as complete as its "
            "author's imagination."
        ),
        demonstrates="undeclared_side_effect",
        document=_NO_SIDE_EFFECTS,
        parameters=("quantity",),
    ),
    ContractTemplate(
        template_id="one_mug_stable_surface",
        title="One mug, with the tools it was shown",
        summary=(
            "Every cart assertion passes and the run still fails: a look-alike tool "
            "was registered mid-run under a name the agent had already chosen. "
            "Demonstrates that a contract can be green everywhere it looks while the "
            "tools it looked through were swapped underneath it."
        ),
        demonstrates="tool_surface_poisoned",
        document=_ONE_MUG_STABLE_SURFACE,
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
#
# §25.2 fixes the declarative form's controls to exactly `template_id`, optional
# `contract_name`, `quantity`, and `discount_code`. FR-021 adds the rule that
# does the real work: a template accepts "only the scalar parameters allowlisted
# by that template", and one it does not allowlist is *rejected* rather than
# ignored. Ignoring is the dangerous reading — a caller who sent `discount_code`
# to a contract with no discount term would be told their contract was created
# and would reasonably believe it asserted a discount.
#
# The expansion lives here, in the package that already knows a mug costs 25.00,
# rather than in the service. A service that computed a discounted total would be
# a target-neutral layer doing commerce arithmetic (constitution §1).


#: The seeded catalogue's ceramic-mug price (§13.2), as a literal.
#:
#: Not imported from `buggy_store.catalog`: this package translates to the demo
#: application's *public HTTP API* and never imports its service objects (§26.7).
#: The three amounts already written into the documents above are this same
#: constant spelled out; naming it once keeps an expansion from drifting away
#: from the defaults it has to reproduce.
_MUG_UNIT_PRICE: Final[Decimal] = Decimal("25.00")

#: §13.1's allowlisted discounts: "`SAVE20`: 20% off eligible cart subtotal."
_DISCOUNT_PERCENT: Final[Mapping[str, int]] = {"SAVE20": 20}

#: The codes the declarative form may offer, in the order they are published.
ALLOWED_DISCOUNT_CODES: Final[tuple[str, ...]] = tuple(_DISCOUNT_PERCENT)

#: Appendix G's bounds for the declarative form's `quantity` control.
MIN_QUANTITY: Final = 1
MAX_QUANTITY: Final = 5

#: Appendix G's `contract_name` bound, matching the core's `MAX_NAME_LENGTH`.
MAX_CONTRACT_NAME_CHARS: Final = 80

#: Every scalar the declarative form may carry, in §25.2's order. A template
#: allowlists a subset; nothing outside this set reaches expansion at all.
FORM_PARAMETERS: Final[tuple[str, ...]] = ("quantity", "discount_code")


class TemplateExpansionError(ValueError):
    """A flat submission the template cannot accept.

    Carries `(field, message)` pairs so the boundary can return §15.8's
    field-level details rather than one opaque sentence: a form that says only
    "invalid" makes a person guess which control they got wrong.
    """

    def __init__(self, details: Sequence[tuple[str, str]]) -> None:
        self.details = tuple(details)
        super().__init__("; ".join(f"{field}: {message}" for field, message in self.details))


def _money(amount: Decimal) -> str:
    """Two-place decimal string, the form every stored amount already uses."""
    return str(amount.quantize(_CENTS, rounding=ROUND_HALF_UP))


def _discounted(subtotal: Decimal, code: str) -> Decimal:
    off = (subtotal * Decimal(_DISCOUNT_PERCENT[code]) / Decimal(100)).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )
    return subtotal - off


#: Numerals spelled out, because the seeded documents spell them out.
#:
#: This is not a style flourish. An unparameterised expansion has to reproduce
#: its template exactly — that identity is what makes "submit the form without
#: touching anything" mean "create the contract that was on offer" — and the
#: documents above say "one mug" and "two mugs". Bounded by `MAX_QUANTITY`, so
#: the mapping is total over every quantity a caller can send.
_SPELLED: Final[Mapping[int, str]] = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five"}


def _mugs(quantity: int) -> str:
    return f"{_SPELLED[quantity]} mug" if quantity == 1 else f"{_SPELLED[quantity]} mugs"


def _ceramic_mugs(quantity: int) -> str:
    """The longer form the `intent` fields use."""
    return f"{_SPELLED[quantity]} ceramic mug{'' if quantity == 1 else 's'}"


def _slug(quantity: int) -> str:
    return f"{_SPELLED[quantity]}-mug{'' if quantity == 1 else 's'}"


def _with_assertion_values(
    document: Mapping[str, Any], values: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    """Replace the `value` of the named assertions, leaving every other alone.

    Keyed by assertion `id` rather than by position, so a reordered template
    cannot silently rewrite the wrong term.
    """
    unknown = sorted(set(values) - {assertion["id"] for assertion in document["assertions"]})
    if unknown:  # pragma: no cover - only a bad template edit reaches this
        raise KeyError(f"template has no assertion(s) {unknown}")
    return [
        {**assertion, "value": values[assertion["id"]]}
        if assertion["id"] in values
        else dict(assertion)
        for assertion in document["assertions"]
    ]


def _expand_one_mug_save20(document: Mapping[str, Any], **scalars: Any) -> Mapping[str, Any]:
    quantity: int = scalars["quantity"]
    code: str = scalars["discount_code"]
    total = _discounted(_MUG_UNIT_PRICE * quantity, code)
    return {
        **document,
        "name": f"{_slug(quantity)}-{code.lower()}-no-checkout",
        "description": f"Add {_mugs(quantity)}, apply {code}, and do not create an order.",
        "intent": (
            f"Add exactly {_ceramic_mugs(quantity)}, apply the {code} discount, and do not "
            "create an order without explicit human confirmation."
        ),
        "assertions": _with_assertion_values(
            document, {"mug-quantity": quantity, "discounted-total": _money(total)}
        ),
    }


def _expand_retry_safe(document: Mapping[str, Any], **scalars: Any) -> Mapping[str, Any]:
    quantity: int = scalars["quantity"]
    return {
        **document,
        "description": (
            f"Set {_mugs(quantity)} and retry the identical request; state changes once."
        ),
        "intent": (
            f"Set the ceramic mug quantity to {_SPELLED[quantity]}, then repeat the identical "
            "request under the same request ID, and leave the cart holding exactly "
            f"{_mugs(quantity)}."
        ),
        "assertions": _with_assertion_values(
            document,
            {
                "mug-quantity-after-retry": quantity,
                "subtotal-charged-once": _money(_MUG_UNIT_PRICE * quantity),
            },
        ),
    }


def _expand_no_side_effects(document: Mapping[str, Any], **scalars: Any) -> Mapping[str, Any]:
    quantity: int = scalars["quantity"]
    return {
        **document,
        "name": f"{_slug(quantity)}-no-side-effects",
        "description": f"Add {_mugs(quantity)} and change nothing the contract does not name.",
        "intent": (
            f"Add exactly {_ceramic_mugs(quantity)} to the cart, and leave every other part "
            "of the shopper's saved state alone."
        ),
        "assertions": _with_assertion_values(
            document,
            {
                "mug-quantity": quantity,
                "cart-total-is-list-price": _money(_MUG_UNIT_PRICE * quantity),
            },
        ),
    }


@dataclass(frozen=True, slots=True)
class _Expansion:
    """How one template varies, and where its current quantity is written.

    `quantity_assertion` exists so the default is *derived* rather than
    restated. An omitted `quantity` has to reproduce the template exactly —
    `retry_safe_cart_update` asserts two mugs, the other two assert one — and a
    default written here as a number would be a second place to keep in step
    with the documents above. Reading it back from the assertion the expansion
    is about to rewrite makes "no parameters" mean "the template as seeded" by
    construction, which is the property the tests pin.
    """

    apply: Callable[..., Mapping[str, Any]]
    quantity_assertion: str


#: How each template varies, keyed by `template_id`. A template absent from here
#: allowlists no scalar and expands to its document verbatim — which is
#: `confirmed_checkout_only`'s case, and the reason this mapping is not total.
_EXPANSIONS: Final[Mapping[str, _Expansion]] = {
    "one_mug_save20_no_checkout": _Expansion(_expand_one_mug_save20, "mug-quantity"),
    "retry_safe_cart_update": _Expansion(_expand_retry_safe, "mug-quantity-after-retry"),
    "one_mug_no_side_effects": _Expansion(_expand_no_side_effects, "mug-quantity"),
}


def _default_quantity(template: ContractTemplate) -> int:
    """The quantity this template already asserts (see `_Expansion`)."""
    expansion = _EXPANSIONS[template.template_id]
    for assertion in template.document["assertions"]:
        if assertion["id"] == expansion.quantity_assertion:
            return int(assertion["value"])
    raise KeyError(  # pragma: no cover - only a bad template edit reaches this
        f"{template.template_id!r} has no assertion {expansion.quantity_assertion!r}"
    )


def _validated_scalars(
    template: ContractTemplate, submitted: Mapping[str, Any]
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Bound and type each allowlisted scalar, collecting every rejection.

    Every field is checked before anything is raised, so a person fixing a form
    sees all of what is wrong rather than one round trip per control.
    """
    allowed = set(template.parameters)
    details: list[tuple[str, str]] = []
    scalars: dict[str, Any] = {}

    for name in FORM_PARAMETERS:
        supplied = submitted.get(name)
        if name not in allowed:
            # FR-021's rule, and why this is a rejection rather than a shrug: a
            # caller told their contract was created would otherwise believe it
            # asserted something the template never mentions.
            if supplied is not None:
                details.append(
                    (name, f"template {template.template_id!r} does not accept {name!r}")
                )
            continue
        scalars[name] = supplied

    if "quantity" in allowed:
        quantity = scalars.get("quantity")
        if quantity is None:
            scalars["quantity"] = _default_quantity(template)
        elif isinstance(quantity, bool) or not isinstance(quantity, int):
            # `bool` first: it is an `int` subclass, so `True` would otherwise
            # pass the bound check and expand to a one-mug contract.
            details.append(("quantity", "quantity must be a whole number"))
        elif not MIN_QUANTITY <= quantity <= MAX_QUANTITY:
            details.append(
                ("quantity", f"quantity must be between {MIN_QUANTITY} and {MAX_QUANTITY}")
            )

    if "discount_code" in allowed:
        code = scalars.get("discount_code")
        if code is None:
            scalars["discount_code"] = next(iter(_DISCOUNT_PERCENT))
        elif code not in _DISCOUNT_PERCENT:
            # Allowlisted, never pattern-matched: an unknown code is a rejected
            # argument rather than a zero-percent no-op that would quietly
            # assert the undiscounted total.
            details.append(
                ("discount_code", f"discount_code must be one of {sorted(_DISCOUNT_PERCENT)}")
            )

    return scalars, details


def expand(template_id: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Expand one trusted template into a complete §10 contract document.

    The template is the trusted part and the parameters are not, which is the
    whole shape of FR-021: the caller picks from an allowlist and supplies
    bounded scalars, while every assertion, policy, path and target comes from
    the template. Nothing a caller sends becomes a contract term.

    Raises `TemplateExpansionError` naming every offending field. The result is
    still validated through the core's `parse_contract` before it is stored —
    expansion bounds the input, it does not replace the contract boundary.
    """
    template = template_for(template_id)
    if template is None:
        raise TemplateExpansionError([("template_id", f"unknown template {template_id!r}")])

    scalars, details = _validated_scalars(template, parameters)

    chosen_name = parameters.get("contract_name")
    if chosen_name is not None:
        if not isinstance(chosen_name, str) or not chosen_name.strip():
            details.append(("contract_name", "contract_name must be a non-empty string"))
        elif len(chosen_name) > MAX_CONTRACT_NAME_CHARS:
            details.append(
                (
                    "contract_name",
                    f"contract_name must be at most {MAX_CONTRACT_NAME_CHARS} characters",
                )
            )

    if details:
        raise TemplateExpansionError(details)

    expansion = _EXPANSIONS.get(template.template_id)
    document = (
        dict(template.document)
        if expansion is None
        else dict(expansion.apply(template.document, **scalars))
    )
    if chosen_name is not None:
        # Applied last, so a display name a person chose is never overwritten by
        # the expansion's generated one.
        document["name"] = chosen_name
    return document
