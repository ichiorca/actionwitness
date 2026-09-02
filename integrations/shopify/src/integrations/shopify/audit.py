"""The external-audit target: observe, never drive (§12.17, §25.8; 015-T2).

`ExternalTargetAdapter` has no `execute`, and §9.1 says why: an
`external_webmcp` target runs its own tools, and the adapter must not
impersonate them "through a second implementation". Nothing here dispatches
anything. The audited site's own agent tools do the work; this reads what
happened afterwards.

**The only observation channel the specification names is Shopify's.** §9.3
defines exactly one Tier 3 provider — `shopify_cart_state`, "a locale-aware
`GET /cart.js` response fetched inside the paired Shopify storefront session...
independent of WebMCP tool-return text". For any other origin shape there is no
independent channel, and this module refuses to invent one: an audit of a site
whose cart cannot be read produces `observation_unavailable`, which is a
finding, not a gap.

That refusal is the entire point of the module. §12.17 exists because a tool's
self-report is the channel under test, so promoting one to an observation would
make the audit agree with whatever the site claimed — and the sites this feature
is aimed at are the ones that claim success and change nothing. There is
deliberately no code path from a tool result to an `Observation` here, and
`normalize` refuses a payload that looks like one.

**Nothing in this module fetches anything.** FR-160a puts the `cart.js` read in
the operator's own browser session; the payload arrives already fetched. A
maintainer looking for the HTTP client will not find one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from actionwitness_core.kernel import JsonValue
from actionwitness_core.ports.enums import ExecutionMode
from actionwitness_core.ports.models import Observation, TargetDescriptor

__all__ = [
    "MAX_CART_PAYLOAD_BYTES",
    "PROVENANCE",
    "PROVIDER_ID",
    "SCHEMA_VERSION",
    "TARGET_ID",
    "AuditObservationError",
    "ExternalAuditAdapter",
    "normalize_cart",
]

TARGET_ID: Final = "external-audit"
ADAPTER_ID: Final = "integrations.shopify.audit"

#: §9.3's Tier 3 provider, named there and not invented here.
PROVIDER_ID: Final = "shopify_cart_state"

#: §9.3: "Its provenance is `platform_session_api`; it is independent of WebMCP
#: tool-return text but is not described as direct database evidence." Both
#: halves of that sentence matter, and the second is why this is not called
#: anything stronger.
PROVENANCE: Final = "platform_session_api"

SCHEMA_VERSION: Final = "1.0"

#: FR-117's bound on a submitted cart payload, in bytes.
#:
#: Enforced by `POST /api/v1/audits/current/evidence`, which reads the raw request
#: body and checks its length *before* handing it to the JSON parser. That is the
#: only place it can be applied: `normalize_cart` below receives a payload that
#: has already been parsed, by which point the decode this bound exists to
#: prevent has happened.
#:
#: The history is worth keeping, because the comment was wrong twice in opposite
#: directions. It first claimed enforcement that did not exist; it was then
#: corrected to say the route did not exist, which was true until 015-T8 built
#: it. A bound is only a protection when a call site reads it, so if that
#: endpoint ever stops consulting this constant, this comment is wrong again.
MAX_CART_PAYLOAD_BYTES: Final = 256 * 1024

#: §9.3's conventional namespace, so a contract author writes `target.cart.total`
#: against an audited storefront exactly as they would against the demo target.
NAMESPACE: Final = "target"

#: Keys a Shopify `cart.js` response carries. Their presence is what makes a
#: payload "Shopify-shaped"; their absence is what makes an origin unobservable
#: rather than incorrectly observed.
_REQUIRED_CART_KEYS: Final = frozenset({"items", "item_count"})

#: Fields that would mean the caller sent a *tool result* rather than a session
#: read. Refused by name, because the failure this module exists to catch is
#: exactly a self-report wearing an observation's clothes.
_SELF_REPORT_MARKERS: Final = frozenset(
    {"status", "content", "isError", "is_error", "result", "reported_status"}
)

DESCRIPTOR: Final = TargetDescriptor(
    target_type="external_surface",
    target_id=TARGET_ID,
    execution_mode=ExecutionMode.EXTERNAL_WEBMCP,
    # §9.1: an external target has no pre/post fixture to switch between, and
    # FR-162 forbids injected fault profiles against one entirely. Advertising a
    # mode here would offer a control that must never work.
    supported_scenario_modes=("external_current",),
)


class AuditObservationError(ValueError):
    """A submitted payload could not become an authoritative observation.

    Distinct from "the cart was empty": this means the channel did not produce
    something the harness is willing to call evidence, which §12.17 turns into
    `observation_unavailable` rather than into a passing run.
    """


def _amount(cents: object) -> Decimal:
    """Shopify reports money as integer minor units; the harness stores exact decimals.

    Converted through `Decimal` rather than float arithmetic — `2599 / 100` is
    not `25.99` in binary floating point, and this value is compared for
    equality by contract assertions.
    """
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise AuditObservationError(f"expected integer minor units, got {type(cents).__name__}")
    return (Decimal(cents) / Decimal(100)).quantize(Decimal("0.01"))


def normalize_cart(payload: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Map a `cart.js` response into §9.3's `target.cart` shape.

    §9.3: the normalizer "maps variant IDs, quantities, subtotal, total,
    currency, and cart item count into that namespace while retaining bounded
    original field names in observation metadata".

    Line keys are the variant id as a string. Shopify has no stable per-line key
    of its own, and using the array index would make an unrelated reordering
    look like every line changing — which under `no_undeclared_changes` is a
    critical failure caused by presentation.
    """
    missing = sorted(_REQUIRED_CART_KEYS - set(payload))
    if missing:
        raise AuditObservationError(f"not a cart.js payload; missing {missing}")

    present = _SELF_REPORT_MARKERS & set(payload)
    if present:
        raise AuditObservationError(
            f"payload carries tool-result fields {sorted(present)}; a self-report is "
            "never promoted to an observation (§12.17)"
        )

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise AuditObservationError("cart.js `items` must be a list")

    items: dict[str, JsonValue] = {}
    for entry in raw_items:
        if not isinstance(entry, Mapping):
            raise AuditObservationError("each cart line must be an object")
        variant = entry.get("variant_id", entry.get("id"))
        if variant is None:
            raise AuditObservationError("a cart line carried no variant identifier")
        items[str(variant)] = {
            "variant_id": str(variant),
            "quantity": _count(entry.get("quantity")),
            "unit_price": str(_amount(entry.get("price"))),
            "line_price": str(_amount(entry.get("line_price", entry.get("price")))),
        }

    total = _amount(payload.get("total_price", 0))
    return {
        "cart": {
            "items": items,
            "item_count": _count(payload.get("item_count")),
            "subtotal": str(
                _amount(payload.get("items_subtotal_price", payload.get("total_price", 0)))
            ),
            "total": str(total),
            "currency": _currency(payload.get("currency")),
        },
        # FR-162 forbids order creation against an external target, so this is
        # always false and is recorded rather than omitted: a contract asserting
        # "no order was created" needs a path to assert on, and an absent path
        # would make that assertion unevaluable instead of true.
        "order": {"created": False, "order_id": None},
    }


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AuditObservationError(f"expected a non-negative integer count, got {value!r}")
    return value


def _currency(value: object) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
        raise AuditObservationError("cart.js must report a three-letter currency code")
    return value.upper()


class ExternalAuditAdapter:
    """§12.17's target: observed through the operator's browser, never driven.

    Implements `ExternalTargetAdapter` — `normalize` and `validate_origin`, and
    no `execute`. The absence is the interface.
    """

    def __init__(
        self,
        authorized_origin: str,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._authorized_origin = authorized_origin
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def descriptor(self) -> TargetDescriptor:
        return DESCRIPTOR

    def validate_origin(self, origin: str) -> None:
        """Exact equality, and nothing looser (FR-110, §12.17).

        §12.17 forbids following "a redirect, a link, or a navigation beyond"
        the authorized origin, so there is no prefix, suffix, or subdomain rule
        here to be talked into `https://shop.example.evil.test`.
        """
        if origin != self._authorized_origin:
            raise AuditObservationError("observation origin does not match the authorized origin")

    def normalize(self, payload: dict, provenance: str) -> Observation:
        """Validate a submitted `cart.js` read into an authoritative observation.

        `provenance` is checked rather than recorded: a caller that could label
        its own payload could label a tool result `platform_session_api` and the
        whole independence claim would rest on a string the browser chose.
        """
        if provenance != PROVENANCE:
            raise AuditObservationError(
                f"an external-audit observation must be {PROVENANCE!r}, not {provenance!r}"
            )
        return Observation(
            namespace=NAMESPACE,
            provider_id=PROVIDER_ID,
            provenance=PROVENANCE,
            schema_version=SCHEMA_VERSION,
            payload=normalize_cart(payload),
            # Shopify's cart has no monotonic version the harness can trust, and
            # inventing one would let a comparison claim state moved when
            # nothing said so. `None` is the honest answer; FR-032's
            # change detection falls back to the content hash.
            state_version=None,
            captured_at=self._clock(),
        )
