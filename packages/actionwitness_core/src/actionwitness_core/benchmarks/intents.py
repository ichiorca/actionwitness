"""Candidate intent variants, and the limits Python validates them against.

Spec v1.9 FR-100 ("up to six paraphrased, ambiguous, and adversarial variants …
Python shall schema-validate length and character limits"), §12.11, AC-17.

**A variant is model-authored text that becomes a benchmark input.** It is
therefore untrusted in the ordinary §5 sense — validated as data, never read as
an instruction — and this module is where that validation happens before a
human is asked to look at it. Screening for secrets and confirmation-bypass
language is the *next* stage (FR-100's second clause); this one establishes that
what arrives is text of a plausible shape at all.

**Six is a ceiling, not a target.** FR-100 says "up to six". A set that arrived
with more is refused rather than truncated: truncating would silently choose
which variants a human then approves, and the approval would cover a set nobody
selected.

**Control characters are refused, not stripped.** A zero-width joiner or a
bidirectional override in a benchmark intent is either corruption or an attempt
to make text render differently from how it validates. Stripping would hide
both; the variant is rejected and regenerated instead.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Annotated, Final

from pydantic import Field, StringConstraints, model_validator

from actionwitness_core.benchmarks.enums import VariantKind
from actionwitness_core.kernel import ContractError, CoreErrorCode, CoreModel, JsonValue

__all__ = [
    "MAX_INTENT_VARIANTS",
    "MAX_INTENT_VARIANT_CHARS",
    "MIN_INTENT_VARIANT_CHARS",
    "CandidateVariants",
    "IntentVariant",
    "VariantKind",
    "validate_candidates",
]

#: FR-100's ceiling.
MAX_INTENT_VARIANTS: Final = 6

#: Length bounds. The upper bound matches `MAX_TOOL_DESCRIPTION_CHARS` because a
#: variant plays the same role — a short natural-language instruction a model is
#: given — and reusing the established figure keeps one number to reason about
#: rather than two that drift. The lower bound exists because a one-word
#: "variant" is not a paraphrase of anything, and a human approving a list of
#: them would be approving noise.
MAX_INTENT_VARIANT_CHARS: Final = 500
MIN_INTENT_VARIANT_CHARS: Final = 8


#: Unicode general categories that have no place in a benchmark intent.
#: `Cc` control, `Cf` format (zero-width joiners, bidi overrides), `Cs`
#: surrogate, `Co` private use, `Cn` unassigned. Each is either corruption or a
#: way to make text render differently from how it validates.
_FORBIDDEN_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _offending_characters(text: str) -> list[str]:
    """Every character whose category this module refuses, named for a reader."""
    offending: list[str] = []
    for character in text:
        if unicodedata.category(character) in _FORBIDDEN_CATEGORIES:
            offending.append(f"U+{ord(character):04X}")
    return offending


class IntentVariant(CoreModel):
    """One candidate variant of the canonical intent.

    Carries its kind because FR-100 asks for three *kinds*, and a set that
    turned out to be six paraphrases would satisfy a count check while testing
    much less than the benchmark claims.
    """

    kind: VariantKind
    text: Annotated[
        str,
        StringConstraints(min_length=MIN_INTENT_VARIANT_CHARS, max_length=MAX_INTENT_VARIANT_CHARS),
    ]

    @model_validator(mode="after")
    def _text_is_plain_and_meaningful(self) -> IntentVariant:
        offending = _offending_characters(self.text)
        if offending:
            raise ContractError(
                f"variant text carries characters that cannot appear in an intent: "
                f"{', '.join(offending[:5])}. Regenerate the variant rather than "
                "stripping them — the difference between what renders and what "
                "validates is the problem.",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        if not self.text.strip():
            raise ContractError(
                "variant text is only whitespace",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"kind": self.kind.value, "text": self.text}


class CandidateVariants(CoreModel):
    """A generated set, awaiting screening and human approval.

    Deliberately *not* the thing that goes into a manifest. FR-100's sequence is
    generate → validate → screen → approve → freeze, and a type that could be
    frozen straight from generation would let the middle two be skipped by a
    caller in a hurry.
    """

    canonical_intent: Annotated[
        str,
        StringConstraints(min_length=MIN_INTENT_VARIANT_CHARS, max_length=MAX_INTENT_VARIANT_CHARS),
    ]
    variants: tuple[IntentVariant, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _within_the_ceiling_and_distinct(self) -> CandidateVariants:
        if len(self.variants) > MAX_INTENT_VARIANTS:
            raise ContractError(
                f"FR-100 allows up to {MAX_INTENT_VARIANTS} variants; this set has "
                f"{len(self.variants)}. Refused rather than truncated: truncating "
                "would choose which variants a human then approves.",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        seen = [variant.text.strip().casefold() for variant in self.variants]
        if len(set(seen)) != len(seen):
            raise ContractError(
                "two variants are the same text; a duplicate adds a repetition "
                "rather than a variant, and would weight the benchmark toward one "
                "phrasing without saying so",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        if any(text == self.canonical_intent.strip().casefold() for text in seen):
            raise ContractError(
                "a variant repeats the canonical intent verbatim; the canonical "
                "intent is already the baseline every variant is compared against",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    @property
    def kinds(self) -> frozenset[VariantKind]:
        return frozenset(variant.kind for variant in self.variants)

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "canonical_intent": self.canonical_intent,
            "variants": [variant.canonical_document() for variant in self.variants],
        }


def validate_candidates(
    canonical_intent: str, variants: Sequence[Mapping[str, object]]
) -> CandidateVariants:
    """FR-100's schema validation, over whatever generation produced.

    Takes loose mappings because the caller is handing over model output: the
    point of this function is that untrusted material becomes a typed value
    here and nowhere else, so no other module has to decide what a malformed
    variant means.
    """
    return CandidateVariants(
        canonical_intent=canonical_intent,
        variants=tuple(IntentVariant.model_validate(variant) for variant in variants),
    )
