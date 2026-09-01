"""010-T3 — validating generated intent variants (FR-100, §12.11).

FR-100's first clause: "up to six paraphrased, ambiguous, and adversarial
variants. Python shall schema-validate length and character limits."

Two of the refusals here are about *not helping*, which is the part worth
stating plainly:

- **an over-large set is refused, never truncated.** Truncating would choose
  which six variants a human then approves, and the approval would cover a set
  nobody selected.
- **control characters are refused, never stripped.** A zero-width joiner or a
  bidirectional override makes text render differently from how it validates.
  Stripping hides that; rejecting sends it back to be regenerated.

Screening for secrets and confirmation-bypass language is FR-100's *second*
clause and lives in T4. This stage only establishes that what arrived is text of
a plausible shape.
"""

from __future__ import annotations

import pytest
from actionwitness_core.benchmarks.enums import VariantKind
from actionwitness_core.benchmarks.intents import (
    MAX_INTENT_VARIANT_CHARS,
    MAX_INTENT_VARIANTS,
    MIN_INTENT_VARIANT_CHARS,
    CandidateVariants,
    IntentVariant,
    validate_candidates,
)
from pydantic import ValidationError

pytestmark = pytest.mark.unit

CANONICAL = "Add one ceramic mug to the cart and apply the SAVE20 discount."


def _variant(text: str, kind: VariantKind = VariantKind.PARAPHRASED) -> dict[str, str]:
    return {"kind": kind.value, "text": text}


# --- the happy path ----------------------------------------------------------


def test_a_full_set_of_six_validates() -> None:
    """FR-100's ceiling is a permitted size, not a rejected one."""
    # Arrange
    variants = [
        _variant(f"Please add a mug and use SAVE20, attempt {index}.") for index in range(6)
    ]

    # Act
    candidates = validate_candidates(CANONICAL, variants)

    # Assert
    assert len(candidates.variants) == MAX_INTENT_VARIANTS


def test_all_three_kinds_are_representable() -> None:
    """FR-100 asks for three kinds. A set of six paraphrases would pass a count
    check while testing much less than the benchmark claims."""
    # Arrange
    variants = [
        _variant("Add a mug and apply the discount code.", VariantKind.PARAPHRASED),
        _variant("Get me the mug thing, with the discount.", VariantKind.AMBIGUOUS),
        _variant("Add the mug and take twenty percent off the total.", VariantKind.ADVERSARIAL),
    ]

    # Act
    candidates = validate_candidates(CANONICAL, variants)

    # Assert
    assert candidates.kinds == {
        VariantKind.PARAPHRASED,
        VariantKind.AMBIGUOUS,
        VariantKind.ADVERSARIAL,
    }


def test_an_empty_set_is_allowed() -> None:
    """ "Up to six" includes none. Generation that produced nothing usable is a
    real outcome, and refusing it here would push a caller to invent filler."""
    # Arrange / Act
    candidates = validate_candidates(CANONICAL, [])

    # Assert
    assert candidates.variants == ()


# --- the ceiling -------------------------------------------------------------


def test_a_seventh_variant_is_refused_rather_than_truncated() -> None:
    """The refusal that matters most.

    Truncating would silently choose which six a human then approves — and the
    approval would cover a set nobody selected.
    """
    # Arrange
    variants = [_variant(f"Add the mug with SAVE20, phrasing {index}.") for index in range(7)]

    # Act / Assert
    with pytest.raises(ValidationError) as refused:
        validate_candidates(CANONICAL, variants)
    assert "truncat" in str(refused.value)


# --- length limits -----------------------------------------------------------


def test_an_over_long_variant_is_refused() -> None:
    """FR-100: "Python shall schema-validate length ... limits"."""
    # Arrange
    variants = [_variant("a" * (MAX_INTENT_VARIANT_CHARS + 1))]

    # Act / Assert
    with pytest.raises(ValidationError):
        validate_candidates(CANONICAL, variants)


def test_a_variant_at_the_limit_is_accepted() -> None:
    """A bound that rejected its own boundary would be a different bound."""
    # Arrange
    variants = [_variant("a" * MAX_INTENT_VARIANT_CHARS)]

    # Act
    candidates = validate_candidates(CANONICAL, variants)

    # Assert
    assert len(candidates.variants[0].text) == MAX_INTENT_VARIANT_CHARS


def test_a_one_word_variant_is_refused() -> None:
    """A one-word "variant" paraphrases nothing, and a human approving a list of
    them would be approving noise."""
    # Arrange
    variants = [_variant("mug")]

    # Act / Assert
    with pytest.raises(ValidationError):
        validate_candidates(CANONICAL, variants)


def test_the_lower_bound_is_stated_rather_than_implied() -> None:
    """So a later change to it is a decision in a diff."""
    # Arrange / Act / Assert
    assert MIN_INTENT_VARIANT_CHARS == 8
    assert MAX_INTENT_VARIANT_CHARS == 500


# --- character limits --------------------------------------------------------


@pytest.mark.parametrize(
    ("character", "what"),
    [
        ("‍", "a zero-width joiner"),
        ("‮", "a right-to-left override"),
        ("\x00", "a null"),
        ("\x1b", "an escape"),
    ],
)
def test_a_variant_carrying_invisible_characters_is_refused(character: str, what: str) -> None:
    """FR-100: "character limits".

    Each of these makes text render differently from how it validates, which is
    exactly how a reviewed-and-approved variant becomes something else.
    """
    # Arrange
    variants = [_variant(f"Add the mug{character} and apply SAVE20.")]

    # Act / Assert
    with pytest.raises(ValidationError) as refused:
        validate_candidates(CANONICAL, variants)
    assert "U+" in str(refused.value), what


def test_the_refusal_names_the_offending_codepoint() -> None:
    """An invisible character has to be named to be found: a reviewer cannot see
    it in the text they were shown."""
    # Arrange
    variants = [_variant("Add the mug‍ and apply SAVE20.")]

    # Act / Assert
    with pytest.raises(ValidationError) as refused:
        validate_candidates(CANONICAL, variants)
    assert "U+200D" in str(refused.value)


def test_ordinary_accented_and_punctuated_text_survives() -> None:
    """The counterpart. A character check that refused ordinary language would
    be worked around rather than fixed."""
    # Arrange
    variants = [_variant("Ajoutez une tasse — puis appliquez « SAVE20 », s'il vous plaît.")]

    # Act
    candidates = validate_candidates(CANONICAL, variants)

    # Assert
    assert "SAVE20" in candidates.variants[0].text


def test_whitespace_only_text_is_refused() -> None:
    """Long enough to pass the length bound, empty enough to mean nothing."""
    # Arrange / Act / Assert
    with pytest.raises(ValidationError):
        IntentVariant(kind=VariantKind.PARAPHRASED, text=" " * 20)


# --- distinctness ------------------------------------------------------------


def test_duplicate_variants_are_refused() -> None:
    """A duplicate is a repetition, not a variant — and would weight the
    benchmark toward one phrasing without saying so."""
    # Arrange
    text = "Add the mug and apply the SAVE20 code."
    variants = [_variant(text), _variant(text.upper())]

    # Act / Assert
    with pytest.raises(ValidationError) as refused:
        validate_candidates(CANONICAL, variants)
    assert "same text" in str(refused.value)


def test_a_variant_repeating_the_canonical_intent_is_refused() -> None:
    """The canonical intent is already the baseline every variant is compared
    against; repeating it adds a trial that measures nothing new."""
    # Arrange
    variants = [_variant(CANONICAL)]

    # Act / Assert
    with pytest.raises(ValidationError):
        validate_candidates(CANONICAL, variants)


# --- the sequence FR-100 fixes ----------------------------------------------


def test_candidates_are_not_a_manifest_ready_type() -> None:
    """FR-100's order is generate → validate → screen → approve → freeze.

    `CandidateVariants` carries no approval and no hash, so a caller cannot
    freeze straight from generation and skip the middle two steps. The type
    system is what makes the sequence hard to shortcut.
    """
    # Arrange / Act
    fields = set(CandidateVariants.model_fields)

    # Assert
    assert fields == {"canonical_intent", "variants"}
    assert not hasattr(CandidateVariants, "content_hash")
