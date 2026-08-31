"""Observed-trajectory conformance: did the required calls actually occur?

Spec v1.9 §10.3 (expected-tool semantics), §12.6/FR-056 (the engine "shall
deterministically compare eligible target-tool invocation-start events with
`expected_tools`"; "human events are excluded"), §22 (`missing_expected_tool`,
`trajectory_order_violation`), §23.1 (the `observed_trajectory` layer).

The naming discipline matters as much as the algorithm. §10.3 closes with "this
check is deterministic and evaluates recorded execution only. It shall not be
labeled as model tool-selection evaluation" - the calls happened, and that is all
this proves. Whether a model *chose* them is a different layer that only an
imported evaluator report can fill, and conflating the two would let the product
claim exactly the thing it was built to distinguish.

Order of checks is deliberate: multiset containment first, subsequence second. A
required call that never happened at all is `missing_expected_tool`, not an
ordering complaint, and reporting it as the latter would send a reader looking
for a call that is not there.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from actionwitness_core.contracts.enums import AssertionSeverity
from actionwitness_core.contracts.models import ExpectedTools
from actionwitness_core.engine.enums import CheckStatus, CheckType, FailureClassification
from actionwitness_core.engine.findings import Finding
from actionwitness_core.evidence.models import RunEvent, ordered
from actionwitness_core.journeys.enums import EventActor

__all__ = [
    "ELIGIBLE_TRAJECTORY_ACTORS",
    "EXPECTED_TOOLS_CHECK_ID",
    "evaluate_expected_tools",
    "observed_calls",
]

#: FR-056 excludes human events; §10.3 establishes an occurrence from an
#: invocation start with actor `agent` in an outcome run or `eval` in a replay.
#: Harness and external events are excluded because neither invokes a target tool.
ELIGIBLE_TRAJECTORY_ACTORS: frozenset[EventActor] = frozenset({EventActor.AGENT, EventActor.EVAL})

#: One finding covers the whole `expected_tools` term, so its identifier is
#: stable across runs and sorts lexically alongside the policy check IDs (§22).
EXPECTED_TOOLS_CHECK_ID = "expected_tools"


def observed_calls(events: Sequence[RunEvent]) -> tuple[str, ...]:
    """Eligible target-tool invocation starts, in recorded order (§10.3).

    Only *starts* count. §10.3: "an occurrence is established by an immutable
    `tool_invocation_started` event... success or failure is evaluated separately
    in the tool-execution layer." A call that was made and failed still happened,
    and folding execution failure into this layer would make one defect look like
    two.
    """
    return tuple(
        event.tool_name
        for event in ordered(events)
        if event.is_invocation_start
        and event.actor in ELIGIBLE_TRAJECTORY_ACTORS
        and event.tool_name is not None
    )


def _is_subsequence(required: Sequence[str], observed: Sequence[str]) -> bool:
    """Greedy subsequence match (§10.3: "unrelated extra calls may occur between")."""
    remaining = iter(observed)
    return all(any(call == want for call in remaining) for want in required)


def evaluate_expected_tools(expected: ExpectedTools | None, events: Sequence[RunEvent]) -> Finding:
    """Compare the recorded trajectory with the contract's `expected_tools`.

    Returns `not_evaluated` when the contract omits the term - §10.3 makes that
    the defined outcome, and it is reported rather than assumed, so a reader can
    never mistake "no trajectory requirement" for "the trajectory was correct".
    """
    observed = observed_calls(events)
    if expected is None:
        return Finding(
            check_id=EXPECTED_TOOLS_CHECK_ID,
            check_type=CheckType.EXPECTED_TOOLS,
            status=CheckStatus.NOT_EVALUATED,
            severity=AssertionSeverity.CRITICAL,
            evidence={
                "reason": "the contract declares no expected_tools",
                "observed_calls": list(observed),
            },
        )

    required = list(expected.calls)
    shortfall = Counter(required) - Counter(observed)
    evidence = {
        "ordered": expected.ordered,
        "required_calls": required,
        "observed_calls": list(observed),
    }

    if shortfall:
        # §10.3: each entry requires one distinct occurrence, so duplicates
        # express multiplicity and a shortfall is counted, not merely detected.
        return Finding(
            check_id=EXPECTED_TOOLS_CHECK_ID,
            check_type=CheckType.EXPECTED_TOOLS,
            status=CheckStatus.FAILED,
            severity=AssertionSeverity.CRITICAL,
            classification=FailureClassification.MISSING_EXPECTED_TOOL,
            causal_event_sequence=_first_eligible_sequence(events),
            evidence={
                **evidence,
                "missing_calls": sorted(shortfall.elements()),
                "reason": "a required tool call did not occur",
            },
        )

    if expected.ordered and not _is_subsequence(required, observed):
        return Finding(
            check_id=EXPECTED_TOOLS_CHECK_ID,
            check_type=CheckType.EXPECTED_TOOLS,
            status=CheckStatus.FAILED,
            severity=AssertionSeverity.CRITICAL,
            classification=FailureClassification.TRAJECTORY_ORDER_VIOLATION,
            causal_event_sequence=_first_eligible_sequence(events),
            evidence={
                **evidence,
                "reason": "every required call occurred, but not in the required order",
            },
        )

    return Finding(
        check_id=EXPECTED_TOOLS_CHECK_ID,
        check_type=CheckType.EXPECTED_TOOLS,
        status=CheckStatus.PASSED,
        severity=AssertionSeverity.CRITICAL,
        evidence=evidence,
    )


def _first_eligible_sequence(events: Sequence[RunEvent]) -> int | None:
    """The sequence of the first eligible invocation start, for §22 ordering.

    A trajectory failure concerns the journey rather than one call, so it is
    attributed to where the journey began. With no eligible call at all it has no
    causal event and §22 sorts it after every finding that has one.
    """
    for event in ordered(events):
        if event.is_invocation_start and event.actor in ELIGIBLE_TRAJECTORY_ACTORS:
            return event.sequence_number
    return None
