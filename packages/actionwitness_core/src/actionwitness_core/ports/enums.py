"""Closed vocabulary for the public target-adapter surface.

Spec v1.9 §9.1 (`TargetDescriptor.execution_mode`; `TargetToolSpec` carries "the
allowlisted tool name, input schema, side-effect class, and retry semantics"),
§9.5 (protected mutation, qualifying state-changing completion), §13.4 (the
declared target-effect map, whose read-only rows this vocabulary names),
FR-032 and FR-063 (retry and idempotency semantics).

Scenario mode is deliberately *not* here. §9.1 makes it an adapter-declared
opaque token that the core validates against the descriptor's supported list
without interpreting; enumerating `pre_fix` and `post_fix` in the core would put
a demo's vocabulary in a target-neutral library.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

__all__ = [
    "ENUM_REGISTRATIONS",
    "MUTATING_SIDE_EFFECTS",
    "ExecutionMode",
    "RetrySemantics",
    "SideEffectClass",
]


class ExecutionMode(StrEnum):
    """How the harness reaches a target (spec §9.1)."""

    MANAGED = "managed"
    EXTERNAL_WEBMCP = "external_webmcp"


EXECUTION_MODE_DESCRIPTIONS: Mapping[ExecutionMode, str] = {
    ExecutionMode.MANAGED: (
        "The harness drives and can restore the target through its own integration, "
        "so fixtures replay through the same adapter."
    ),
    ExecutionMode.EXTERNAL_WEBMCP: (
        "The browser-owned target executes its own tools; the adapter observes them "
        "and never impersonates them through a second implementation."
    ),
}


class SideEffectClass(StrEnum):
    """What a declared target tool does to canonical state (spec §9.1).

    The three values are the distinctions the engine actually acts on: §13.4
    marks reads as "none; read-only", §9.5's `maximum_mutations` counts
    "qualifying state-changing completions", and §9.5's `requires_confirmation`
    governs "a successful protected mutation". A finer taxonomy would carry no
    behaviour, and a coarser one would lose the protected/ordinary distinction
    that consent depends on.
    """

    READ_ONLY = "read_only"
    MUTATING = "mutating"
    PROTECTED_MUTATING = "protected_mutating"


SIDE_EFFECT_CLASS_DESCRIPTIONS: Mapping[SideEffectClass, str] = {
    SideEffectClass.READ_ONLY: "Declares no target-effect path; cannot change canonical state.",
    SideEffectClass.MUTATING: "May change canonical state under its declared effect paths.",
    SideEffectClass.PROTECTED_MUTATING: (
        "May change canonical state and requires a bound human confirmation first."
    ),
}

#: Side-effect classes that count as a mutation for policy purposes (§9.5).
MUTATING_SIDE_EFFECTS: frozenset[SideEffectClass] = frozenset(
    {SideEffectClass.MUTATING, SideEffectClass.PROTECTED_MUTATING}
)


class RetrySemantics(StrEnum):
    """Whether repeating a call is safe, and on what basis (spec §9.1).

    `not_retryable` is the default an adapter should declare when it is unsure.
    Constitution §5: an ambiguous outcome is never automatically retried, because
    a retry that is not idempotent duplicates the mutation it was meant to repair.

    `naturally_idempotent` exists because Appendix D.2's `apply_discount` fits
    none of the other three: it mutates, so it is not read-only safe; it carries
    no request ID, so it is not idempotent *by* one; and calling it twice cannot
    duplicate anything, so declaring it not-retryable would publish a false
    statement about the target and make every caller more conservative than the
    tool requires. Added when the Buggy Store adapter needed it, which
    `specs/003-buggy-store-target/plan.md` names as the signal that the ports
    were underspecified rather than that the target was unusual.
    """

    READ_ONLY_SAFE = "read_only_safe"
    IDEMPOTENT_BY_REQUEST_ID = "idempotent_by_request_id"
    NATURALLY_IDEMPOTENT = "naturally_idempotent"
    NOT_RETRYABLE = "not_retryable"


RETRY_SEMANTICS_DESCRIPTIONS: Mapping[RetrySemantics, str] = {
    RetrySemantics.READ_ONLY_SAFE: "Changes nothing, so repetition is always safe.",
    RetrySemantics.IDEMPOTENT_BY_REQUEST_ID: (
        "Repeating the same request ID with identical intent returns the first "
        "persisted result; reuse with changed intent is a conflict, not a retry."
    ),
    RetrySemantics.NATURALLY_IDEMPOTENT: (
        "Changes state, but repeating the same call cannot change it a second time, "
        "so it needs no request ID. Appendix D.2's apply_discount is the worked "
        "example: reapplying the active code is a successful no-op reporting "
        "already_applied."
    ),
    RetrySemantics.NOT_RETRYABLE: (
        "Repetition may duplicate the mutation, so an ambiguous outcome is surfaced "
        "rather than retried."
    ),
}


ENUM_REGISTRATIONS: tuple[tuple[str, str, type[StrEnum], Mapping[StrEnum, str]], ...] = (
    ("execution_mode", "spec §9.1", ExecutionMode, EXECUTION_MODE_DESCRIPTIONS),
    ("side_effect_class", "spec §9.1 / §13.4", SideEffectClass, SIDE_EFFECT_CLASS_DESCRIPTIONS),
    ("retry_semantics", "spec §9.1 / FR-063", RetrySemantics, RETRY_SEMANTICS_DESCRIPTIONS),
)
