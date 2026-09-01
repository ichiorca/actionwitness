"""Explicit human approval of generated variants (FR-100, §14, constitution §5).

Spec v1.9 FR-100 ("require explicit human approval. Approved variants are frozen
into the content-hashed benchmark manifest before trials begin"), §12.11.

**Approval is bound to the exact set that was screened.** A `VariantApproval`
carries the fingerprint of the candidates it was given, and `ApprovedVariants`
refuses to exist unless the two agree. That is what makes "approved *these*
variants" a checkable statement rather than a claim about a list that may have
changed between the reviewing and the freezing — the same binding 006 applies to
a protected mutation, for the same reason.

**An agent cannot approve.** The constitution is unambiguous that an agent
"cannot create, broaden, or approve its own consent", and a benchmark variant is
consent in the relevant sense: a human is accepting that these texts may be sent
to a model and their results published as evidence. The actor is checked here,
not left to a caller.

**Approving nothing is a real outcome.** A reviewer who rejects all six variants
has done the job, and the frozen set is then empty — the benchmark runs the
canonical intent alone. Refusing an empty approval would push a reviewer to keep
one variant they did not want.

**Order is enforced by the type, not by a comment.** Freezing (FR-100's next
clause) consumes `ApprovedVariants`, which cannot be constructed without an
approval; and an approval cannot be constructed without a fingerprint of a
screened set. A caller in a hurry has no shortcut available.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from actionwitness_core.benchmarks.intents import CandidateVariants, IntentVariant
from actionwitness_core.journeys.enums import EventActor
from actionwitness_core.kernel import (
    ContractError,
    CoreErrorCode,
    CoreModel,
    JsonValue,
    UtcInstant,
)
from actionwitness_core.security.canonical import content_hash

__all__ = [
    "ApprovedVariants",
    "FrozenVariantSet",
    "VariantApproval",
    "fingerprint",
    "freeze",
]

type ContentHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


def fingerprint(candidates: CandidateVariants) -> str:
    """The hash an approval is bound to.

    A module-level function rather than a method on `CandidateVariants`,
    deliberately. That type is the *un-approved* stage, and giving it a
    `content_hash` would make it look like something that could be sealed and
    handed on — which is exactly the shortcut FR-100's sequence exists to
    prevent. The binding value is available; the appearance of finality is not.
    """
    return content_hash(candidates.canonical_document())


class VariantApproval(CoreModel):
    """One human's explicit decision about one screened set."""

    #: The set this decision was made about. An approval that does not match
    #: the set being frozen is refused rather than reinterpreted.
    candidates_fingerprint: ContentHash
    #: Which variants were approved, by position. Explicit rather than "all":
    #: a reviewer who rejects two of six has made a decision the record should
    #: carry, and an implicit "everything" cannot express it.
    approved_indices: tuple[Annotated[int, Field(ge=0)], ...] = ()
    #: Who approved. Checked, not trusted: the constitution forbids an agent
    #: approving its own consent, and this is the point where that would happen.
    actor: EventActor = EventActor.HUMAN
    reviewer: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    approved_at: UtcInstant
    #: Optional free text from the reviewer. Bounded, and never interpreted.
    note: Annotated[str, StringConstraints(max_length=500)] = ""

    @model_validator(mode="after")
    def _a_human_approved_a_definite_set(self) -> VariantApproval:
        if self.actor is not EventActor.HUMAN:
            raise ContractError(
                f"a variant approval must be made by a human; this one names "
                f"{self.actor.value}. An agent cannot approve the material it "
                "will then be measured against (constitution §5).",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        if len(set(self.approved_indices)) != len(self.approved_indices):
            raise ContractError(
                "an approval names the same variant twice; a decision recorded "
                "twice is not a decision about two things",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        if list(self.approved_indices) != sorted(self.approved_indices):
            raise ContractError(
                "approved indices are not in order; a stable order is what lets "
                "two records of the same decision hash identically",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "candidates_fingerprint": self.candidates_fingerprint,
            "approved_indices": list(self.approved_indices),
            "actor": self.actor.value,
            "reviewer": self.reviewer,
            "approved_at": self.approved_at.isoformat(),
            "note": self.note,
        }


class ApprovedVariants(CoreModel):
    """The screened set plus the decision made about it.

    The only type freezing accepts. Constructing it re-checks the binding, so a
    caller cannot pair an approval with a different set even by assembling the
    parts by hand.
    """

    candidates: CandidateVariants
    approval: VariantApproval

    @model_validator(mode="after")
    def _the_approval_matches_this_set(self) -> ApprovedVariants:
        expected = fingerprint(self.candidates)
        if self.approval.candidates_fingerprint != expected:
            raise ContractError(
                "this approval was given for a different set of variants. "
                "Re-review the set you intend to freeze — an approval is a "
                "statement about specific texts, and reusing one across sets "
                "would attach a human decision to material nobody read.",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        limit = len(self.candidates.variants)
        out_of_range = [index for index in self.approval.approved_indices if index >= limit]
        if out_of_range:
            raise ContractError(
                f"approval names variants that do not exist: {out_of_range} (the set has {limit})",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    @property
    def approved(self) -> tuple[IntentVariant, ...]:
        """Exactly the variants the reviewer named, in the set's own order."""
        return tuple(self.candidates.variants[index] for index in self.approval.approved_indices)

    @property
    def rejected(self) -> tuple[IntentVariant, ...]:
        """The rest. Kept reachable because "what was turned down" is part of
        the record a later reader needs to understand the frozen set."""
        approved = set(self.approval.approved_indices)
        return tuple(
            variant
            for index, variant in enumerate(self.candidates.variants)
            if index not in approved
        )

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "canonical_intent": self.candidates.canonical_intent,
            "approved": [variant.canonical_document() for variant in self.approved],
            "approval": self.approval.canonical_document(),
        }


def approve(
    candidates: CandidateVariants,
    *,
    approved_indices: Sequence[int],
    reviewer: str,
    approved_at: object,
    note: str = "",
    actor: EventActor = EventActor.HUMAN,
) -> ApprovedVariants:
    """Record a decision about `candidates`.

    The fingerprint is computed here rather than accepted from the caller: a
    caller-supplied one could name a set the reviewer never saw, which is the
    single thing this whole module exists to prevent.
    """
    approval = VariantApproval(
        candidates_fingerprint=fingerprint(candidates),
        approved_indices=tuple(approved_indices),
        actor=actor,
        reviewer=reviewer,
        approved_at=approved_at,  # type: ignore[arg-type]
        note=note,
    )
    return ApprovedVariants(candidates=candidates, approval=approval)


class FrozenVariantSet(CoreModel):
    """Approved variants, sealed for a manifest (FR-100's final clause).

    **This is the stage that carries a content hash**, and the contrast with
    `CandidateVariants` is deliberate. Un-reviewed material has no hash because
    it must not look sealable; this type has one because it *is* sealed, and
    because FR-100 requires the variants to live in "the content-hashed
    benchmark manifest".

    It keeps the approval alongside the texts rather than only the texts. A
    frozen set whose provenance had been dropped would be indistinguishable
    from one somebody typed in, and the whole point of the review is that a
    named person accepted these specific words.
    """

    canonical_intent: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    variants: tuple[IntentVariant, ...] = ()
    approval: VariantApproval

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "canonical_intent": self.canonical_intent,
            "variants": [variant.canonical_document() for variant in self.variants],
            "approval": self.approval.canonical_document(),
        }

    def content_hash(self) -> str:
        """Over the intent, the variants, and the decision that admitted them."""
        return content_hash(self.canonical_document())


def freeze(approved: ApprovedVariants) -> FrozenVariantSet:
    """Seal an approved set for the manifest.

    Takes `ApprovedVariants` and nothing looser, so freezing something that was
    never reviewed is not expressible. The rejected variants are deliberately
    *not* carried forward: they were not approved, and a manifest that listed
    them would invite a later reader to wonder whether they ran.
    """
    return FrozenVariantSet(
        canonical_intent=approved.candidates.canonical_intent,
        variants=approved.approved,
        approval=approved.approval,
    )
