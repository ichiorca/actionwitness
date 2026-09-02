"""Scenario modes and the injectable failure profiles (spec v1.9 §12.2, §13.3).

FR-011 fixes the vocabulary: six profiles, of which `none` is the honest one and
every other is "an injected unsafe behaviour of the embedded demo target,
labelled as such in the UI and reports". FR-011 also fixes what the two scenario
modes mean — "in `pre_fix`, the selected non-`none` fault is active; in
`post_fix`, it remains recorded as the comparison fault but is disabled".

That last clause is why a profile is *recorded* separately from whether it is
*active*. A `post_fix` run has to remember which fault it is the corrected
counterpart of, or FR-019's matched comparison has nothing to match on: the pair
differs only in scenario mode, and every other controlled input — including the
comparison fault identity — must be equal.

**All six are recognised; the shipped set grows one milestone at a time.**
BUILD_ORDER §7/M2 shipped `discount_reported_but_not_applied` and said to "keep
the other injected profiles disabled until their Tier 3 work is complete"; 013
added `undeclared_side_effect` and 012-T1 adds `duplicate_on_retry`. Selecting
an unimplemented one is refused with a stated reason rather than silently
treated as `none` — a store that quietly ran the honest path while a report said
a fault was active would be lying about the thing this application exists to
demonstrate.

Nothing here is a security control. These are deliberate defects in a demo
storefront; the harness is what makes them visible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "IMPLEMENTED_PROFILES",
    "PROFILE_DESCRIPTIONS",
    "UNSAFE_PROFILE_LABEL",
    "FaultProfile",
    "ScenarioConfiguration",
    "ScenarioMode",
]


class ScenarioMode(StrEnum):
    """FR-017's two modes, and exactly two.

    The UI copy is normative about what these are *not*: §23.7 says it "shall
    never imply that switching to `post_fix` changed source code or deployed a
    patch". Both implementations ship in the same build; the operator selects
    which one runs.
    """

    PRE_FIX = "pre_fix"
    POST_FIX = "post_fix"


class FaultProfile(StrEnum):
    """FR-011's closed profile list, transcribed in its published order."""

    NONE = "none"
    DUPLICATE_ON_RETRY = "duplicate_on_retry"
    DISCOUNT_REPORTED_BUT_NOT_APPLIED = "discount_reported_but_not_applied"
    CHECKOUT_WITHOUT_CONFIRMATION = "checkout_without_confirmation"
    UNDECLARED_SIDE_EFFECT = "undeclared_side_effect"
    TOOL_SURFACE_POISONED = "tool_surface_poisoned"


#: §13.3's description of each profile, so a UI or report can label an injected
#: behaviour without retyping the specification.
PROFILE_DESCRIPTIONS: Final[Mapping[FaultProfile, str]] = {
    FaultProfile.NONE: "No fault is injected. The store behaves correctly.",
    FaultProfile.DUPLICATE_ON_RETRY: (
        "A repeated update_cart with the same request ID and identical payload "
        "incorrectly applies the mutation again, while the tool response stays "
        "syntactically valid."
    ),
    FaultProfile.DISCOUNT_REPORTED_BUT_NOT_APPLIED: (
        "apply_discount returns an apparent success response while canonical cart "
        "state retains no discount and an unchanged total."
    ),
    FaultProfile.CHECKOUT_WITHOUT_CONFIRMATION: (
        "Checkout creates an order without a preceding approval event."
    ),
    FaultProfile.UNDECLARED_SIDE_EFFECT: (
        "A cart mutation additionally rewrites a state path no contract term "
        "mentions, such as a saved delivery preference."
    ),
    FaultProfile.TOOL_SURFACE_POISONED: (
        "A simulated third-party script registers a look-alike tool mid-run."
    ),
}

#: Shipped in this build. The rest are recognised, described, and refused.
IMPLEMENTED_PROFILES: Final[frozenset[FaultProfile]] = frozenset(
    {
        FaultProfile.NONE,
        FaultProfile.DISCOUNT_REPORTED_BUT_NOT_APPLIED,
        # 013-T5. `update_cart` performs a correct mutation and additionally
        # rewrites `preferences.delivery_note` — a path no built-in contract
        # asserts. Every declared assertion still passes and only
        # `no_undeclared_changes` fails, which is the demonstration §13.3 asks
        # this profile to make.
        FaultProfile.UNDECLARED_SIDE_EFFECT,
        # 012-T1. A repeated `update_cart` with the same request ID and an
        # identical payload applies the mutation a second time instead of
        # replaying the first result, while the response stays syntactically
        # valid. The retry is treated as a fresh delta rather than the absolute
        # assignment Appendix D.2 defines — re-applying the same absolute
        # quantity would change nothing, and a fault that leaves state
        # untouched is one `idempotent_by_request_id` cannot see.
        FaultProfile.DUPLICATE_ON_RETRY,
    }
)

#: FR-011 requires every non-`none` profile to be labelled as injected unsafe
#: behaviour wherever it is shown. One string, so the label cannot drift between
#: the storefront, the API, and a report.
UNSAFE_PROFILE_LABEL: Final = "injected unsafe demo behaviour"


@dataclass(frozen=True, slots=True)
class ScenarioConfiguration:
    """One workspace's immutable-per-run scenario selection (FR-012).

    Frozen because FR-012 fixes scenario mode and fault profile for an armed run:
    "changing any value requires reset and creates a new run; completed evidence
    is never relabeled".
    """

    mode: ScenarioMode = ScenarioMode.POST_FIX
    fault_profile: FaultProfile = FaultProfile.NONE

    @property
    def fault_active(self) -> bool:
        """FR-011: active in `pre_fix` only, and never for `none`."""
        return self.mode is ScenarioMode.PRE_FIX and self.fault_profile is not FaultProfile.NONE

    @property
    def is_unsafe(self) -> bool:
        """Whether this selection carries an injected defect at all."""
        return self.fault_profile is not FaultProfile.NONE

    def injects(self, profile: FaultProfile) -> bool:
        """True when `profile` is the selected fault *and* it is active."""
        return self.fault_active and self.fault_profile is profile

    def as_document(self) -> dict[str, object]:
        """What the API and the storefront show about the current selection.

        `fault_active` is reported alongside `fault_profile` rather than derived
        by each reader, because the difference between "recorded" and "running"
        is exactly what a `post_fix` run needs to communicate.
        """
        document: dict[str, object] = {
            "scenario_mode": str(self.mode),
            "fault_profile": str(self.fault_profile),
            "fault_active": self.fault_active,
            "description": PROFILE_DESCRIPTIONS[self.fault_profile],
        }
        if self.is_unsafe:
            document["label"] = UNSAFE_PROFILE_LABEL
        return document
