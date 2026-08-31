"""Causal classification: telling a wrong value apart from a lying tool.

Spec v1.9 §12.6/FR-055 (the false-success rule, quoted in full below), §13.4
(overlap at a dotted-key boundary, and "classification considers only the last
relevant intended-effect action"), §22 (the classification set), §23.1 (the
`tool_execution` layer), FR-033 (safe denial, expiry, and cancellation are not
execution errors).

This is the module the product is named after. Every other layer can tell you
that a value was wrong; this one decides whether a tool *said it had set that
value* and was contradicted by independent observation. FR-055 draws the line
narrowly, and the narrowness is the point:

> For a failed final assertion, the classifier shall find the last terminal
> agent-tool action whose declared intended effect overlaps the assertion path,
> regardless of whether state actually changed. It shall use
> `false_success_or_state_mismatch` only when that tool reported success and its
> immediate authoritative post-call effect observation also mismatches the
> assertion. If the relevant action failed, was cancelled, lacks an immediate
> observation, or has no declared effect relationship, the engine shall use
> `assertion_mismatch` rather than infer causality.

So four separate conditions must all hold before the strong claim is made, and
each one has a test. An engine that guessed here would produce the product's
most serious false positive: accusing a target of reporting success it never
reported.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from actionwitness_core.contracts.enums import TRANSITION_OPERATORS
from actionwitness_core.contracts.models import Assertion
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.assertions import evaluate_operator
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.engine.findings import Finding
from actionwitness_core.evidence.models import RunEvent, ordered
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType
from actionwitness_core.kernel import JsonValue
from actionwitness_core.reports.enums import LayerResult

__all__ = [
    "classify_assertion_failures",
    "execution_findings",
    "tool_execution_layer",
]

#: Terminal invocation events a causal search may consider.
_TERMINAL = frozenset(
    {
        OutcomeEventType.TOOL_INVOCATION_COMPLETED,
        OutcomeEventType.TOOL_INVOCATION_FAILED,
        OutcomeEventType.TOOL_INVOCATION_CANCELLED,
    }
)

#: FR-055 speaks of an "agent-tool action"; a replay drives the same tools under
#: the `eval` actor, and a replayed run must classify identically to its source
#: (AC-15) or the regression case would not reproduce its own failure.
_ACTING_ACTORS = frozenset({EventActor.AGENT, EventActor.EVAL})

#: FR-033: "expected denial, expiry, and cancellation of a protected action are
#: safe terminal outcomes and shall not be classified as tool_execution_error".
_SAFE_BLOCKS = frozenset(
    {
        OutcomeEventType.TOOL_INVOCATION_CANCELLED,
    }
)


def _last_relevant_action(
    path: ObservationPath,
    events: Sequence[RunEvent],
    effect_map: Mapping[str, tuple[ObservationPath, ...]],
) -> RunEvent | None:
    """The last terminal acting-tool action whose declared effect overlaps `path`.

    "Regardless of whether state actually changed" (FR-055): a tool that declared
    it would touch this path is the relevant action even if it touched nothing,
    which is precisely the false-success case. Overlap is evaluated at a
    dotted-key boundary (§13.4), so `target.cart` does not claim
    `target.cartridge`.
    """
    relevant = [
        event
        for event in ordered(events)
        if event.event_type in _TERMINAL
        and event.actor in _ACTING_ACTORS
        and event.tool_name is not None
        and any(prefix.overlaps(path) for prefix in effect_map.get(event.tool_name, ()))
    ]
    # §13.4: "only the last relevant intended-effect action", which prevents an
    # earlier tool being blamed for a value a later tool or human changed.
    return relevant[-1] if relevant else None


def _effect_observation_mismatches(
    assertion: Assertion, event: RunEvent, initial: Mapping[str, JsonValue] | None
) -> bool | None:
    """Does the immediate post-call observation also contradict the assertion?

    `None` means the event carried no immediate observation, which FR-055 lists
    as a reason to fall back rather than a reason to accuse.
    """
    if event.post_call_effect_state is None:
        return None
    needs_initial = assertion.operator in TRANSITION_OPERATORS
    if needs_initial and initial is None:
        return None
    held, _actual, _reason = evaluate_operator(
        assertion.operator,
        assertion.value,
        "value" in assertion.model_fields_set,
        _resolve_or_missing(assertion.path, initial),
        _resolve_or_missing(assertion.path, event.post_call_effect_state),
    )
    return not held


def _resolve_or_missing(path: ObservationPath, context: Mapping[str, JsonValue] | None):
    from actionwitness_core.contracts.paths import MISSING, resolve

    return MISSING if context is None else resolve(path, context)


def classify_assertion_failures(
    findings: Sequence[Finding],
    assertions: Sequence[Assertion],
    *,
    events: Sequence[RunEvent],
    effect_map: Mapping[str, tuple[ObservationPath, ...]] | None = None,
    initial: Mapping[str, JsonValue] | None = None,
) -> tuple[Finding, ...]:
    """Refine generic assertion mismatches into causal classifications (FR-055).

    Findings that are not failed assertion mismatches pass through untouched, so
    this is safe to run over a whole finding set. Every refined finding gains an
    `attributed_cause` recording which action was blamed and why - §17.1 stores
    that attribution, and a classification a reader cannot audit is not evidence.
    """
    by_id = {assertion.id: assertion for assertion in assertions}
    effects = effect_map or {}
    return tuple(
        _classify_one(finding, by_id.get(finding.check_id), events, effects, initial)
        for finding in findings
    )


def _classify_one(
    finding: Finding,
    assertion: Assertion | None,
    events: Sequence[RunEvent],
    effect_map: Mapping[str, tuple[ObservationPath, ...]],
    initial: Mapping[str, JsonValue] | None,
) -> Finding:
    if (
        assertion is None
        or finding.status is not CheckStatus.FAILED
        or finding.classification is not FailureClassification.ASSERTION_MISMATCH
        or finding.path is None
    ):
        return finding

    action = _last_relevant_action(finding.path, events, effect_map)
    if action is None:
        # "No declared effect relationship" - either the adapter published no
        # effect metadata (§12.2) or no tool claimed this path.
        return finding.model_copy(
            update={
                "attributed_cause": {
                    "kind": "none",
                    "reason": (
                        "no executed tool declared an effect overlapping this path, so "
                        "causality was not inferred"
                    ),
                }
            }
        )

    cause: dict[str, JsonValue] = {
        "kind": "tool_action",
        "tool_name": action.tool_name,
        "event_sequence": action.sequence_number,
        "terminal_event": action.event_type.value,
    }

    if action.event_type is not OutcomeEventType.TOOL_INVOCATION_COMPLETED:
        cause["reason"] = "the relevant action did not complete, so success was never claimed"
        return finding.model_copy(
            update={"attributed_cause": cause, "causal_event_sequence": action.sequence_number}
        )

    if not action.reported_success:
        cause["reason"] = "the relevant action completed without claiming success"
        cause["reported_status"] = action.reported_status.value if action.reported_status else None
        return finding.model_copy(
            update={"attributed_cause": cause, "causal_event_sequence": action.sequence_number}
        )

    mismatched = _effect_observation_mismatches(assertion, action, initial)
    if mismatched is None:
        cause["reason"] = (
            "the relevant action reported success but recorded no immediate authoritative "
            "post-call observation, so the contradiction could not be established"
        )
        return finding.model_copy(
            update={"attributed_cause": cause, "causal_event_sequence": action.sequence_number}
        )
    if not mismatched:
        cause["reason"] = (
            "the immediate post-call observation satisfied the assertion, so the final "
            "mismatch was caused by something later"
        )
        return finding.model_copy(
            update={"attributed_cause": cause, "causal_event_sequence": action.sequence_number}
        )

    cause["reason"] = (
        "the tool reported success and the immediate authoritative post-call observation "
        "contradicted the assertion"
    )
    return finding.model_copy(
        update={
            "classification": FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH,
            "attributed_cause": cause,
            "causal_event_sequence": action.sequence_number,
        }
    )


def tool_execution_layer(events: Sequence[RunEvent]) -> LayerResult:
    """The §23.1 `tool_execution` layer result.

    `blocked_safely` is not a failure: §23.1 says it "does not by itself fail the
    overall run; the contract's assertions and policies determine whether the
    resulting outcome is acceptable". Reporting a correctly refused checkout as
    an execution failure would punish the safe behaviour the product exists to
    encourage.
    """
    invocations = [
        event
        for event in ordered(events)
        if event.event_type in _TERMINAL and event.actor in _ACTING_ACTORS
    ]
    if not invocations:
        return LayerResult.NOT_EVALUATED
    if any(event.event_type is OutcomeEventType.TOOL_INVOCATION_FAILED for event in invocations):
        return LayerResult.FAILED
    blocked = any(
        event.event_type in _SAFE_BLOCKS
        or (
            event.reported_status is not None
            and event.reported_status.value.startswith("blocked_by_")
        )
        for event in invocations
    )
    return LayerResult.BLOCKED_SAFELY if blocked else LayerResult.PASSED


def execution_findings(events: Sequence[RunEvent]) -> tuple[Finding, ...]:
    """One critical finding per unexpected execution failure (§22).

    Only `tool_invocation_failed` qualifies. A cancellation or a
    consent-blocked completion is a safe terminal outcome under FR-033 and
    produces no finding at all.
    """
    from actionwitness_core.contracts.enums import AssertionSeverity
    from actionwitness_core.engine.enums import CheckType

    return tuple(
        Finding(
            check_id=f"tool_execution:{event.tool_name}:{event.sequence_number}",
            check_type=CheckType.POLICY,
            status=CheckStatus.FAILED,
            severity=AssertionSeverity.CRITICAL,
            classification=FailureClassification.TOOL_EXECUTION_ERROR,
            causal_event_sequence=event.sequence_number,
            evidence={
                "reason": "the tool returned an unexpected non-policy error",
                "tool_name": event.tool_name,
            },
        )
        for event in ordered(events)
        if event.event_type is OutcomeEventType.TOOL_INVOCATION_FAILED
        and event.actor in _ACTING_ACTORS
    )
