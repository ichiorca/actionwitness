"""The authorized Shopify development store: observed, never driven (§9.1, §12.12).

One store, one variant, one currency, cart only. Every one of those is a safety
boundary rather than a scope preference — an accidental order on somebody's
storefront is not recoverable by a code change — and each is held server-side
(FR-110, project rules), never accepted from a caller.

**There is no `execute`, and the absence is the interface.** §9.1: an
`external_webmcp` adapter "observes tools executed by the browser-owned target
and never impersonates them through a second implementation". Shopify's own
WebMCP tools run in the shopper's session and do the work; this adapter reads
what the cart looked like afterwards, through the bridge, and evaluates a
contract against it. An `execute` added later would be a second, fictional
Shopify.

**It publishes no tool surface, and that is deliberate rather than unfinished.**
FR-114 is explicit that "the standalone bridge cannot observe Shopify's internal
tool trajectory", and AC-18 requires model selection, observed trajectory, and
tool execution to stay `not_evaluated`. `tool_specs()` returning nothing makes
that structural: §10.2 refuses a contract naming a tool the selected adapter does
not publish, so *no* contract against this target can carry `expected_tools`, a
`forbidden_tool`, or an idempotency policy. A published surface would leave the
door open to a contract that claimed `proceed_to_checkout` was not called — a
claim nothing here could witness. The tools Shopify does publish are enumerated
in `pack.py` for the audit path, where the operator's browser reports them.

**No Shopify credential of any kind (FR-118).** No Admin API token, no customer
password, no payment credential, no order creation. This module holds an origin,
a variant id, and a currency code; there is nothing here to leak and nothing to
rotate. The development-store access needed to install the theme bridge is a
setup activity and is never stored by the harness.

**Nothing here reaches the network.** Like `audit.py`, this module imports no
HTTP client: the `cart.js` read happens in the paired shopper session (FR-112),
and an adapter holding both an operator-supplied origin and a client is one edit
away from being a crawler.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from actionwitness_core.ports.enums import ExecutionMode
from actionwitness_core.ports.models import Observation, TargetDescriptor, TargetToolSpec
from integrations.shopify.audit import require_exact_origin
from integrations.shopify.observation import ShopifyCartObservationProvider

__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "TARGET_ID",
    "TARGET_TYPE",
    "ShopifyAdapter",
]

#: The contract's `target_id`, and what a workspace selects. Distinct from the
#: audit target's `external-audit`: this one is a *configured, authorized*
#: development store, not an arbitrary origin an operator asserted rights to.
TARGET_ID: Final = "shopify-development-store"

#: §9.1's Tier 3 target type, named there verbatim.
TARGET_TYPE: Final = "shopify_development_store"

ADAPTER_ID: Final = "integrations.shopify.adapter"

#: §9.1's descriptor.
#:
#: `external_current` alone, because §9.1 says so directly: "The Buggy Store
#: adapter advertises `pre_fix` and `post_fix`; Shopify advertises only
#: `external_current`, so the UI disables the pre/post control for Shopify with
#: an explanation." There is no fixture to switch between on a store the project
#: does not own.
#:
#: `("none",)` rather than `()` for the fault list, and the difference is
#: load-bearing. `TargetDescriptor.injects` reads an empty tuple as "this
#: adapter makes no claim" and therefore permits *every* profile. This adapter
#: injects nothing and cannot: FR-162 forbids fault injection against an
#: external target outright, and Shopify would not cooperate if it did not.
#: Naming the one profile it supports turns silence into a statement, so a run
#: armed with `discount_reported_but_not_applied` is refused rather than
#: producing a report naming an active defect nothing produced.
DESCRIPTOR: Final = TargetDescriptor(
    target_type=TARGET_TYPE,
    target_id=TARGET_ID,
    execution_mode=ExecutionMode.EXTERNAL_WEBMCP,
    supported_scenario_modes=("external_current",),
    supported_fault_profiles=("none",),
)


class ShopifyAdapter:
    """`ExternalTargetAdapter` for one configured Shopify development store.

    `normalize` and `validate_origin`, plus the three `TargetAdapter` members —
    and no `execute`.
    """

    descriptor = DESCRIPTOR
    adapter_id = ADAPTER_ID

    def __init__(
        self,
        *,
        store_origin: str,
        test_variant_id: str,
        expected_currency: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """The locked configuration, and nothing else.

        Keyword-only on purpose: three strings in a row is exactly the signature
        where an origin and a variant id get swapped, and the swap would produce
        an adapter that refused every observation for a reason nobody could see.
        """
        self._store_origin = store_origin
        self._test_variant_id = test_variant_id
        self._expected_currency = expected_currency
        self._clock = clock or (lambda: datetime.now(UTC))
        self._observations = ShopifyCartObservationProvider(
            store_origin=store_origin,
            test_variant_id=test_variant_id,
            clock=self._clock,
        )

    # -- TargetAdapter -------------------------------------------------------

    def tool_specs(self) -> Sequence[TargetToolSpec]:
        """Nothing, because nothing here observes a tool call (FR-114).

        See the module docstring: an empty surface is what makes AC-18's
        `not_evaluated` trajectory structural instead of a promise, since §10.2
        then refuses any contract for this target that names a tool at all.
        """
        return ()

    def effect_map(self) -> Mapping[str, tuple[str, ...]]:
        """Empty, and it costs exactly what §9.1 says it costs.

        "An adapter that omits effect metadata may still be evaluated but
        receives only generic assertion classifications." There are no observed
        invocations here to attribute an effect to, and §12.2 forbids the
        harness from inferring one it was not told about.
        """
        return {}

    def observation_provider(self) -> ShopifyCartObservationProvider:
        return self._observations

    # -- ExternalTargetAdapter -----------------------------------------------

    def validate_origin(self, origin: str) -> None:
        """Refuse any origin but the exact configured one (FR-110)."""
        require_exact_origin(origin, self._store_origin)

    def normalize(self, payload: dict, provenance: str) -> Observation:
        """Validate a submitted `cart.js` read into an authoritative observation."""
        return self._observations.normalize(payload, provenance)

    # -- the locked configuration, published ---------------------------------

    def contract_parameters(self) -> Mapping[str, Any]:
        """The server-controlled scalars `templates.expand` needs (§13.5, FR-114).

        Read from the adapter — which the registry built from `ServiceSettings`
        — rather than assembled at the call site, so the variant and currency a
        contract asserts on are server-controlled by construction. A route that
        gathered them from a request body would be letting the caller choose
        which variant counted as correct.
        """
        return {
            "variant_id": self._test_variant_id,
            "expected_currency": self._expected_currency,
        }
