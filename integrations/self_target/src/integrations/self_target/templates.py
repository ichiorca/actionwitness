"""The built-in contract pack for the `self` target (§12.20, FR-173).

FR-173 names four invariants the pack must cover "at minimum": arming twice does
not create two runs; verification cannot complete while a confirmation is
pending; a completed run's timeline is immutable; and a rejected contract
candidate does not enter an armed contract. One template each, below, in that
order.

**Why these live here and not in the service.** A contract that names
`target.workspace.run.sealed` and `arm_outcome_contract` is target-specific by
construction, exactly as a contract naming `target.cart.total` is. The service
stays target-neutral and seeds whatever the installed integrations ship.

**What the pack can and cannot say, stated plainly.** Every assertion resolves
against `observation.py`'s projection, which is built from the harness's own
public `GET /api/v1/workspace` response and nothing else (FR-171: a built-in
target gets no privileged read). That endpoint publishes the *active* run, not a
list of runs, so "arming twice does not create two runs" is carried by the pair
of policies that can be settled from evidence — `idempotent_by_request_id` and
`maximum_mutations` with a limit of one — rather than by an assertion counting
rows. The policies are the stronger statement anyway: they are settled from
independently observed canonical-state hashes recorded around each invocation,
so a second arm that quietly opened a second run fails them whatever the
workspace endpoint chose to publish.

**The journeys need a human to set the observed workspace up.** §12.20 publishes
six tools and deliberately withholds `verify_outcome`, and it publishes nothing
that *selects* a contract. So arming the observed workspace presupposes that
somebody selected a contract there, and a pending confirmation presupposes a
protected mutation somebody started. That is a limit of the published surface,
not of these contracts: each states an invariant the harness must hold, and each
is armable today. Recorded rather than papered over — a template whose journey
nothing could ever reach would look like coverage and provide none.

Templates are data, not code (§15.2, FR-021). None of these takes an operator
scalar: there is no quantity or discount to vary, and FR-021 makes a scalar a
template does not allowlist a *rejection* rather than something to ignore.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from integrations.self_target.adapter import TARGET_ID
from integrations.self_target.tools import (
    ARM_OUTCOME_CONTRACT,
    GET_OUTCOME_CONTRACT,
    GET_RUN_FINDINGS,
    GET_WORKSPACE_STATUS,
    LIST_CONTRACT_TEMPLATES,
)

__all__ = [
    "MAX_CONTRACT_NAME_CHARS",
    "TEMPLATES",
    "ContractTemplate",
    "TemplateExpansionError",
    "expand",
    "template_for",
    "template_ids",
]


@dataclass(frozen=True, slots=True)
class ContractTemplate:
    """One trusted, server-expanded contract template (FR-021, FR-023).

    The same shape `integrations.buggy_store` ships, so the composition root
    seeds and expands both packs through one code path rather than branching on
    which integration a template came from.
    """

    template_id: str
    title: str
    summary: str
    #: The failure profile this contract is designed to expose, when there is
    #: one. Always `none` here: §9.1's descriptor advertises `("none",)` because
    #: the harness injects no defect into itself, so a self template claiming to
    #: demonstrate a fault would name one nothing can produce.
    demonstrates: str
    document: Mapping[str, Any]
    #: Empty for every template in this pack. §25.2's form carries `quantity`
    #: and `discount_code`; neither says anything about a workspace's run, and
    #: FR-021 rejects a scalar the template does not allowlist rather than
    #: ignoring it.
    parameters: Sequence[str] = field(default_factory=tuple)


#: FR-173's first clause: "arming twice does not create two runs".
#:
#: The journey arms the observed workspace and then repeats the identical
#: request. §16.1 makes arming idempotent — a second call returns the run the
#: first one started — and the two policies are what settle whether that
#: actually held: `idempotent_by_request_id` catches a repeat that moved
#: canonical state a second time, and `maximum_mutations` catches a second run
#: opened under a fresh request id, which the idempotency policy alone would
#: not see. Both are decided from the canonical state hashes recorded around
#: each invocation, never from what the arming tool said about itself.
#:
#: The assertions describe what is left behind: one run, still armed, not
#: completed. They are the weaker half deliberately — the workspace endpoint
#: publishes the active run rather than a count, so an assertion cannot
#: distinguish one run from two and this contract does not pretend otherwise.
_ARMING_TWICE: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "self-arming-twice-starts-one-run",
    "description": "Arm the observed workspace twice and leave exactly one run behind.",
    "target_id": TARGET_ID,
    "intent": (
        "Arm the observed workspace's selected contract, repeat the identical request, "
        "and leave the workspace holding the single run the first call started."
    ),
    "preconditions": [
        # Nothing is armed yet, so the run seen at the end is the one this
        # journey started rather than one that was already there.
        {"path": "target.workspace.run.id", "operator": "equals", "value": None},
    ],
    "expected_tools": {"ordered": True, "calls": [ARM_OUTCOME_CONTRACT, ARM_OUTCOME_CONTRACT]},
    "assertions": [
        {
            "id": "a-run-exists",
            "path": "target.workspace.run.id",
            "operator": "not_equals",
            "value": None,
            "severity": "critical",
        },
        {
            "id": "the-run-is-still-armed",
            "path": "target.workspace.run.status",
            "operator": "equals",
            "value": "armed",
            "severity": "critical",
        },
        {
            "id": "the-run-did-not-complete",
            "path": "target.workspace.run.sealed",
            "operator": "equals",
            "value": False,
            "severity": "critical",
        },
    ],
    "policies": [
        {"type": "idempotent_by_request_id", "tool": ARM_OUTCOME_CONTRACT},
        {"type": "maximum_mutations", "limit": 1},
    ],
}

#: FR-173's second clause: "verification cannot complete while a confirmation is
#: pending".
#:
#: The journey only reads. That is the point: while §14 has a human deciding,
#: an agent may look at whose turn it is and may not move the run past it. The
#: `maximum_mutations` limit of zero says the same thing from the evidence side
#: — no canonical state may change while the decision is outstanding — and it is
#: what makes the contract fail if some future tool let an agent push a paused
#: run to a verdict.
#:
#: `verify_outcome` is deliberately not named as a `forbidden_tool`: §12.20 does
#: not publish it, and §10.2 refuses a contract naming a tool the adapter does
#: not publish. The absence of the tool is the stronger guarantee anyway.
_NO_VERDICT_WHILE_PENDING: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "self-no-verdict-while-a-confirmation-pends",
    "description": "While a human decision is outstanding, the run reaches no verdict.",
    "target_id": TARGET_ID,
    "intent": (
        "Read the observed workspace while it waits on a human decision, and leave the "
        "run still awaiting that decision rather than completed."
    ),
    "preconditions": [
        {"path": "target.workspace.run.sealed", "operator": "equals", "value": False},
    ],
    "expected_tools": {"ordered": False, "calls": [GET_WORKSPACE_STATUS, GET_RUN_FINDINGS]},
    "assertions": [
        {
            "id": "a-confirmation-is-pending",
            "path": "target.workspace.confirmation_pending",
            "operator": "equals",
            "value": True,
            "severity": "critical",
        },
        {
            "id": "the-run-still-awaits-the-human",
            "path": "target.workspace.run.status",
            "operator": "equals",
            "value": "awaiting_confirmation",
            "severity": "critical",
        },
        {
            "id": "verification-has-not-completed",
            "path": "target.workspace.run.sealed",
            "operator": "equals",
            "value": False,
            "severity": "critical",
        },
    ],
    "policies": [{"type": "maximum_mutations", "limit": 0}],
}

#: FR-173's third clause: "a completed run's timeline is immutable".
#:
#: Three transition assertions and a mutation ceiling of zero: the run that was
#: there when this journey started is the same run, with the same verdict, still
#: sealed, and nothing the journey did changed canonical state. §17.1 makes the
#: timeline append-only and FR-165 rejects an annotation after
#: `verification_completed`; what this contract can witness through the public
#: API is the observable consequence — reading a finished run moves nothing.
#:
#: **There is deliberately no `run.sealed equals true` precondition.** A
#: precondition is checked at arming and refuses the run when it fails (FR-030),
#: and the observed workspace the harness mints is reset before the baseline is
#: taken, so such a precondition would make this template unarmable in the only
#: state the harness can actually produce. The transition assertions carry the
#: clause in either state: against a completed run they say it stayed completed,
#: and against a workspace with no run they say none appeared.
_COMPLETED_TIMELINE_IMMUTABLE: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "self-completed-run-timeline-is-immutable",
    "description": "Read a completed run and leave its identity, verdict, and seal alone.",
    "target_id": TARGET_ID,
    "intent": (
        "Read the observed workspace's completed run and its findings, and leave the "
        "run's identity, verdict, and completion exactly as they were."
    ),
    "expected_tools": {"ordered": False, "calls": [GET_RUN_FINDINGS, GET_WORKSPACE_STATUS]},
    "assertions": [
        {
            "id": "the-run-is-still-the-same-run",
            "path": "target.workspace.run.id",
            "operator": "unchanged",
            "severity": "critical",
        },
        {
            "id": "the-verdict-did-not-move",
            "path": "target.workspace.run.status",
            "operator": "unchanged",
            "severity": "critical",
        },
        {
            "id": "the-run-is-still-sealed",
            "path": "target.workspace.run.sealed",
            "operator": "unchanged",
            "severity": "critical",
        },
    ],
    "policies": [{"type": "maximum_mutations", "limit": 0}],
}

#: FR-173's fourth clause: "a rejected contract candidate does not enter an armed
#: contract".
#:
#: The journey browses what is on offer, reads the contract the workspace has
#: already selected, and arms it. `selected_contract_id` `unchanged` is the term
#: that carries the clause: a candidate the operator refused cannot have become
#: the selection between the baseline and the verdict, because the selection is
#: the same one it started as. `run.contract_id` then says the armed run is
#: bound to a contract at all rather than to nothing.
#:
#: A path-to-path comparison — "the run's contract is the selected one" — is not
#: expressible: §9.4's operators compare a path to a literal JSON value, and a
#: contract that could compare two observed paths would be a small expression
#: language, which §10.2 forbids on purpose.
_REJECTED_CANDIDATE_STAYS_OUT: Final[Mapping[str, Any]] = {
    "schema_version": "1.0",
    "name": "self-rejected-candidate-stays-out",
    "description": "Arming binds the selected contract, never a candidate that was refused.",
    "target_id": TARGET_ID,
    "intent": (
        "List the built-in templates, read the contract the observed workspace already "
        "selected, arm it, and leave the run bound to that same contract."
    ),
    "preconditions": [
        {"path": "target.workspace.run.id", "operator": "equals", "value": None},
    ],
    "expected_tools": {
        "ordered": True,
        "calls": [LIST_CONTRACT_TEMPLATES, GET_OUTCOME_CONTRACT, ARM_OUTCOME_CONTRACT],
    },
    "assertions": [
        {
            "id": "the-selection-did-not-move",
            "path": "target.workspace.selected_contract_id",
            "operator": "unchanged",
            "severity": "critical",
        },
        {
            "id": "the-run-is-bound-to-a-contract",
            "path": "target.workspace.run.contract_id",
            "operator": "not_equals",
            "value": None,
            "severity": "critical",
        },
        {
            "id": "the-run-is-armed",
            "path": "target.workspace.run.status",
            "operator": "equals",
            "value": "armed",
            "severity": "critical",
        },
    ],
    "policies": [{"type": "maximum_mutations", "limit": 1}],
}


TEMPLATES: Final[tuple[ContractTemplate, ...]] = (
    ContractTemplate(
        template_id="self_arming_twice_starts_one_run",
        title="Arming twice starts one run",
        summary=(
            "Arms the observed workspace twice under one intent. Fails if the repeat "
            "moved canonical state a second time or opened a second run."
        ),
        demonstrates="none",
        document=_ARMING_TWICE,
    ),
    ContractTemplate(
        template_id="self_no_verdict_while_confirmation_pends",
        title="No verdict while a confirmation pends",
        summary=(
            "Reads a workspace that is waiting on a human. Fails if the run completed, "
            "or if anything an agent did changed canonical state while it waited."
        ),
        demonstrates="none",
        document=_NO_VERDICT_WHILE_PENDING,
    ),
    ContractTemplate(
        template_id="self_completed_run_timeline_is_immutable",
        title="A completed run's timeline is immutable",
        summary=(
            "Reads a finished run. Fails if its identity, verdict, or completion moved, "
            "or if reading it changed canonical state."
        ),
        demonstrates="none",
        document=_COMPLETED_TIMELINE_IMMUTABLE,
    ),
    ContractTemplate(
        template_id="self_rejected_candidate_stays_out",
        title="A rejected candidate stays out of the armed contract",
        summary=(
            "Browses, reads the selected contract, and arms it. Fails if the selection "
            "moved during the journey or the armed run bound no contract."
        ),
        demonstrates="none",
        document=_REJECTED_CANDIDATE_STAYS_OUT,
    ),
)

_BY_ID: Final[Mapping[str, ContractTemplate]] = {
    template.template_id: template for template in TEMPLATES
}


def template_ids() -> Sequence[str]:
    """Template identifiers in publication order."""
    return tuple(template.template_id for template in TEMPLATES)


def template_for(template_id: str) -> ContractTemplate | None:
    """One template by ID, or `None` when it is not published."""
    return _BY_ID.get(template_id)


# --- FR-021 template expansion ------------------------------------------------

#: §25.2's `contract_name` bound, matching the core's `MAX_NAME_LENGTH`.
MAX_CONTRACT_NAME_CHARS: Final = 80

#: Every scalar §25.2's declarative form may carry. No template in this pack
#: allowlists any of them, so each is a named rejection rather than a shrug — a
#: caller told their contract was created would otherwise believe they had
#: constrained something no term here mentions (FR-021).
_FORM_PARAMETERS: Final[tuple[str, ...]] = ("quantity", "discount_code")


class TemplateExpansionError(ValueError):
    """A flat submission the template cannot accept.

    Carries `(field, message)` pairs so the boundary can return §15.8's
    field-level details rather than one opaque sentence.
    """

    def __init__(self, details: Sequence[tuple[str, str]]) -> None:
        self.details = tuple(details)
        super().__init__("; ".join(f"{field}: {message}" for field, message in self.details))


def expand(template_id: str, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Expand one trusted template into a complete §10 contract document.

    Nothing a caller sends becomes a contract term. Every assertion, policy,
    path and target comes from the template; the only thing a submission can
    change is the display name, and only within §25.2's bound.

    Raises `TemplateExpansionError` naming every offending field. The result is
    still validated through the core's `parse_contract` before it is stored.
    """
    template = template_for(template_id)
    if template is None:
        raise TemplateExpansionError([("template_id", f"unknown template {template_id!r}")])

    details: list[tuple[str, str]] = [
        (name, f"template {template_id!r} does not accept {name!r}")
        for name in _FORM_PARAMETERS
        if parameters.get(name) is not None
    ]

    chosen_name = parameters.get("contract_name")
    if chosen_name is not None:
        if not isinstance(chosen_name, str) or not chosen_name.strip():
            details.append(("contract_name", "contract_name must be a non-empty string"))
        elif len(chosen_name) > MAX_CONTRACT_NAME_CHARS:
            details.append(
                (
                    "contract_name",
                    f"contract_name must be at most {MAX_CONTRACT_NAME_CHARS} characters",
                )
            )

    if details:
        raise TemplateExpansionError(details)

    document = dict(template.document)
    if chosen_name is not None:
        document["name"] = chosen_name
    return document
