"""Scenario selection and reseeding through the adapter (FR-011, FR-013, §9.1).

Until this task, the harness recorded a scenario selection and never told the
target about it. Every test that needed a genuinely faulty store had to drive the
store's own `/demo` surface directly and say so in a comment — which was honest,
but it meant the product's own control surface did not work.

FR-013: reset "shall ... **reseed managed-target state through the adapter when
supported**". FR-011: the selection "must be chosen before arming", and in
`post_fix` the profile "remains recorded as the comparison fault but is disabled
by the Buggy Store adapter" — so the harness records the same profile in both
modes and the *adapter* decides what that means. §9.1 is explicit that the
harness "neither interprets mode names nor implements a fault".

**The target is prepared before the selection is recorded.** If preparation
fails, nothing is written: a workspace whose column says `pre_fix` while the
target is still in `post_fix` would arm a run against a scenario nobody
selected, and every verdict from it would be labelled with a lie. Recording only
after the target agrees is what keeps the two in step.

**Reset is the other order,** and FR-013 fixes it: cancel the run and append the
cancellation events first, *then* reseed. A reseed that ran first would wipe the
state a cancelled run's evidence describes.

Nothing here holds a lock or a transaction across the adapter call (ADR-0003).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from actionwitness_core.kernel import CoreError
from actionwitness_core.ports import ScopedTargetAdapter
from actionwitness_core.ports.models import ScenarioSelection

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry
from actionwitness_service.application.self_witness import scope_target_adapter

__all__ = ["ReseedOutcome", "ScenarioPreparer"]


@dataclass(frozen=True)
class ReseedOutcome:
    """Whether the target was reseeded, and why not when it was not.

    Reported rather than swallowed. FR-013 says "when supported", and a caller
    who asked for a reset deserves to know whether the target actually moved —
    a silent no-op would leave them believing the target was clean.
    """

    reseeded: bool
    reason: str

    @classmethod
    def done(cls, mode: str) -> ReseedOutcome:
        return cls(reseeded=True, reason=f"target reseeded in {mode}")

    @classmethod
    def skipped(cls, reason: str) -> ReseedOutcome:
        return cls(reseeded=False, reason=reason)


class ScenarioPreparer:
    """Puts the selected target into the workspace's selected scenario."""

    def __init__(self, registry: AdapterRegistry) -> None:
        self._registry = registry

    async def prepare(
        self,
        workspace_id: str,
        *,
        target_id: str | None,
        scenario_mode: str | None,
        failure_profile: str | None,
        observed_workspace_id: str | None = None,
    ) -> ReseedOutcome:
        """Reseed the target for this scenario, or explain why it was skipped.

        A missing target or an unselected mode are both "not supported" rather
        than errors: a workspace that has chosen nothing yet has nothing to
        prepare, and §21.1 keeps an absent optional target a bounded state.
        """
        if not target_id:
            return ReseedOutcome.skipped("no target is selected")
        if not scenario_mode:
            return ReseedOutcome.skipped("no scenario mode is selected")

        slot = self._registry.resolve(target_id)
        if slot is None or not slot.is_available:
            return ReseedOutcome.skipped(
                slot.state.reason if slot else "no adapter is registered for this target"
            )

        adapter = slot.factory()  # type: ignore[misc]
        if not hasattr(adapter, "prepare"):
            # §9.1: an `ExternalTargetAdapter` is observed and never driven, so
            # it has no `prepare` and reseeding it is not merely unsupported but
            # meaningless.
            return ReseedOutcome.skipped("this target is observed rather than driven")

        if isinstance(adapter, ScopedTargetAdapter) and not observed_workspace_id:
            return ReseedOutcome.skipped(
                "the target workspace will be provisioned when the run is armed"
            )
        adapter = scope_target_adapter(adapter, workspace_id, observed_workspace_id)

        selection = ScenarioSelection(
            scenario_mode=scenario_mode,
            # Recorded in both modes. FR-011 keeps the profile as the comparison
            # fault in `post_fix` and lets the adapter disable it, which is what
            # makes a matched pre/post pair describe one changed variable.
            fault_profile=failure_profile,
        )
        try:
            # The fixture is empty: §9.8's fixture restoration arrives with the
            # replay milestone, and passing something the adapter would refuse
            # is worse than passing nothing.
            await adapter.prepare(workspace_id, {}, selection)
        except CoreError:
            # The adapter validated the selection against its own descriptor and
            # refused. Propagated so the §15.8 envelope names the offending
            # field rather than reporting a generic failure.
            raise
        except Exception as exc:
            raise ApiError(
                ApiErrorCode.TARGET_UNAVAILABLE,
                "The target could not be prepared for this scenario.",
                details=[{"path": "scenario_mode", "message": type(exc).__name__}],
            ) from exc

        return ReseedOutcome.done(scenario_mode)

    def supported_modes(self, target_id: str | None) -> tuple[str, ...]:
        """What the selected adapter advertises (§9.1)."""
        return self._registry.supported_scenario_modes(target_id)


def scenario_of(workspace: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """The workspace's selected target, mode, and profile, as stored."""
    return (
        workspace["selected_target_id"],
        workspace["scenario_mode"],
        workspace["failure_profile"],
    )
