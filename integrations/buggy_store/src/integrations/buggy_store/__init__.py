"""integrations.buggy_store — the adapter between the assurance core and the demo store.

Implements `actionwitness_core.ports.ManagedTargetAdapter` for the separately
packaged Buggy Store, reaching it only through its versioned `/demo/api/v1`
surface (spec v1.9 §9.1, §13, §26.7; BUILD_ORDER invariant 3).

The dependency direction is one-way and enforced: this package imports the core
and `httpx`, the core imports neither this package nor the store, and the store
imports nothing from the assurance stack at all.
"""

from integrations.buggy_store.adapter import (
    ADAPTER_ID,
    DESCRIPTOR,
    TARGET_ID,
    BuggyStoreAdapter,
    ToolNotAllowed,
)
from integrations.buggy_store.observation import (
    OBSERVATION_NAMESPACE,
    PROVENANCE,
    PROVIDER_ID,
    BuggyStoreObservationProvider,
)
from integrations.buggy_store.templates import TEMPLATES, ContractTemplate, template_for
from integrations.buggy_store.tools import EFFECT_MAP, TOOL_NAMES, TOOL_SPECS

__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "EFFECT_MAP",
    "OBSERVATION_NAMESPACE",
    "PROVENANCE",
    "PROVIDER_ID",
    "TARGET_ID",
    "TEMPLATES",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "BuggyStoreAdapter",
    "BuggyStoreObservationProvider",
    "ContractTemplate",
    "ToolNotAllowed",
    "template_for",
]
