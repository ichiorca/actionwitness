"""FR-100's generate step, stopped one stage short of approval.

Spec v1.9 FR-100 ("generate up to six paraphrased, ambiguous, and adversarial
variants … reject variants containing secrets or instructions to bypass
confirmation … require explicit human approval"), FR-099, §12.11, AC-17;
constitution §5.

FR-100's sequence is **generate → validate → screen → approve → freeze**. This
module owns the first three and deliberately cannot perform the last two: it
returns `CandidateVariants`, which the core designed to be un-freezable, and it
never imports `approve` or `freeze`. A reviewer types their name into
`POST /benchmarks/{id}/frozen-variants` and that route remains the only place a
variant becomes part of a manifest.

Saying it as structure rather than as a rule matters, because the tempting
shortcut here is real: the model has just written six plausible sentences, the
service already holds the suite, and auto-approving would remove a click. It
would also record a human decision nobody made, which is precisely what the
constitution's "an agent cannot create, broaden, or approve its own consent"
forbids — the agent would be approving the material it is about to be measured
against.

**The proposer is a protocol, injected.** This module names no vendor, imports
no integration, and holds no HTTP client; the composition root decides whether a
live backend exists at all. A deployment with the module off never constructs
one, and §21.1's rule — one absent integration disables only itself — holds
without a branch in here.

**The credential is read here and nowhere else in the service.** `config.py`
stores the variable's *name* on purpose, so the value is fetched at the moment
of use from an injected environment mapping, handed straight to the client, and
never written into settings, application state, or a model this module returns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from actionwitness_core.benchmarks.intents import (
    MAX_INTENT_VARIANTS,
    CandidateVariants,
    validate_candidates,
)
from actionwitness_core.benchmarks.screening import require_screened

__all__ = [
    "MAX_GENERATED_VARIANTS",
    "CredentialUnavailable",
    "VariantGenerationService",
    "VariantProposer",
    "live_credential",
]

#: The same ceiling FR-100 gives, read from the core rather than retyped. A
#: second number here would drift, and the drift would show up as a route
#: cheerfully asking for seven variants the core then refuses.
MAX_GENERATED_VARIANTS = MAX_INTENT_VARIANTS


class VariantProposer(Protocol):
    """Whatever can draft candidate variants.

    Structural rather than a base class, so the integration's client satisfies
    it without importing this module and a test's fake satisfies it without
    importing the integration. The return type is loose mappings on purpose —
    the same shape `validate_candidates` takes — because a proposer that handed
    over already-typed core models would have done the validation this service
    is responsible for.
    """

    async def propose(
        self, canonical_intent: str, *, count: int = 3
    ) -> Sequence[Mapping[str, object]]:
        """Candidate rephrasings, or raise. Never a shorter set on failure."""
        ...


class CredentialUnavailable(RuntimeError):
    """The configured credential variable holds nothing in this process.

    Distinct from "the module is off": an operator who set
    `LIVE_EVALUATOR_ENABLED` and forgot the key needs to see a mistake rather
    than an absence, which is the same distinction `ModuleStatus` draws between
    `misconfigured` and `disabled`.
    """


def live_credential(credential_var: str, environ: Mapping[str, str]) -> str:
    """The credential value, from an injected environment mapping.

    Injected rather than reaching for `os.environ` here, so every absence is
    testable without mutating process state — the same reason `config.py`
    resolves settings from a mapping. The composition root passes the real
    environment; nothing stores the result.
    """
    value = environ.get(credential_var, "").strip()
    if not value:
        raise CredentialUnavailable(
            f"{credential_var} is named as this deployment's model credential and "
            "holds no value. The value reaches this process only from the "
            "environment (FR-099); nothing here can supply one."
        )
    return value


class VariantGenerationService:
    """Draft candidates, validate them, screen them, hand them to a human.

    Holds no database handle and opens no transaction. Everything it does is one
    outbound call plus pure CPU over at most six short strings, and ADR-0003
    forbids a transaction spanning the first — so the caller reads whatever it
    needs about the suite before and after, and this stays a function of its
    inputs.
    """

    def __init__(self, proposer: VariantProposer, *, credential_var: str = "") -> None:
        """`credential_var` is deployment knowledge the core cannot have.

        The *name* of the variable this deployment's credential lives in — never
        its value. The core is target-neutral and has no way to know it, and a
        variant quoting the name is a secret being carried in. Empty when no
        live backend is configured, which is the ordinary state and not an error.
        """
        self._proposer = proposer
        self._credential_var = credential_var.strip()

    async def propose(self, canonical_intent: str, *, count: int) -> CandidateVariants:
        """FR-100's first three stages, in order, over untrusted model output.

        The order is the control:

        1. **Screen for credential material first.** A model that echoed a
           credential-shaped key into its answer is an incident, and FR-099's
           remedy — rotate the value — is different from every other refusal
           here. Naming it before a shape error means the operator is told what
           actually happened.
        2. **Validate.** `validate_candidates` is the single point where loose
           model output becomes typed values: the ceiling, the length bounds,
           the forbidden character categories, the duplicate check, and the
           "does not merely repeat the canonical intent" check all live there.
        3. **Screen the typed set.** `require_screened` refuses the *whole* set
           if any variant carries secret-shaped material or asks for a
           confirmation to be skipped, because a reviewer must never be shown a
           text asking them to approve bypassing a safeguard.

        What comes back is `CandidateVariants` — un-approved, un-freezable, and
        the exact type the freeze route's own screening will re-derive from what
        the reviewer submits. Nothing is persisted: FR-100 freezes the set a
        human approved, not the set a model proposed.
        """
        from integrations.google_evals.live import screen_for_credential_material

        proposed = await self._proposer.propose(canonical_intent, count=count)

        # Screened as a document, by key, before anything is typed. The core's
        # screen looks at variant *text*; this one looks at the *shape* the
        # model returned, which is where a `{"api_key": ...}` would appear.
        screen_for_credential_material(
            {"variants": [dict(variant) for variant in proposed]},
            _CredentialName(self._credential_var),
        )

        candidates = validate_candidates(canonical_intent, proposed)
        markers = (self._credential_var,) if self._credential_var else ()
        return require_screened(candidates, extra_secret_markers=markers)


class _CredentialName:
    """The one attribute `screen_for_credential_material` reads.

    A three-line adapter rather than passing `ServiceSettings.live_evaluator`
    straight through, because this service must also be usable with a fake
    proposer and no configured backend at all — and because the integration's
    screen documents its input as "something carrying a `credential_var`", not
    as the service's settings type.
    """

    __slots__ = ("credential_var",)

    def __init__(self, credential_var: str) -> None:
        self.credential_var = credential_var
