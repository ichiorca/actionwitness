"""actionwitness_core.ports — the public protocols a target or application implements.

These are the extension surface promised by spec v1.8 §29.2 ("documented
ManagedTargetAdapter/ExternalTargetAdapter/ObservationProvider protocol") and the
one-way boundary enforced by §26.7 / LD-7-8: the core talks ONLY to these
protocols; it never imports an integration, demo, or commerce module.

Scaffolding: names and docstrings only. Method signatures are added with the
first Tier 1 vertical slice; keep them minimal and versioned.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class ManagedTargetAdapter(Protocol):
    """A target the harness can drive AND restore (fixture replay) — e.g. Buggy Store.

    Spec: §9.1 targets, FR-084/FR-086 replay through the adapter, §26.7 gates.
    """


@runtime_checkable
class ExternalTargetAdapter(Protocol):
    """A target the harness can observe but not restore — e.g. the Shopify
    development store (§12.12): observations arrive via a bridge, replay is unsupported.
    """


@runtime_checkable
class ObservationProvider(Protocol):
    """A named, trusted source of business-state observations (§9.3): canonical
    target state, journey events, or an external platform session API
    (`shopify_cart_state`, provenance `platform_session_api` — FR-112/FR-117).
    """


class Repository(Protocol):
    """Persistence port (§17): implemented by the application layer (aiosqlite),
    never by actionwitness_core itself.
    """
