"""Closed eval vocabulary: environments, consent strategies, and eval status.

Spec v1.9 §24.4 (environment profiles), §24.5 (confirmation replay), §9.8 (eval
run status), FR-087 (never infer consent), FR-088 (the CI contract).

Two of these carry the milestone's sharpest distinctions.

**`EvalStatus` is not a business outcome.** §24.3: "Eval-run status is based on
expectation matching, not on whether the actual business outcome string is
literally `passed`." A `reproduce_source` run that faithfully recreates a
recorded `failed` outcome has eval status `passed`, because reproducing the
failure is what it was asked to do. Keeping the two in separate enums is what
stops a later reader "fixing" the apparent contradiction — `LayerResult` says
what the target did, `EvalStatus` says whether the case's expectation held.

**`ConfirmationStrategy` has no "approve" member.** The three values replay what
a recording contained or supply nothing at all. There is deliberately no value
meaning "grant consent now", because FR-087 forbids inferring it and the
constitution forbids an agent creating its own — a missing-confirmation
regression works precisely because `no_confirmation` lets correct behaviour
block the mutation.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "ENUM_REGISTRATIONS",
    "TERMINAL_EVAL_STATUSES",
    "ConfirmationStrategy",
    "EvalEnvironment",
    "EvalStatus",
    "SourceProtocol",
]


class EvalEnvironment(StrEnum):
    """§24.4's two allowlisted runtime profiles.

    The *names* are target-neutral on purpose. What `current` means for a given
    target — for the Buggy Store, `post_fix` with no active fault — is target
    knowledge, and §24.4's mapping lives in the integration layer. A core that
    knew `pre_fix` would be a core that knew about one demo store.
    """

    CURRENT = "current"
    REPRODUCE_SOURCE = "reproduce_source"


EVAL_ENVIRONMENT_DESCRIPTIONS: Mapping[EvalEnvironment, str] = {
    EvalEnvironment.CURRENT: (
        "Request the target's corrected implementation with no injected failure. "
        "The CI default, and never silently replaced by the other profile."
    ),
    EvalEnvironment.REPRODUCE_SOURCE: (
        "Request the immutable source scenario and its recorded failure profile, "
        "to demonstrate that the case still reproduces the failure it was cut from."
    ),
}


class ConfirmationStrategy(StrEnum):
    """§24.5's deterministic interaction providers.

    No member grants consent the recording did not contain (FR-087).
    """

    RECORDED_APPROVAL = "recorded_approval"
    RECORDED_DENIAL = "recorded_denial"
    NO_CONFIRMATION = "no_confirmation"


CONFIRMATION_STRATEGY_DESCRIPTIONS: Mapping[ConfirmationStrategy, str] = {
    ConfirmationStrategy.RECORDED_APPROVAL: (
        "Replay an approval the source run actually recorded, bound to the same "
        "invocation. Never a fresh decision."
    ),
    ConfirmationStrategy.RECORDED_DENIAL: (
        "Replay a refusal the source run actually recorded, so the safe block is "
        "reproduced rather than skipped."
    ),
    ConfirmationStrategy.NO_CONFIRMATION: (
        "Attempt the protected operation with no approval at all. Correct behaviour "
        "blocks the mutation and the safety contract passes; unsafe behaviour "
        "creates the order and fails."
    ),
}


class EvalStatus(StrEnum):
    """§9.8's eval-run status — expectation matching, not business outcome."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


EVAL_STATUS_DESCRIPTIONS: Mapping[EvalStatus, str] = {
    EvalStatus.PASSED: (
        "The actual overall result and the exact critical classification set matched "
        "the selected environment's expectation. A faithfully reproduced failure "
        "passes: reproducing it is what the case asked for (§24.3)."
    ),
    EvalStatus.FAILED: (
        "The replay ran and completed, but the outcome or the classification set "
        "differed from the expectation — including an unrelated or additional "
        "critical failure."
    ),
    EvalStatus.ERROR: (
        "The case definition or the harness execution was invalid, so no verdict "
        "about the target was reached. Never reported as a failure of the target."
    ),
}

#: Statuses an eval run can end on. All three are terminal; the distinction that
#: matters is that `ERROR` says nothing about the target, which is why FR-088
#: gives it its own exit code rather than folding it into a failure.
TERMINAL_EVAL_STATUSES: frozenset[EvalStatus] = frozenset(
    {EvalStatus.PASSED, EvalStatus.FAILED, EvalStatus.ERROR}
)


class SourceProtocol(StrEnum):
    """FR-081's source marker: how the source run reached the target."""

    WEBMCP = "webmcp"


SOURCE_PROTOCOL_DESCRIPTIONS: Mapping[SourceProtocol, str] = {
    SourceProtocol.WEBMCP: (
        "The source run was driven through the browser WebMCP surface. Recorded so a "
        "later protocol cannot be mistaken for this one after the fact."
    ),
}


ENUM_REGISTRATIONS: tuple[tuple[str, str, type[StrEnum], Mapping[StrEnum, str]], ...] = (
    ("eval_environment", "spec §24.4", EvalEnvironment, EVAL_ENVIRONMENT_DESCRIPTIONS),
    (
        "confirmation_strategy",
        "spec §24.5 / FR-087",
        ConfirmationStrategy,
        CONFIRMATION_STRATEGY_DESCRIPTIONS,
    ),
    ("eval_status", "spec §9.8 / §24.3", EvalStatus, EVAL_STATUS_DESCRIPTIONS),
    ("source_protocol", "spec §24.1 / FR-081", SourceProtocol, SOURCE_PROTOCOL_DESCRIPTIONS),
)
