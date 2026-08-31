"""Closed report vocabulary: the five layers, their legal values, and run mode.

Spec v1.9 §23.1 (the layer table and its closed value sets), FR-070 (the layered
result), BUILD_ORDER invariant 10 ("Model-selection, observed-trajectory,
execution, business-outcome, and safety-policy layers remain distinct").

The per-layer value sets are not decoration. `model_tool_selection` cannot take
`passed_with_warnings` because nothing in the source run judges it; `safety_policy`
cannot take `not_evaluated` because a policy is always either evaluated, failed,
or unresolvable; and only `tool_execution` may report `blocked_safely`, which is
the value that keeps a correctly refused mutation from reading as a failure.
Collapsing the five layers into one status is exactly the summary this product
exists to refuse.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "ALLOWED_LAYER_RESULTS",
    "ENUM_REGISTRATIONS",
    "LayerResult",
    "ReportLayer",
    "RunMode",
]


class ReportLayer(StrEnum):
    """The five distinct assurance layers (spec §23.1, FR-070)."""

    MODEL_TOOL_SELECTION = "model_tool_selection"
    OBSERVED_TRAJECTORY = "observed_trajectory"
    TOOL_EXECUTION = "tool_execution"
    BUSINESS_OUTCOME = "business_outcome"
    SAFETY_POLICY = "safety_policy"


REPORT_LAYER_DESCRIPTIONS: Mapping[ReportLayer, str] = {
    ReportLayer.MODEL_TOOL_SELECTION: (
        "Whether a model chose the right tools and arguments. Only ever populated "
        "from an explicitly correlated external evaluator trial; the source outcome "
        "report finalizes it as not_evaluated."
    ),
    ReportLayer.OBSERVED_TRAJECTORY: (
        "Whether the recorded invocation-start events conform to expected_tools. An "
        "observed-execution layer, never a model tool-selection score."
    ),
    ReportLayer.TOOL_EXECUTION: "Whether the tool calls themselves completed or failed.",
    ReportLayer.BUSINESS_OUTCOME: (
        "Whether authoritative state satisfied the contract's assertions."
    ),
    ReportLayer.SAFETY_POLICY: "Whether consent, retry, and scope policies held.",
}


class LayerResult(StrEnum):
    """Every value any layer may report (spec §23.1).

    Which subset a given layer may use is fixed by `ALLOWED_LAYER_RESULTS`; this
    enum is their union so one type can carry them all.
    """

    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    BLOCKED_SAFELY = "blocked_safely"
    FAILED = "failed"
    ERROR = "error"
    NOT_EVALUATED = "not_evaluated"


LAYER_RESULT_DESCRIPTIONS: Mapping[LayerResult, str] = {
    LayerResult.PASSED: "Every check in this layer held.",
    LayerResult.PASSED_WITH_WARNINGS: "No critical failure in this layer; warnings exist.",
    LayerResult.BLOCKED_SAFELY: (
        "A protected action was denied, expired, or cancelled without mutation. Not a "
        "failure by itself; the contract decides whether the outcome is acceptable."
    ),
    LayerResult.FAILED: "At least one critical check in this layer failed.",
    LayerResult.ERROR: "This layer could not be evaluated because the harness failed.",
    LayerResult.NOT_EVALUATED: (
        "Nothing in this run judged this layer. Never a pass, and never inferred from "
        "another layer's result."
    ),
}

#: The §23.1 table, verbatim. A layer may report only these values.
ALLOWED_LAYER_RESULTS: Mapping[ReportLayer, frozenset[LayerResult]] = {
    ReportLayer.MODEL_TOOL_SELECTION: frozenset(
        {LayerResult.PASSED, LayerResult.FAILED, LayerResult.NOT_EVALUATED}
    ),
    ReportLayer.OBSERVED_TRAJECTORY: frozenset(
        {LayerResult.PASSED, LayerResult.FAILED, LayerResult.NOT_EVALUATED}
    ),
    ReportLayer.TOOL_EXECUTION: frozenset(
        {
            LayerResult.PASSED,
            LayerResult.BLOCKED_SAFELY,
            LayerResult.FAILED,
            LayerResult.NOT_EVALUATED,
        }
    ),
    ReportLayer.BUSINESS_OUTCOME: frozenset(
        {
            LayerResult.PASSED,
            LayerResult.PASSED_WITH_WARNINGS,
            LayerResult.FAILED,
            LayerResult.ERROR,
            LayerResult.NOT_EVALUATED,
        }
    ),
    ReportLayer.SAFETY_POLICY: frozenset(
        {LayerResult.PASSED, LayerResult.FAILED, LayerResult.ERROR}
    ),
}


class RunMode(StrEnum):
    """Whether a run judged a contract or proposed one (spec §23.1, §16).

    A proposal run reports `business_outcome: not_evaluated`, a null overall
    result, and a candidate list rather than a verdict. Keeping the two modes in
    one closed enum is what stops a proposal from being read as a passing
    verification.
    """

    VERIFICATION = "verification"
    PROPOSAL = "proposal"


RUN_MODE_DESCRIPTIONS: Mapping[RunMode, str] = {
    RunMode.VERIFICATION: "The run judged a contract and carries a verdict.",
    RunMode.PROPOSAL: "The run derived assertion candidates and carries no verdict.",
}


ENUM_REGISTRATIONS: tuple[tuple[str, str, type[StrEnum], Mapping[StrEnum, str]], ...] = (
    ("report_layer", "spec §23.1 / FR-070", ReportLayer, REPORT_LAYER_DESCRIPTIONS),
    ("layer_result", "spec §23.1", LayerResult, LAYER_RESULT_DESCRIPTIONS),
    ("run_mode", "spec §23.1", RunMode, RUN_MODE_DESCRIPTIONS),
)
