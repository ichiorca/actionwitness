"""The in-memory template catalogue behind §15.2's instantiate route (012-T5).

Seeding already puts each built-in template's *document* in the database, and
that is enough to list and read them. Instantiating needs something seeding
does not store: which scalars a template allowlists, and the arithmetic that
turns one into a complete contract. Three mugs at 25.00 with SAVE20 come to
60.00, and a contract that raised the quantity without the total would fail a
journey that behaved correctly — the worst way for an assurance harness to be
wrong.

That arithmetic is target knowledge, so it stays in the integration that owns
it (constitution §1: the core and the service are target-neutral). This module
is the seam. It holds callables the composition root supplied and knows nothing
about carts, discounts, or currencies; `api/app.py` builds it from whichever
integrations are available, exactly as it already does for seeding.

`ExpansionRejected` is the service's own error so the boundary has one type to
catch. An integration raises whatever it likes; the composition root translates.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = ["ExpansionRejected", "TemplateCatalogue", "TemplateExpansion"]


class ExpansionRejected(Exception):
    """A flat submission the template cannot accept (FR-021).

    Carries `(field, message)` pairs rather than one sentence, because §15.8's
    envelope can name every offending control at once and a form that says only
    "invalid" makes a person guess which one they got wrong.
    """

    def __init__(self, details: Sequence[tuple[str, str]]) -> None:
        self.details = tuple(details)
        super().__init__("; ".join(f"{field}: {message}" for field, message in self.details))


@dataclass(frozen=True, slots=True)
class TemplateExpansion:
    """One template's allowlist and the callable that expands it.

    `parameters` is published to the client so the form can render exactly the
    controls this template accepts. It is a convenience, never the enforcement:
    `expand` re-checks the allowlist, because a browser deciding which fields
    are legal would be the client authorizing its own input.
    """

    template_id: str
    parameters: tuple[str, ...]
    expand: Callable[[Mapping[str, Any]], Mapping[str, Any]]


class TemplateCatalogue:
    """Every instantiable template this deployment composed.

    An absent integration contributes nothing and is not an error (§21.1) — the
    catalogue is simply smaller, and a request naming one of its templates is
    refused by name rather than by a crash.
    """

    def __init__(self, expansions: Iterable[TemplateExpansion] = ()) -> None:
        self._by_id = {expansion.template_id: expansion for expansion in expansions}

    def parameters_for(self, template_id: str) -> tuple[str, ...]:
        """The scalars this template allowlists; empty for one it does not know.

        Empty is honest for an unknown template: it accepts no scalar, and a
        listing that carried controls for a template nothing can expand would
        offer a form that always fails.
        """
        expansion = self._by_id.get(template_id)
        return () if expansion is None else expansion.parameters

    def expand(self, template_id: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        """Expand one trusted template, or refuse with field-level detail."""
        expansion = self._by_id.get(template_id)
        if expansion is None:
            raise ExpansionRejected([("template_id", f"unknown template {template_id!r}")])
        return expansion.expand(parameters)
