"""§16.4's benchmark-suite state machine.

A table plus one function, for the same reason the other lifecycles in this core
are shaped that way: the permitted transitions are a specification sentence, and
a reader should be able to check the code against it without following control
flow through a service.

**`ready` is where bindings freeze.** §16.4: "bindings become immutable when the
suite enters `ready`". That is why there is no transition back to `draft` — a
suite whose bindings changed is a different suite, and FR-091's one-to-one
guarantee would otherwise hold only until somebody edited it.

**`completed` is terminal and means finalized.** §16.4 again: "a changed
manifest, adapter, binding, or source artifact requires a new suite; completed
suites are immutable". Recalculation produces a new artifact version beside the
old one (FR-094), never an edit in place.
"""

from __future__ import annotations

from collections.abc import Mapping

from actionwitness_core.benchmarks.enums import BenchmarkStatus, CorrelationMode
from actionwitness_core.kernel import ContractError, CoreErrorCode

__all__ = ["PERMITTED_TRANSITIONS", "can_transition", "require_transition"]

#: §16.4's table, verbatim. Terminal states map to an empty set rather than
#: being absent, so "terminal" is a fact this table states rather than one a
#: caller infers from a missing key.
PERMITTED_TRANSITIONS: Mapping[BenchmarkStatus, frozenset[BenchmarkStatus]] = {
    BenchmarkStatus.DRAFT: frozenset(
        {BenchmarkStatus.READY, BenchmarkStatus.CANCELLED, BenchmarkStatus.ERROR}
    ),
    BenchmarkStatus.READY: frozenset(
        {
            BenchmarkStatus.RUNNING,
            BenchmarkStatus.COMPLETED,
            BenchmarkStatus.CANCELLED,
            BenchmarkStatus.ERROR,
        }
    ),
    BenchmarkStatus.RUNNING: frozenset(
        {BenchmarkStatus.COMPLETED, BenchmarkStatus.CANCELLED, BenchmarkStatus.ERROR}
    ),
    BenchmarkStatus.COMPLETED: frozenset(),
    BenchmarkStatus.CANCELLED: frozenset(),
    BenchmarkStatus.ERROR: frozenset(),
}


def can_transition(current: BenchmarkStatus, target: BenchmarkStatus) -> bool:
    """Whether §16.4 permits this move."""
    return target in PERMITTED_TRANSITIONS[current]


def require_transition(
    current: BenchmarkStatus,
    target: BenchmarkStatus,
    *,
    correlation_mode: CorrelationMode | None = None,
) -> BenchmarkStatus:
    """The target status, or a refusal naming what was attempted.

    `ready` → `completed` is the one transition that depends on the mode. §16.4
    permits it because "an `executed_browser` suite may transition directly from
    `ready` to `completed`: its outcome runs already exist". An
    `imported_trajectory_replay` suite has to pass through `running`, because its
    outcome evidence does not exist until the replay produces it — skipping that
    would finalize a matrix over outcomes nobody observed.
    """
    if not can_transition(current, target):
        permitted = sorted(status.value for status in PERMITTED_TRANSITIONS[current])
        raise ContractError(
            f"a benchmark suite cannot move from {current.value} to {target.value}; "
            f"§16.4 permits {permitted or 'nothing — this state is terminal'}",
            code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
        )
    if (
        current is BenchmarkStatus.READY
        and target is BenchmarkStatus.COMPLETED
        and correlation_mode is CorrelationMode.IMPORTED_TRAJECTORY_REPLAY
    ):
        raise ContractError(
            "an imported_trajectory_replay suite cannot finalize straight from ready: "
            "its outcome evidence does not exist until the replay produces it, so it "
            "must pass through running (§16.4)",
            code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
        )
    return target
