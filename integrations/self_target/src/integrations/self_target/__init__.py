"""integrations.self_target — ActionWitness as its own target (§12.20, FR-171).

The module is `self_target` because `self` is a Python keyword and
`integrations.self` will not import. The *target id* is `self`; only the module
name differs, and nothing outside this package sees the difference.
"""

from integrations.self_target.adapter import (
    ADAPTER_ID,
    DESCRIPTOR,
    TARGET_ID,
    SelfTargetAdapter,
    UnboundSelfTarget,
    UnknownSelfTool,
)
from integrations.self_target.observation import (
    PROVENANCE,
    PROVIDER_ID,
    SelfObservationLoop,
    SelfObservationProvider,
)
from integrations.self_target.templates import (
    TEMPLATES,
    ContractTemplate,
    TemplateExpansionError,
    expand,
    template_for,
    template_ids,
)
from integrations.self_target.tools import TOOL_NAMES, TOOL_SPECS

__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "PROVENANCE",
    "PROVIDER_ID",
    "TARGET_ID",
    "TEMPLATES",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "ContractTemplate",
    "SelfObservationLoop",
    "SelfObservationProvider",
    "SelfTargetAdapter",
    "TemplateExpansionError",
    "UnboundSelfTarget",
    "UnknownSelfTool",
    "expand",
    "template_for",
    "template_ids",
]
