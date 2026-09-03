"""integrations.shopify — the authorized development-store target (Tier 3).

Spec: FR-110..FR-119; §13.5 behavior; §16.5 pairing state machine.

Two targets live in this distribution and they are not the same thing:

* **`adapter.ShopifyAdapter`** (`shopify-development-store`) — one *configured*
  development store the project is authorized on, observed through the paired
  theme bridge. This is the cart proof.
* **`audit.ExternalAuditAdapter`** (`external-audit`) — §12.17's audit of an
  origin the operator asserts authorization for, which is a different feature
  with a different consent story. `pack.py` belongs to it.

They share the `cart.js` normalizer and the exact-origin rule (`audit.py` owns
both) precisely so the two paths cannot drift into two different ideas of what a
cart is worth.
"""

from integrations.shopify.adapter import (
    ADAPTER_ID,
    DESCRIPTOR,
    TARGET_ID,
    TARGET_TYPE,
    ShopifyAdapter,
)
from integrations.shopify.observation import (
    ShopifyCartObservationProvider,
    ShopifyCartUnobservable,
)
from integrations.shopify.templates import (
    TEMPLATE_ID,
    TEMPLATES,
    ContractTemplate,
    TemplateExpansionError,
    expand,
    template_for,
    template_ids,
)

__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "TARGET_ID",
    "TARGET_TYPE",
    "TEMPLATES",
    "TEMPLATE_ID",
    "ContractTemplate",
    "ShopifyAdapter",
    "ShopifyCartObservationProvider",
    "ShopifyCartUnobservable",
    "TemplateExpansionError",
    "expand",
    "template_for",
    "template_ids",
]
