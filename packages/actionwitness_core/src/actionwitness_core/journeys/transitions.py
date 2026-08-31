"""Pure lifecycle transition validation for runs, evals, and benchmark suites.

Spec v1.9 §16 (the outcome-run table), §16.2 (eval runs), §16.4 (benchmark
suites); BUILD_ORDER §7/M1 ("closed run/eval/benchmark state enums and pure
transition validation. Persistence of transitions remains in the application
layer").

The tables below are transcribed from the specification, one row per state. They
are data, not code, so a reviewer can check them against §16 line by line -
which is the only way a transition table ever gets checked.

Two modelling decisions follow the specification's own wording:

* **`reset` is not a state.** §16: "reset is a workspace action rather than a
  persisted run state and is valid from every workspace/run state". So the tables
  map states to states, and reset is a separate predicate that is always true.
  Encoding it as a state would put a terminal run one hop from being reopened.
* **A proposal run has no verdict.** §16 says it "never enters `running`,
  `verifying`, or any verdict state", so no transition out of `proposing` or
  `capturing` reaches one, and a test asserts the two subgraphs stay disjoint.

Nothing here writes anything. The application decides *when* to transition and
records it; this decides only whether the move is legal, which is what lets the
same rule govern an API request, a replay, and a test.
"""

from __future__ import annotations

from collections.abc import Mapping

from actionwitness_core.journeys.enums import BenchmarkSuiteState, EvalRunState, RunState
from actionwitness_core.kernel import CoreErrorCode, ErrorDetail, TransitionError

__all__ = [
    "BENCHMARK_SUITE_TRANSITIONS",
    "EVAL_RUN_TRANSITIONS",
    "PROPOSAL_RUN_STATES",
    "RUN_TRANSITIONS",
    "TERMINAL_BENCHMARK_SUITE_STATES",
    "TERMINAL_EVAL_RUN_STATES",
    "TERMINAL_RUN_STATES",
    "VERDICT_RUN_STATES",
    "can_reset",
    "is_terminal",
    "validate_benchmark_suite_transition",
    "validate_eval_run_transition",
    "validate_run_transition",
]

#: §16, transcribed. A state mapping to the empty set is terminal: the only move
#: left is `reset`, which is not a state.
RUN_TRANSITIONS: Mapping[RunState, frozenset[RunState]] = {
    RunState.ARMED: frozenset({RunState.RUNNING, RunState.CANCELLED, RunState.ERROR}),
    RunState.PROPOSING: frozenset({RunState.CAPTURING, RunState.CANCELLED, RunState.ERROR}),
    RunState.CAPTURING: frozenset({RunState.PROPOSED, RunState.CANCELLED, RunState.ERROR}),
    RunState.PROPOSED: frozenset(),
    RunState.RUNNING: frozenset(
        {
            RunState.AWAITING_CONFIRMATION,
            RunState.VERIFYING,
            RunState.CANCELLED,
            RunState.ERROR,
        }
    ),
    RunState.AWAITING_CONFIRMATION: frozenset(
        {RunState.RUNNING, RunState.CANCELLED, RunState.ERROR}
    ),
    RunState.VERIFYING: frozenset(
        {
            RunState.PASSED,
            RunState.PASSED_WITH_WARNINGS,
            RunState.FAILED,
            RunState.ERROR,
        }
    ),
    RunState.PASSED: frozenset(),
    RunState.PASSED_WITH_WARNINGS: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.ERROR: frozenset(),
    RunState.CANCELLED: frozenset(),
}

#: §16.2, transcribed.
EVAL_RUN_TRANSITIONS: Mapping[EvalRunState, frozenset[EvalRunState]] = {
    EvalRunState.QUEUED: frozenset(
        {EvalRunState.RUNNING, EvalRunState.CANCELLED, EvalRunState.ERROR}
    ),
    EvalRunState.RUNNING: frozenset(
        {
            EvalRunState.PASSED,
            EvalRunState.FAILED,
            EvalRunState.CANCELLED,
            EvalRunState.ERROR,
        }
    ),
    EvalRunState.PASSED: frozenset(),
    EvalRunState.FAILED: frozenset(),
    EvalRunState.CANCELLED: frozenset(),
    EvalRunState.ERROR: frozenset(),
}

#: §16.4, transcribed. `ready` may reach `completed` directly because an
#: `executed_browser` suite's outcome runs already exist.
BENCHMARK_SUITE_TRANSITIONS: Mapping[BenchmarkSuiteState, frozenset[BenchmarkSuiteState]] = {
    BenchmarkSuiteState.DRAFT: frozenset(
        {
            BenchmarkSuiteState.READY,
            BenchmarkSuiteState.CANCELLED,
            BenchmarkSuiteState.ERROR,
        }
    ),
    BenchmarkSuiteState.READY: frozenset(
        {
            BenchmarkSuiteState.RUNNING,
            BenchmarkSuiteState.COMPLETED,
            BenchmarkSuiteState.CANCELLED,
            BenchmarkSuiteState.ERROR,
        }
    ),
    BenchmarkSuiteState.RUNNING: frozenset(
        {
            BenchmarkSuiteState.COMPLETED,
            BenchmarkSuiteState.CANCELLED,
            BenchmarkSuiteState.ERROR,
        }
    ),
    BenchmarkSuiteState.COMPLETED: frozenset(),
    BenchmarkSuiteState.CANCELLED: frozenset(),
    BenchmarkSuiteState.ERROR: frozenset(),
}

#: The three states a proposal-mode run may occupy (§16).
PROPOSAL_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.PROPOSING, RunState.CAPTURING, RunState.PROPOSED}
)

#: States that carry a business verdict. A proposal run reaches none of them.
VERDICT_RUN_STATES: frozenset[RunState] = frozenset(
    {RunState.PASSED, RunState.PASSED_WITH_WARNINGS, RunState.FAILED}
)

TERMINAL_RUN_STATES: frozenset[RunState] = frozenset(
    state for state, targets in RUN_TRANSITIONS.items() if not targets
)
TERMINAL_EVAL_RUN_STATES: frozenset[EvalRunState] = frozenset(
    state for state, targets in EVAL_RUN_TRANSITIONS.items() if not targets
)
TERMINAL_BENCHMARK_SUITE_STATES: frozenset[BenchmarkSuiteState] = frozenset(
    state for state, targets in BENCHMARK_SUITE_TRANSITIONS.items() if not targets
)


def _validate(current, target, table: Mapping, machine: str) -> None:
    if current not in table:
        raise TransitionError(
            f"{current!r} is not a {machine} state",
            code=CoreErrorCode.INVALID_STATE_TRANSITION,
        )
    if target not in table:
        raise TransitionError(
            f"{target!r} is not a {machine} state",
            code=CoreErrorCode.INVALID_STATE_TRANSITION,
        )
    if target not in table[current]:
        permitted = sorted(str(state) for state in table[current])
        raise TransitionError(
            f"a {machine} may not move from {current.value!r} to {target.value!r}; "
            f"permitted transitions are {permitted or 'none (terminal)'}",
            code=CoreErrorCode.INVALID_STATE_TRANSITION,
            details=(
                ErrorDetail(
                    location=f"{machine}.status",
                    message=f"{current.value} -> {target.value} is not permitted",
                ),
            ),
        )


def validate_run_transition(current: RunState, target: RunState) -> None:
    """Refuse an outcome-run transition §16 does not permit.

    The application maps this refusal onto HTTP 409 with a stable code (§16:
    "invalid non-reset state transitions shall return HTTP 409"); the status code
    is not decided here, because the core carries no transport concern.
    """
    _validate(current, target, RUN_TRANSITIONS, "run")


def validate_eval_run_transition(current: EvalRunState, target: EvalRunState) -> None:
    """Refuse an eval-run transition §16.2 does not permit."""
    _validate(current, target, EVAL_RUN_TRANSITIONS, "eval run")


def validate_benchmark_suite_transition(
    current: BenchmarkSuiteState, target: BenchmarkSuiteState
) -> None:
    """Refuse a benchmark-suite transition §16.4 does not permit."""
    _validate(current, target, BENCHMARK_SUITE_TRANSITIONS, "benchmark suite")


def is_terminal(state: RunState | EvalRunState | BenchmarkSuiteState) -> bool:
    """True when no transition leads out of `state`."""
    for table in (RUN_TRANSITIONS, EVAL_RUN_TRANSITIONS, BENCHMARK_SUITE_TRANSITIONS):
        if state in table:
            return not table[state]
    raise TransitionError(
        f"{state!r} belongs to no registered state machine",
        code=CoreErrorCode.INVALID_STATE_TRANSITION,
    )


def can_reset(state: RunState) -> bool:
    """§16: reset "is valid from every workspace/run state".

    Always true, and written as a function rather than assumed at call sites so
    the rule is stated once and a future exception has somewhere to live. Reset
    cancels nonterminal work; it does not reopen a terminal run, and it is a
    workspace action rather than a run transition.
    """
    return state in RUN_TRANSITIONS
