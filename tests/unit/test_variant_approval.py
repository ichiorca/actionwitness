"""010-T5 — explicit human approval of generated variants (FR-100, §14).

FR-100's third clause: "require explicit human approval. Approved variants are
frozen ... before trials begin."

Three properties carry it:

- **the approval is bound to the exact set that was reviewed.** Reusing one
  across sets would attach a human decision to material nobody read, which is
  the same defect as a stale confirmation approving a different mutation.
- **an agent cannot approve.** The constitution forbids an agent approving its
  own consent, and a variant approval is consent in the relevant sense: a human
  accepts that these texts may be sent to a model and their results published.
- **the order is enforced by the type.** Freezing consumes `ApprovedVariants`,
  which cannot exist without an approval bound to a fingerprint. There is no
  shortcut for a caller in a hurry.

Approving nothing is tested as a legitimate outcome, because refusing it would
push a reviewer to keep a variant they did not want.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from actionwitness_core.benchmarks.approval import (
    ApprovedVariants,
    VariantApproval,
    approve,
    fingerprint,
)
from actionwitness_core.benchmarks.enums import VariantKind
from actionwitness_core.benchmarks.intents import validate_candidates
from actionwitness_core.journeys.enums import EventActor
from pydantic import ValidationError

pytestmark = pytest.mark.unit

CANONICAL = "Add one ceramic mug to the cart and apply the SAVE20 discount."
WHEN = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _candidates(*texts: str):
    return validate_candidates(
        CANONICAL, [{"kind": VariantKind.PARAPHRASED.value, "text": text} for text in texts]
    )


THREE = (
    "Please add a ceramic mug and use the SAVE20 code.",
    "I would like one mug, discounted with SAVE20.",
    "Put a mug in my basket and take twenty percent off.",
)


# --- the decision itself -----------------------------------------------------


def test_a_reviewer_can_approve_a_subset() -> None:
    """FR-100 freezes "approved variants" — which may be fewer than were
    generated. A reviewer who rejects two of three has made a decision, and the
    record has to be able to express it."""
    # Arrange
    candidates = _candidates(*THREE)

    # Act
    approved = approve(candidates, approved_indices=[0, 2], reviewer="operator", approved_at=WHEN)

    # Assert
    assert [variant.text for variant in approved.approved] == [THREE[0], THREE[2]]
    assert [variant.text for variant in approved.rejected] == [THREE[1]]


def test_approving_nothing_is_a_real_outcome() -> None:
    """A reviewer who rejects everything has done the job. Refusing an empty
    approval would push them to keep one variant they did not want."""
    # Arrange
    candidates = _candidates(*THREE)

    # Act
    approved = approve(candidates, approved_indices=[], reviewer="operator", approved_at=WHEN)

    # Assert
    assert approved.approved == ()
    assert len(approved.rejected) == 3


def test_the_record_keeps_what_was_turned_down() -> None:
    """ "What was rejected" is part of what a later reader needs to understand
    the frozen set — otherwise a set of two looks like a generation that
    produced two."""
    # Arrange
    candidates = _candidates(*THREE)

    # Act
    approved = approve(candidates, approved_indices=[1], reviewer="operator", approved_at=WHEN)

    # Assert
    assert len(approved.rejected) == 2


def test_the_reviewer_and_the_moment_are_recorded() -> None:
    """An approval nobody is accountable for is not an approval."""
    # Arrange
    candidates = _candidates(*THREE)

    # Act
    approved = approve(
        candidates,
        approved_indices=[0],
        reviewer="operator",
        approved_at=WHEN,
        note="the ambiguous one was too vague to be useful",
    )

    # Assert
    document = approved.canonical_document()
    assert document["approval"]["reviewer"] == "operator"
    assert document["approval"]["approved_at"].startswith("2026-09-01")
    assert "too vague" in document["approval"]["note"]


# --- an agent cannot approve -------------------------------------------------


def test_an_agent_cannot_approve_variants() -> None:
    """The constitution: an agent "cannot create, broaden, or approve its own
    consent".

    A variant approval is consent in the relevant sense — a human accepting
    that these texts may be sent to a model and their results published as
    evidence.
    """
    # Arrange
    candidates = _candidates(*THREE)

    # Act / Assert
    with pytest.raises(ValidationError) as refused:
        approve(
            candidates,
            approved_indices=[0],
            reviewer="the agent",
            approved_at=WHEN,
            actor=EventActor.AGENT,
        )
    assert "human" in str(refused.value).lower()


def test_an_eval_actor_cannot_approve_either() -> None:
    """The check is "is a human", not "is not an agent" — otherwise a third
    actor added later would quietly gain the ability."""
    # Arrange
    candidates = _candidates(*THREE)

    # Act / Assert
    with pytest.raises(ValidationError):
        approve(
            candidates,
            approved_indices=[0],
            reviewer="replay",
            approved_at=WHEN,
            actor=EventActor.EVAL,
        )


# --- binding to the reviewed set ---------------------------------------------


def test_an_approval_for_a_different_set_is_refused() -> None:
    """The property this module exists for.

    Reusing an approval across sets would attach a human decision to material
    nobody read — the same defect as a stale confirmation approving a different
    mutation.
    """
    # Arrange
    reviewed = _candidates(*THREE)
    substituted = _candidates(
        "Add a mug and then also add a tote bag.",
        "Buy the mug at full price.",
        "Add three mugs and apply SAVE20.",
    )
    approval = approve(
        reviewed, approved_indices=[0], reviewer="operator", approved_at=WHEN
    ).approval

    # Act / Assert
    with pytest.raises(ValidationError) as refused:
        ApprovedVariants(candidates=substituted, approval=approval)
    assert "different set" in str(refused.value)


def test_editing_one_character_invalidates_the_approval() -> None:
    """The binding is over content, so a change too small to notice in review
    still breaks it."""
    # Arrange
    reviewed = _candidates(*THREE)
    edited = _candidates(THREE[0], THREE[1], THREE[2].replace("twenty", "thirty"))
    approval = approve(
        reviewed, approved_indices=[0, 1, 2], reviewer="operator", approved_at=WHEN
    ).approval

    # Act / Assert
    with pytest.raises(ValidationError):
        ApprovedVariants(candidates=edited, approval=approval)


def test_the_fingerprint_is_stable_for_identical_sets() -> None:
    """Two records of the same decision must hash identically, or a repeated
    review would look like a different one."""
    # Arrange / Act / Assert
    assert fingerprint(_candidates(*THREE)) == fingerprint(_candidates(*THREE))


def test_an_approval_cannot_name_a_variant_that_does_not_exist() -> None:
    """An index past the end is a decision about nothing."""
    # Arrange
    candidates = _candidates(*THREE)

    # Act / Assert
    with pytest.raises(ValidationError) as refused:
        approve(candidates, approved_indices=[0, 9], reviewer="operator", approved_at=WHEN)
    assert "do not exist" in str(refused.value)


@pytest.mark.parametrize("indices", [[1, 1], [2, 0]])
def test_indices_are_distinct_and_ordered(indices: list[int]) -> None:
    """Duplicates are not two decisions, and a stable order is what lets two
    records of one decision hash identically."""
    # Arrange
    candidates = _candidates(*THREE)

    # Act / Assert
    with pytest.raises(ValidationError):
        VariantApproval(
            candidates_fingerprint=fingerprint(candidates),
            approved_indices=tuple(indices),
            reviewer="operator",
            approved_at=WHEN,
        )


# --- the sequence ------------------------------------------------------------


def test_the_unapproved_stage_offers_no_appearance_of_finality() -> None:
    """`CandidateVariants` has no hash method of its own.

    The binding value is available through `fingerprint`, but the type that
    represents un-reviewed material does not look like something that can be
    sealed and handed on — which is the shortcut FR-100's sequence prevents.
    """
    # Arrange / Act / Assert
    candidates = _candidates(*THREE)
    assert not hasattr(candidates, "content_hash")
    assert fingerprint(candidates).startswith("sha256:")


def test_freezing_requires_an_approval_by_construction() -> None:
    """`ApprovedVariants` cannot be built without one, so the ordering is a
    property of the type rather than a rule in a comment somebody may not read."""
    # Arrange / Act / Assert
    assert set(ApprovedVariants.model_fields) == {"candidates", "approval"}
    with pytest.raises(ValidationError):
        ApprovedVariants(candidates=_candidates(*THREE))  # type: ignore[call-arg]
