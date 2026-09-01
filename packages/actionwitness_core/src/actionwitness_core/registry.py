"""The single exported registry of every closed enum the core publishes.

Spec v1.9 §15.8, §16, §17.1; BUILD_ORDER §7/M0 ("a machine-readable list of
stable API error codes and closed state/event enums so API handlers, UI, and
tests share names").

Each vocabulary module owns its own enums and descriptions and publishes an
`ENUM_REGISTRATIONS` tuple; this module concatenates them in a stable order. That
split keeps the assertion operators next to the contract code and the failure
classifications next to the engine, while still leaving exactly one place the
exporter, the frontend artifact, and the drift tests read from.

Order is load-bearing: `registry.json` is a committed artifact, so a reordering
shows up as a diff. Lifecycle enums come first, preserving the order they were
registered in during M0, and each later group is appended rather than
interleaved.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import ModuleType

from actionwitness_core.contracts import enums as contract_enums
from actionwitness_core.engine import enums as engine_enums
from actionwitness_core.evals import enums as eval_enums
from actionwitness_core.evidence import enums as evidence_enums
from actionwitness_core.journeys import enums as journey_enums
from actionwitness_core.ports import enums as port_enums
from actionwitness_core.reports import enums as report_enums

__all__ = [
    "CLOSED_ENUMS",
    "REGISTERED_ENUM_CLASSES",
    "REGISTRY_MODULES",
    "ClosedEnum",
]

#: Every module allowed to define a registered enum, in export order. The drift
#: test walks exactly this tuple, so an enum defined in a module missing from here
#: is invisible rather than merely unregistered - which is why adding a module is
#: part of adding a vocabulary, not an afterthought.
REGISTRY_MODULES: tuple[ModuleType, ...] = (
    journey_enums,
    contract_enums,
    port_enums,
    evidence_enums,
    engine_enums,
    report_enums,
    # Appended rather than interleaved, so `registry.json` gains rows at the end
    # instead of shifting every existing one (the artifact is committed, and a
    # reordering would be a diff with no meaning).
    eval_enums,
)


@dataclass(frozen=True, slots=True)
class ClosedEnum:
    """One registered enum plus the provenance a reader needs to check it."""

    name: str
    spec_ref: str
    members: Mapping[str, str]


def _members(descriptions: Mapping[StrEnum, str]) -> dict[str, str]:
    return {str(member.value): text for member, text in descriptions.items()}


#: Enum classes paired with their description maps, for coverage checking.
REGISTERED_ENUM_CLASSES: tuple[tuple[str, type[StrEnum], Mapping[StrEnum, str]], ...] = tuple(
    (name, enum_cls, descriptions)
    for module in REGISTRY_MODULES
    for name, _spec_ref, enum_cls, descriptions in module.ENUM_REGISTRATIONS
)

#: Every closed enum this project publishes, in a stable order. The registry
#: exporter and the "no undocumented member" test both iterate this tuple.
CLOSED_ENUMS: tuple[ClosedEnum, ...] = tuple(
    ClosedEnum(name, spec_ref, _members(descriptions))
    for module in REGISTRY_MODULES
    for name, spec_ref, _enum_cls, descriptions in module.ENUM_REGISTRATIONS
)
