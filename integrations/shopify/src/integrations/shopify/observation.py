"""`shopify_cart_state` — the independent cart observation (§9.3, FR-112..FR-114).

§9.3 names exactly one Tier 3 provider: "a locale-aware `GET /cart.js` response
fetched inside the paired Shopify storefront session, schema-validated and
normalized by Python. Its provenance is `platform_session_api`; it is
independent of WebMCP tool-return text but is not described as direct database
evidence."

**Nothing in this module fetches anything.** FR-112 puts the `cart.js` read in
the *paired shopper session*, inside the operator's own browser, built from
`window.Shopify.routes.root + 'cart.js'`. The payload arrives already fetched. A
maintainer looking for the HTTP client will not find one, and that absence is
what keeps the harness from being able to reach an origin somebody handed it.

**Nothing here is derived from a tool result.** The channel under test is
Shopify's own WebMCP tools reporting on themselves; promoting one of their
responses to an observation would make the verdict agree with whatever the tool
claimed. `normalize_cart` refuses a payload wearing a tool result's clothes, and
this module adds no second route in.

## What the bridge submits

The payload is a `cart.js` body, plus one bounded block of facts only the bridge
can know:

```json
{
  "items": [{"variant_id": 42, "quantity": 1, "price": 2500, "line_price": 2500}],
  "item_count": 1,
  "items_subtotal_price": 2500,
  "total_price": 2500,
  "total_discount": 0,
  "currency": "USD",
  "page": {"checkout_navigation_observed": false}
}
```

`page.checkout_navigation_observed` is **required**. FR-114 makes "cross-origin
checkout navigation... a failed or incomplete trial, never a pass", so an
observation that cannot speak to it is not evidence for this contract — and
defaulting it to `false` would make "the bridge did not look" indistinguishable
from "nothing navigated". Refusing is the §5 direction: an observation channel
that cannot answer produces a non-pass, never a quiet success.

`page.store_origin` is **not** read from the payload. FR-110 and the project
rules keep the store origin server-controlled, so the configured origin is what
is recorded; a payload that names a *different* origin is refused rather than
believed, because a bridge submitting another store's cart is the cross-origin
case, not a labelling detail.

## How this differs from `audit.py`, and why

`normalize_cart` is called, never copied: every size, shape, self-report, money
and currency refusal has one implementation. Three differences are deliberate.

* **The configured variant gets the stable line key `test_variant`** (§13.5).
  Every other line keeps its variant id, so a cart holding something unexpected
  is *visible* rather than refused — the contract fails it, which is what makes
  the failure a finding with evidence attached instead of a parse error.
* **`line_total` rather than `line_price`**, which is the name §13.5's shape
  uses for this observation.
* **There is no `order` key.** The audit path records `order.created: False`
  because order creation is forbidden there by construction. Here the harness
  simply cannot see it: observing Shopify order state needs an Admin API
  credential FR-118 forbids the cart-assurance path from having. Writing
  `created: false` would be manufacturing an observation, so this payload says
  nothing about orders and the checkout question is carried by the one fact the
  bridge can actually witness.

## `totals_consistent`

§13.5 requires the contract to assert "internal arithmetic consistency", and
§9.4's operators compare a path to a literal — they cannot compare `subtotal` to
the sum of the lines. So the arithmetic is done here, in the integration that
owns commerce vocabulary, and published as one boolean the contract asserts on.
A deviation from §13.5's illustrated payload shape (one added key), taken
because the alternative is a stated requirement with no expressible term.

It is a *finding*, never a refusal: totals that disagree are exactly what an
assurance harness exists to report, and raising here would delete the evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from actionwitness_core.kernel import JsonValue
from actionwitness_core.ports.models import Observation
from integrations.shopify.audit import (
    MAX_CART_PAYLOAD_BYTES,
    NAMESPACE,
    PROVENANCE,
    PROVIDER_ID,
    AuditObservationError,
    cart_amount,
    normalize_cart,
    require_exact_origin,
)

__all__ = [
    "MAX_CART_PAYLOAD_BYTES",
    "NAMESPACE",
    "PROVENANCE",
    "PROVIDER_ID",
    "SCHEMA_VERSION",
    "TEST_VARIANT_KEY",
    "ShopifyCartObservationProvider",
    "ShopifyCartUnobservable",
    "project_cart",
    "require_within_payload_bound",
]

#: §13.5: "a stable line key `test_variant`". The variant id is *also* recorded
#: on the line, so the contract can assert which variant it was; the key exists
#: so the contract does not have to be rewritten when the configuration changes.
TEST_VARIANT_KEY: Final = "test_variant"

#: This payload's shape version, distinct from `audit.py`'s because the shapes
#: are different — a stored snapshot records which one it was captured under
#: (constitution §4).
SCHEMA_VERSION: Final = "1.0"

#: Bridge-observed page facts. Bounded by name: anything else the bridge sends
#: under `page` is ignored for assertions (FR-113: "unknown fields are ignored").
_PAGE_KEY: Final = "page"
_CHECKOUT_KEY: Final = "checkout_navigation_observed"
_ORIGIN_KEY: Final = "store_origin"


class ShopifyCartUnobservable(RuntimeError):
    """`capture` was called on a target the harness cannot read for itself.

    Not a `ValueError` about the payload — there is no payload. It means a
    caller asked this provider to go and look, and looking is precisely what
    FR-112 puts inside the shopper's browser session. Refusing here keeps the
    absence of an HTTP client from being quietly worked around.
    """


def require_within_payload_bound(raw: bytes) -> None:
    """Refuse a submitted cart body over FR-117's 256 KiB bound, before parsing.

    The bound only protects anything if it is applied to the *raw* bytes: by the
    time `project_cart` below sees a payload it is already a parsed object, and
    the decode this limit exists to prevent has happened. So the guard lives
    here, next to the normalizer that must not be reached without it, and the
    submitting route calls it on the request body it read.

    One number for both Shopify paths: the constant lives in `audit.py`, whose
    own route applies it to the audit submission and this to the bridge's. If a
    call site ever stops consulting it, the bound has stopped being a bound.
    """
    if len(raw) > MAX_CART_PAYLOAD_BYTES:
        raise AuditObservationError(
            f"a submitted cart may be at most {MAX_CART_PAYLOAD_BYTES} bytes, got {len(raw)}"
        )


def project_cart(
    payload: Mapping[str, Any],
    *,
    test_variant_id: str,
    store_origin: str,
) -> dict[str, JsonValue]:
    """Map a submitted `cart.js` body into §13.5's `target` shape.

    Raises `AuditObservationError` for anything that is not an observation:
    a payload that is not a cart, one carrying tool-result fields, money that is
    not integer minor units, a missing checkout fact, or a page block naming
    another store.
    """
    normalized: Any = normalize_cart(payload)
    cart: dict[str, Any] = dict(normalized["cart"])

    lines: dict[str, JsonValue] = {}
    for variant_key, raw_line in dict(cart["items"]).items():
        line: dict[str, Any] = dict(raw_line)
        # §13.5 names this field `line_total`. Renamed rather than duplicated so
        # a contract cannot assert the same money twice under two spellings.
        line["line_total"] = line.pop("line_price")
        key = TEST_VARIANT_KEY if variant_key == test_variant_id else variant_key
        lines[key] = line

    discount_total = cart_amount(payload.get("total_discount", 0))
    subtotal = Decimal(str(cart["subtotal"]))
    total = Decimal(str(cart["total"]))

    return {
        "cart": {
            "items": lines,
            "item_count": cart["item_count"],
            "subtotal": str(subtotal),
            "discount_total": str(discount_total),
            "total": str(total),
            "currency": cart["currency"],
            "totals_consistent": _totals_agree(lines, subtotal, discount_total, total),
        },
        "page": {
            # Server-controlled (FR-110). Recorded from configuration, never
            # from the submission, so a report cannot name a store the operator
            # did not authorize.
            _ORIGIN_KEY: _confirmed_origin(payload, store_origin),
            _CHECKOUT_KEY: _checkout_navigation(payload),
        },
    }


def _totals_agree(
    lines: Mapping[str, JsonValue],
    subtotal: Decimal,
    discount_total: Decimal,
    total: Decimal,
) -> bool:
    """Whether the cart's own numbers add up, in exact decimals (FR-113).

    Three identities, all of them Shopify's own: each line's total is its unit
    price times its quantity, the lines sum to the subtotal, and the subtotal
    less the stated discount is the total. Nothing is inferred — a cart that
    fails any of them is reported as inconsistent rather than corrected.
    """
    running = Decimal("0.00")
    for line in lines.values():
        entry: dict[str, Any] = dict(line)
        line_total = Decimal(str(entry["line_total"]))
        if line_total != Decimal(str(entry["unit_price"])) * Decimal(int(entry["quantity"])):
            return False
        running += line_total
    return running == subtotal and subtotal - discount_total == total


def _checkout_navigation(payload: Mapping[str, Any]) -> bool:
    """The one page fact the bridge must state (FR-114).

    Strictly a boolean, and strictly present. A missing or non-boolean value is
    a refusal rather than a default: `false` would be the harness asserting,
    on nobody's authority, that no checkout navigation happened.
    """
    page = payload.get(_PAGE_KEY)
    if not isinstance(page, Mapping):
        raise AuditObservationError(
            f"a Shopify cart observation must carry a {_PAGE_KEY!r} block stating "
            f"{_CHECKOUT_KEY!r}; FR-114 makes checkout navigation a failed trial, and "
            "an observation that cannot say is not evidence"
        )
    observed = page.get(_CHECKOUT_KEY)
    if not isinstance(observed, bool):
        raise AuditObservationError(
            f"{_PAGE_KEY}.{_CHECKOUT_KEY} must be true or false, got {observed!r}"
        )
    return observed


def _confirmed_origin(payload: Mapping[str, Any], store_origin: str) -> str:
    """The configured origin, after refusing a submission that names another.

    The value returned is always the server's. The check exists for the case the
    bridge *does* label its capture: a label naming a different store means the
    payload came from somewhere the operator did not authorize, and believing
    the cart while ignoring the label would be the loosest possible reading of
    FR-110.
    """
    page = payload.get(_PAGE_KEY)
    claimed = page.get(_ORIGIN_KEY) if isinstance(page, Mapping) else None
    if claimed is not None:
        if not isinstance(claimed, str):
            raise AuditObservationError(f"{_PAGE_KEY}.{_ORIGIN_KEY} must be a string")
        require_exact_origin(claimed, store_origin)
    return store_origin


class ShopifyCartObservationProvider:
    """§9.3's `shopify_cart_state`: fed by the bridge, never fetched.

    Holds the locked configuration — the exact store origin and the configured
    test variant — because both are server-controlled (FR-110, §13.5) and a
    provider that took them per call would take them from whoever was calling.
    """

    provider_id = PROVIDER_ID
    namespace = NAMESPACE
    provenance = PROVENANCE

    def __init__(
        self,
        *,
        store_origin: str,
        test_variant_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store_origin = store_origin
        self._test_variant_id = test_variant_id
        #: Injected so a captured snapshot's instant is reproducible in replay
        #: (constitution §1).
        self._clock = clock or (lambda: datetime.now(UTC))

    async def capture(self, workspace_id: str) -> Observation:
        """Refuse: this provider cannot go and look (FR-112).

        `ObservationProvider.capture` means "read the target's state now", and
        the read this provider needs happens inside a shopper session in the
        operator's browser, authenticated as that shopper. The harness holds no
        such session and, by FR-118, no credential that would create one.

        Refusing rather than returning an empty observation: constitution §5
        makes observation failure "an explicit non-pass result; it never
        degrades to success", and an empty cart payload would make every
        `absent` assertion pass against a store nobody looked at.
        """
        raise ShopifyCartUnobservable(
            "shopify_cart_state is submitted by the paired theme bridge from the "
            "shopper's own session; the harness never fetches it (FR-112, FR-118)"
        )

    def normalize(self, payload: Mapping[str, Any], provenance: str) -> Observation:
        """Validate one submitted `cart.js` read into an authoritative observation.

        `provenance` is checked rather than recorded, for the reason `audit.py`
        gives: a caller that could label its own payload could label a tool
        result `platform_session_api`, and the independence claim would rest on a
        string the browser chose.
        """
        if provenance != PROVENANCE:
            raise AuditObservationError(
                f"a Shopify cart observation must be {PROVENANCE!r}, not {provenance!r}"
            )
        return Observation(
            namespace=self.namespace,
            provider_id=self.provider_id,
            provenance=self.provenance,
            schema_version=SCHEMA_VERSION,
            payload=project_cart(
                payload,
                test_variant_id=self._test_variant_id,
                store_origin=self._store_origin,
            ),
            # Shopify's cart carries no monotonic version the harness can trust,
            # and inventing one would let a comparison claim state moved when
            # nothing said so. `None` is the honest answer; FR-032's change
            # detection falls back to the content hash.
            state_version=None,
            captured_at=self._clock(),
        )
