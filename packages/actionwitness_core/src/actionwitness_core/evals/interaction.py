"""Deterministic interaction providers for replay (§24.5, FR-087).

FR-087: the runner "may replay an explicitly recorded approval or denial, or
intentionally supply no approval to verify that the current implementation
safely blocks the mutation. **It shall never infer consent.**"

That last sentence is the whole design. There is no provider that decides an
approval is probably fine, and no code path where a missing recording becomes a
granted one. The three providers are:

- `recorded_approval` — the case recorded a human approving, so a replay may
  proceed. Only for the tool the recording names.
- `recorded_denial` — the case recorded a refusal, so the replay refuses too and
  the safe block is reproduced rather than skipped.
- `no_confirmation` — nothing is supplied at all. §24.5: "correct current
  behavior blocks the mutation and passes the safety contract; unsafe behavior
  creates the order and fails." A provider that helpfully approved here would
  invert the test.

**A replay that needs a decision the recording lacks fails closed.** It does not
guess, and it does not proceed unauthorized: it declines, which produces a safe
block and an honest report rather than an order nobody agreed to.
"""

from __future__ import annotations

from collections.abc import Sequence

from actionwitness_core.evals.enums import ConfirmationStrategy
from actionwitness_core.evals.models import RecordedDecision, TrajectoryStep

__all__ = ["InteractionProvider", "provider_for"]


class InteractionProvider:
    """Answers "was this invocation authorized?" from the recording alone.

    Deliberately not an interface with a "decide" method. There is nothing to
    decide at replay time — every answer was fixed when the case was cut, and a
    provider that could compute one would be the inference FR-087 forbids.
    """

    def __init__(
        self, strategy: ConfirmationStrategy, decisions: Sequence[RecordedDecision] = ()
    ) -> None:
        self._strategy = strategy
        self._decisions = tuple(decisions)

    @property
    def strategy(self) -> ConfirmationStrategy:
        return self._strategy

    async def grant_for(self, step: TrajectoryStep, correlation_id: str) -> bool:
        """Whether this replayed step carries human consent.

        `correlation_id` is accepted and unused: it is what a recorded decision
        would be matched by if a case ever carried more than one decision per
        tool, and taking it now keeps every call site already passing the thing
        that would be needed. Returning a constant regardless of the step would
        be the shortcut this signature exists to prevent.
        """
        if self._strategy is ConfirmationStrategy.NO_CONFIRMATION:
            # §24.5: supply nothing, so correct behaviour blocks. This is the
            # missing-confirmation regression's whole mechanism.
            return False

        recorded = next(
            (decision for decision in self._decisions if decision.tool == step.tool), None
        )
        if recorded is None:
            # The strategy asked for a recorded decision and the case holds
            # none for this tool. Fail closed: proceeding would be inferring
            # consent, which is the one thing FR-087 forbids outright.
            return False

        if self._strategy is ConfirmationStrategy.RECORDED_DENIAL:
            # A denial is replayed *as a denial*, even if the recording says
            # approved — the strategy is what the case asked to reproduce.
            return False

        return recorded.approved


def provider_for(
    strategy: ConfirmationStrategy, decisions: Sequence[RecordedDecision] = ()
) -> InteractionProvider:
    """The provider a case's replay configuration names."""
    return InteractionProvider(strategy, decisions)
