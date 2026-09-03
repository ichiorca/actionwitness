"""The harness tools the `self` target publishes (§12.20, FR-171).

These are the *harness's own* capabilities, re-declared here as a target
surface. That double life is the point of §12.20: the tools an agent uses to
drive ActionWitness become, for a self-witnessing run, the tools under test —
and the harness's own canonical workspace state becomes the thing observed
independently of what they claim.

**Read-only tools are read-only here too.** `get_run_findings` reports; it does
not move anything, so it declares no effect paths and §12.2 gives it no causal
attribution. The two that change state — arming a contract and resetting a
workspace — declare exactly which observed paths they may move, so a false
success is attributable rather than merely detectable.

**`verify_outcome` is deliberately absent.** A self-witnessing run that could
tell its observed workspace to verify would be able to drive the very machinery
recording it, and FR-172's recursion cap exists to keep those apart. The tools
here read and arm; they never ask the observed workspace to reach a verdict.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from actionwitness_core.ports.enums import RetrySemantics, SideEffectClass
from actionwitness_core.ports.models import TargetToolSpec

__all__ = [
    "ARM_OUTCOME_CONTRACT",
    "EFFECT_MAP",
    "GET_OUTCOME_CONTRACT",
    "GET_RUN_FINDINGS",
    "GET_WORKSPACE_STATUS",
    "LIST_CONTRACT_TEMPLATES",
    "RESET_WORKSPACE",
    "TOOL_NAMES",
    "TOOL_SPECS",
    "published_names",
    "spec_for",
]

GET_WORKSPACE_STATUS: Final = "get_workspace_status"
LIST_CONTRACT_TEMPLATES: Final = "list_contract_templates"
GET_OUTCOME_CONTRACT: Final = "get_outcome_contract"
GET_RUN_FINDINGS: Final = "get_run_findings"
ARM_OUTCOME_CONTRACT: Final = "arm_outcome_contract"
RESET_WORKSPACE: Final = "reset_workspace"

TOOL_SPECS: Final[tuple[TargetToolSpec, ...]] = (
    TargetToolSpec(
        name=GET_WORKSPACE_STATUS,
        description="Read the observed workspace's phase, selection, and guidance.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetrySemantics.READ_ONLY_SAFE,
    ),
    TargetToolSpec(
        name=LIST_CONTRACT_TEMPLATES,
        description="List the built-in outcome-contract templates available to the workspace.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetrySemantics.READ_ONLY_SAFE,
    ),
    TargetToolSpec(
        name=GET_OUTCOME_CONTRACT,
        description="Read the observed workspace's selected outcome contract.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetrySemantics.READ_ONLY_SAFE,
    ),
    TargetToolSpec(
        name=GET_RUN_FINDINGS,
        description="Read the findings of the observed workspace's most recent run.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        side_effect=SideEffectClass.READ_ONLY,
        retry=RetrySemantics.READ_ONLY_SAFE,
    ),
    TargetToolSpec(
        name=ARM_OUTCOME_CONTRACT,
        description="Arm the observed workspace's selected contract, starting a run.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        # Arming is idempotent by design (§16.1): a second call returns the run
        # the first one started rather than opening another. That is what
        # `naturally_idempotent` records, and it is the property FR-173's
        # "arming twice does not create two runs" contract checks — against the
        # observed run id, not against this declaration.
        side_effect=SideEffectClass.MUTATING,
        retry=RetrySemantics.NATURALLY_IDEMPOTENT,
    ),
    TargetToolSpec(
        name=RESET_WORKSPACE,
        description="Return the observed workspace to its ready state.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        side_effect=SideEffectClass.MUTATING,
        retry=RetrySemantics.NATURALLY_IDEMPOTENT,
    ),
)

TOOL_NAMES: Final[tuple[str, ...]] = tuple(spec.name for spec in TOOL_SPECS)

#: §13.4's declared target-effect prefixes, per tool.
#:
#: Only the two state-changing tools appear. §12.2 forbids the harness from
#: inferring an effect it was not told about, so a read tool with no entry loses
#: causal attribution and nothing else — which is correct, because a read that
#: appeared to cause a change would be the more alarming claim.
EFFECT_MAP: Final[Mapping[str, tuple[str, ...]]] = {
    ARM_OUTCOME_CONTRACT: ("workspace.run",),
    RESET_WORKSPACE: ("workspace.run", "workspace.phase"),
}


def published_names() -> tuple[str, ...]:
    return TOOL_NAMES


def spec_for(name: str) -> TargetToolSpec | None:
    return next((spec for spec in TOOL_SPECS if spec.name == name), None)
