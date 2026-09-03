"""Server-derived guidance (FR-120, FR-121, FR-122, §12.13, §15.1).

FR-120: "Every nonterminal workspace state shall produce one `GuidanceState`
containing `phase`, `active_actor`, short headline, instruction, reason,
expected consequence, primary action, optional recovery action, and correlation
ID. FastAPI derives this object from authoritative workspace/run state; **the
frontend shall not invent a conflicting next action.**"

§26.1 locked decision: "Guidance state and `next_action` shall be derived from
the same server lifecycle state for the UI, WebMCP responses, and audit trail."

**One derivation, three surfaces.** That sentence is the whole reason this
module is a pure function in the core rather than three renderers in three
places. The banner a person reads, the `next_action` a tool returns, and the
`guidance_events` row an auditor replays are the same object serialized three
ways. Two implementations would agree in testing and diverge in the exact
situation guidance exists for — the one where a person and an agent disagree
about whose turn it is.

**Copy is data, and its version is recorded.** §12.13: "Display-copy changes may
alter future messages but never rewrite historical actor, action code,
correlation, or outcome evidence." So the stable thing is the
`GuidanceActionCode`, the changeable thing is the sentence, and `COPY_VERSION`
is what lets a reader of an old guidance event know which sentence was shown.

**Copy never implies an agent can decide.** FR-122: "Copy shall not imply that
an agent can make a human decision", and §15.1: "Guidance never authorizes a
protected mutation." The `human_approver` actor is reserved to a person, and a
test in the guidance lane asserts that no instruction addressed to the agent
mentions approving.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Final

from pydantic import Field, StringConstraints

from actionwitness_core.journeys.enums import (
    GuidanceActionCode,
    GuidanceActor,
    RunState,
    WorkspacePhase,
)
from actionwitness_core.kernel import CoreModel

__all__ = [
    "COPY_VERSION",
    "GuidanceState",
    "derive_guidance",
    "phase_for",
]

#: Bumped whenever a sentence below changes. Stored on every guidance event so a
#: historical row can be read against the copy that was actually displayed.
COPY_VERSION: Final = "1.0.0"

type _Text = Annotated[str, StringConstraints(min_length=1, max_length=400)]


class GuidanceState(CoreModel):
    """FR-120's object. One per nonterminal workspace state.

    `action_code` is `None` only when no safe action exists — §15.1: "If no safe
    action is possible, the primary action is omitted and the recovery
    instruction explains why." A caller that needs *something* to render should
    read `headline` and `reason`, which are always present.
    """

    phase: WorkspacePhase
    active_actor: GuidanceActor
    next_actor: GuidanceActor | None = None
    headline: _Text
    instruction: _Text
    reason: _Text
    expected_consequence: _Text
    action_code: GuidanceActionCode | None = None
    #: A safe way out when this phase stalls, or `None` where nothing is stalled
    #: to begin with — a reached verdict, or a phase with no run in flight.
    #:
    #: There is deliberately no `recovery_instruction` beside it. §12.13 makes
    #: the code the stable thing and the sentence the changeable one, and the
    #: sentence for every code already exists once, in
    #: `GUIDANCE_ACTION_DESCRIPTIONS`, which the generated registry publishes to
    #: the frontend. A second copy field here would be the same sentence stored
    #: twice with no rule about which one a reader believes.
    recovery_action_code: GuidanceActionCode | None = None
    waiting_for: _Text | None = None
    #: FR-121's "whether human input is required", so a tool result can say so
    #: without the agent having to infer it from the actor.
    requires_human_input: bool = False
    correlation_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None

    def next_action(self) -> Mapping[str, object]:
        """FR-121's compact projection: actor, action code, instruction, and
        whether human input is required.

        Deliberately *this* object narrowed rather than a second derivation, so
        a tool result and the banner cannot disagree.
        """
        return {
            "actor": str(self.active_actor.value),
            "action_code": None if self.action_code is None else str(self.action_code.value),
            "instruction": self.instruction,
            "requires_human_input": self.requires_human_input,
        }


def phase_for(
    *,
    has_contract: bool,
    run_state: RunState | None,
    regression_case_ready: bool = False,
    regression_replay_open: bool = False,
) -> WorkspacePhase:
    """Project authoritative workspace and run state onto one phase (§11.5).

    A run's own state wins when there is one: a workspace holding a contract and
    a `running` run is in `running`, not `contract_ready`. Without a run, the
    phase is decided by whether a contract has been selected — which is the
    fork §11.5's diagram opens with.

    The two regression flags carry the only lifecycle §11.5 draws that a run
    state cannot express. Its edges are `PassedWarnings --> EvalReady: eval
    created`, `Failed --> EvalReady: eval created`, `EvalReady --> EvalRunning:
    replay started`, and `EvalRunning --> EvalReady: replay completed` — every
    one of them leaves the source run's own state untouched, so a projection
    reading `RunState` alone can never reach either phase. It could not before:
    `eval_ready` and `eval_running` had guidance nobody could ever be shown, and
    the server could not emit `run_regression_eval` however many cases a
    workspace held.

    They are consulted only for the two verdict states §11.5 draws the "eval
    created" edge from. §15.4 generates a case "from a failed or warning-bearing
    run", so a `passed` run with the flags set is still `passed` — offering a
    replay there would name a case that cannot exist.
    """
    if run_state is not None:
        if run_state in _EVAL_CAPABLE_RUN_STATES:
            if regression_replay_open:
                return WorkspacePhase.EVAL_RUNNING
            if regression_case_ready:
                return WorkspacePhase.EVAL_READY
        return _PHASE_BY_RUN_STATE[run_state]
    return WorkspacePhase.CONTRACT_READY if has_contract else WorkspacePhase.NO_CONTRACT


def derive_guidance(phase: WorkspacePhase, *, correlation_id: str | None = None) -> GuidanceState:
    """The single derivation. Pure, total over `WorkspacePhase`, and injectable.

    Total on purpose: a phase with no entry would produce an empty banner, and an
    empty banner is worse than a wrong one because nobody can tell it is empty
    by looking. The table below has one row per member, and a test asserts that.
    """
    template = _GUIDANCE[phase]
    return GuidanceState(**template, correlation_id=correlation_id)


#: §11.5's phases, mapped from the run state that produces each.
_PHASE_BY_RUN_STATE: Final[Mapping[RunState, WorkspacePhase]] = {
    RunState.ARMED: WorkspacePhase.ARMED,
    RunState.PROPOSING: WorkspacePhase.PROPOSING,
    RunState.CAPTURING: WorkspacePhase.PROPOSING,
    RunState.PROPOSED: WorkspacePhase.CANDIDATES,
    RunState.RUNNING: WorkspacePhase.RUNNING,
    RunState.AWAITING_CONFIRMATION: WorkspacePhase.AWAITING_CONFIRMATION,
    RunState.VERIFYING: WorkspacePhase.VERIFYING,
    RunState.PASSED: WorkspacePhase.PASSED,
    RunState.PASSED_WITH_WARNINGS: WorkspacePhase.PASSED_WITH_WARNINGS,
    RunState.FAILED: WorkspacePhase.FAILED,
    #: Each of these keeps its own phase rather than collapsing onto
    #: `contract_ready`. Both *do* leave the contract selected (FR-013), so the
    #: old mapping picked the right next action — and then said nothing else, so
    #: a run the harness had abandoned mid-verification was greeted with "Arm
    #: the run." and no acknowledgement that anything had happened. §22 is
    #: explicit that an observation failure "produces an explicit non-pass
    #: result; it never degrades to success", and a banner that cannot tell the
    #: operator a run ended without a verdict is that degradation in the one
    #: place a person actually reads.
    RunState.ERROR: WorkspacePhase.ERROR,
    RunState.CANCELLED: WorkspacePhase.CANCELLED,
}

#: The verdict states §11.5 draws an "eval created" edge from, and §15.4 permits
#: generating a case from: "a failed or warning-bearing run".
_EVAL_CAPABLE_RUN_STATES: Final[frozenset[RunState]] = frozenset(
    {RunState.FAILED, RunState.PASSED_WITH_WARNINGS}
)


_GUIDANCE: Final[Mapping[WorkspacePhase, Mapping[str, object]]] = {
    WorkspacePhase.NO_CONTRACT: {
        "phase": WorkspacePhase.NO_CONTRACT,
        "active_actor": GuidanceActor.OPERATOR,
        "next_actor": GuidanceActor.AGENT,
        "headline": "Choose what this run should prove.",
        "instruction": "Select one of the built-in contracts to judge the next run against.",
        "reason": "A run needs a contract; without one there is nothing to compare against.",
        "expected_consequence": (
            "The contract and its target become the workspace's selection, and the run can "
            "be armed."
        ),
        "action_code": GuidanceActionCode.SELECT_CONTRACT,
        # No recovery: nothing is in flight to recover *from*. Reset is legal
        # here (FR-013 makes it legal everywhere) and would do nothing a person
        # could observe, and a recovery that changes nothing is worse than none
        # — it invites someone stuck at a real problem to click it and conclude
        # the harness is broken.
        "requires_human_input": True,
    },
    WorkspacePhase.CONTRACT_READY: {
        "phase": WorkspacePhase.CONTRACT_READY,
        "active_actor": GuidanceActor.OPERATOR,
        "next_actor": GuidanceActor.AGENT,
        "headline": "Arm the run.",
        "instruction": "Arm the selected contract to capture the target's starting state.",
        "reason": (
            "Arming records the configuration and one authoritative observation, so the "
            "outcome can later be compared against a baseline nobody edited."
        ),
        "expected_consequence": (
            "A run is created in `armed`, its initial snapshot is stored, and the agent may "
            "begin using target tools."
        ),
        "action_code": GuidanceActionCode.ARM_RUN,
        # No recovery, for the same reason `no_contract` has none: no run exists
        # yet, so there is nothing stalled that a reset would release.
        "requires_human_input": True,
    },
    WorkspacePhase.ARMED: {
        "phase": WorkspacePhase.ARMED,
        "active_actor": GuidanceActor.AGENT,
        "next_actor": GuidanceActor.AGENT,
        "headline": "The run is armed. Work the task.",
        "instruction": "Use the target's tools to carry out the task the contract describes.",
        "reason": (
            "The starting state is recorded, so every change from here on is attributable "
            "to an action on the timeline."
        ),
        "expected_consequence": (
            "Each tool call is recorded with what it reported and what was independently "
            "observed immediately afterwards."
        ),
        "action_code": GuidanceActionCode.INVOKE_TARGET_TOOL,
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
    },
    WorkspacePhase.RUNNING: {
        "phase": WorkspacePhase.RUNNING,
        "active_actor": GuidanceActor.AGENT,
        "next_actor": GuidanceActor.SYSTEM,
        "headline": "Continue, then verify when the task is done.",
        "instruction": (
            "Continue with the target's tools, and verify the outcome when the task is complete."
        ),
        "reason": (
            "Verification is what turns recorded actions into a judged outcome; until it "
            "runs, nothing has been decided."
        ),
        "expected_consequence": (
            "Verification captures final state, evaluates the contract, and produces a "
            "report that stands on independent observation."
        ),
        "action_code": GuidanceActionCode.VERIFY_OUTCOME,
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
    },
    WorkspacePhase.AWAITING_CONFIRMATION: {
        "phase": WorkspacePhase.AWAITING_CONFIRMATION,
        "active_actor": GuidanceActor.HUMAN_APPROVER,
        "next_actor": GuidanceActor.AGENT,
        "headline": "A protected action needs a person's decision.",
        "instruction": "Review the pending action and choose Approve once or Deny.",
        "reason": (
            "This action changes state that cannot be undone by retrying, so it proceeds "
            "only on an explicit decision made by a person."
        ),
        "expected_consequence": (
            "Approving lets this one invocation proceed; denying stops it. Either way the "
            "decision and its outcome are recorded."
        ),
        "action_code": GuidanceActionCode.DECIDE_CONFIRMATION,
        # The one phase where cancelling is a real capability, and the only one
        # that may name it. `DELETE /runs/{run_id}/confirmations/{id}` reaches
        # `Decision.CANCEL`, which refuses any request that is not `pending` —
        # and a pending request exists in no other phase. §14.9 keeps this
        # distinct from denial: a person who cannot decide (the agent's tab
        # closed, the consequence is unreadable) previously had only a reset,
        # which throws the run's evidence away to escape one stuck request.
        "recovery_action_code": GuidanceActionCode.CANCEL_CONFIRMATION,
        "waiting_for": "the agent is waiting for this decision before it can continue",
        "requires_human_input": True,
    },
    WorkspacePhase.VERIFYING: {
        "phase": WorkspacePhase.VERIFYING,
        "active_actor": GuidanceActor.SYSTEM,
        "next_actor": GuidanceActor.OPERATOR,
        "headline": "Verifying the outcome.",
        "instruction": "No action is needed while verification completes.",
        "reason": (
            "Final state is being captured and evaluated; new target actions are refused so "
            "the snapshot describes one moment."
        ),
        "expected_consequence": "A report and its findings become available.",
        "action_code": GuidanceActionCode.WAIT,
        # §11.5 draws `Verifying --> ContractReady: reset`, so this is a named
        # exit rather than an inferred one. It matters here more than in most
        # phases: verification refuses new target actions, so a capture that
        # never returns leaves a workspace with no legal move and — until this
        # code was set — nothing on screen saying what to do about it.
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
        "waiting_for": "the server is capturing final state and evaluating the contract",
    },
    WorkspacePhase.PASSED: {
        "phase": WorkspacePhase.PASSED,
        "active_actor": GuidanceActor.OPERATOR,
        "headline": "The outcome passed.",
        "instruction": "Read the report to see what was observed and how it was judged.",
        "reason": "Every assertion held against independently observed state.",
        "expected_consequence": "The run is terminal; its evidence is retained.",
        "action_code": GuidanceActionCode.REVIEW_FINDINGS,
        # No recovery, and this is the deliberate null the banner renders as an
        # absence. The banner labels recovery "If this stalls" — a run that
        # reached a verdict did not stall, it finished. Naming a reset here
        # would invent a problem to solve, and §15.1 reserves the recovery slot
        # for the case where something is genuinely stuck.
    },
    WorkspacePhase.PASSED_WITH_WARNINGS: {
        "phase": WorkspacePhase.PASSED_WITH_WARNINGS,
        "active_actor": GuidanceActor.OPERATOR,
        "headline": "The outcome passed, with warnings.",
        "instruction": "Read the findings to see which non-critical checks did not hold.",
        "reason": "No critical assertion failed, but at least one warning was recorded.",
        "expected_consequence": (
            "The run is terminal; a regression eval can be generated from it."
        ),
        "action_code": GuidanceActionCode.REVIEW_FINDINGS,
        # No recovery: a warning-bearing verdict is a finished run, not a stall.
    },
    WorkspacePhase.FAILED: {
        "phase": WorkspacePhase.FAILED,
        "active_actor": GuidanceActor.OPERATOR,
        "headline": "The outcome failed.",
        "instruction": "Read the findings to see which check failed and what was observed.",
        "reason": (
            "At least one critical assertion did not hold against independently observed state."
        ),
        "expected_consequence": (
            "The run is terminal; a regression eval can be generated to reproduce it."
        ),
        "action_code": GuidanceActionCode.REVIEW_FINDINGS,
        # No recovery: a failed verdict is the harness working, not stalling.
        # This is the outcome the product exists to produce.
    },
    WorkspacePhase.ERROR: {
        "phase": WorkspacePhase.ERROR,
        "active_actor": GuidanceActor.OPERATOR,
        "headline": "The run stopped without a verdict.",
        "instruction": "Read the report to see why verification could not be completed.",
        "reason": (
            "The harness could not finish — a required observation was unavailable, or a "
            "resource ceiling was reached — so no assertion was judged and nothing here "
            "is a pass."
        ),
        "expected_consequence": (
            "The run is terminal and its evidence is kept. Resetting clears it, keeps the "
            "selected contract, and reseeds the target so the next run starts from a known "
            "state."
        ),
        # §16 makes `error` a verdict-bearing terminal state and §23.1 writes a
        # report for it, so there is something real to read; `_abandon_unobservable`
        # records the `observation_unavailable` finding that says which.
        "action_code": GuidanceActionCode.REVIEW_FINDINGS,
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
    },
    WorkspacePhase.CANCELLED: {
        "phase": WorkspacePhase.CANCELLED,
        "active_actor": GuidanceActor.OPERATOR,
        "next_actor": GuidanceActor.AGENT,
        "headline": "The run was cancelled before it was verified.",
        "instruction": "Arm a new run when you are ready; the selected contract was kept.",
        "reason": (
            "A reset cancelled the run in flight, so no verdict was produced and its "
            "partial evidence stands as a record rather than a result."
        ),
        "expected_consequence": (
            "A new run is created in `armed` with a fresh initial observation, and the "
            "cancelled run stays on record."
        ),
        "action_code": GuidanceActionCode.ARM_RUN,
        # Reset again is the genuine second option: it is what reseeds the
        # target, so an operator unsure what the cancelled run left behind has a
        # way to start from a known state rather than arming on top of it.
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
        "requires_human_input": True,
    },
    WorkspacePhase.PROPOSING: {
        "phase": WorkspacePhase.PROPOSING,
        "active_actor": GuidanceActor.AGENT,
        "next_actor": GuidanceActor.OPERATOR,
        "headline": "Working the task so assertions can be proposed.",
        "instruction": "Use the target's tools to demonstrate the behaviour to be captured.",
        "reason": "A proposal run records a state delta; no contract is being judged.",
        "expected_consequence": "Candidate assertions are derived for a person to curate.",
        "action_code": GuidanceActionCode.INVOKE_TARGET_TOOL,
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
    },
    WorkspacePhase.CANDIDATES: {
        "phase": WorkspacePhase.CANDIDATES,
        "active_actor": GuidanceActor.OPERATOR,
        "headline": "Curate the proposed assertions.",
        "instruction": "Accept or reject each candidate before it becomes a contract.",
        "reason": (
            "Candidates are derived mechanically; a person decides which of them state "
            "the intended outcome."
        ),
        "expected_consequence": "The accepted set becomes one immutable contract.",
        "action_code": GuidanceActionCode.CURATE_CANDIDATES,
        # The proposal run is terminal but the workspace is not: nothing moves
        # until a person curates, and a candidate set nobody wants would
        # otherwise be a dead end with no named way out. Reset returns the
        # workspace to ready (FR-013) and keeps whatever contract was selected.
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
        "requires_human_input": True,
    },
    WorkspacePhase.EVAL_READY: {
        "phase": WorkspacePhase.EVAL_READY,
        "active_actor": GuidanceActor.OPERATOR,
        "headline": "A regression case is ready to replay.",
        "instruction": "Replay the generated regression case to confirm it reproduces.",
        "reason": "A regression case is only useful once it has reproduced its source failure.",
        "expected_consequence": "The replay reports whether the original classification recurred.",
        "action_code": GuidanceActionCode.RUN_REGRESSION_EVAL,
        # §11.5 draws `EvalReady --> ContractReady: reset`.
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
    },
    WorkspacePhase.EVAL_RUNNING: {
        "phase": WorkspacePhase.EVAL_RUNNING,
        "active_actor": GuidanceActor.SYSTEM,
        "next_actor": GuidanceActor.OPERATOR,
        "headline": "Replaying the regression case.",
        "instruction": "No action is needed while the replay completes.",
        "reason": "The fixture is being restored and the recorded trajectory replayed.",
        "expected_consequence": "The replay's classification is compared against the source run.",
        "action_code": GuidanceActionCode.WAIT,
        # §11.5 draws `EvalRunning --> ContractReady: reset`, and this phase is
        # entered on an *open* eval-run row rather than a heartbeat: the row is
        # opened as `error` with no `completed_at` so a process that dies
        # mid-replay leaves an honest record (`EvalRunService._open`). The
        # consequence is that a dead replay keeps saying "replaying", and the
        # named reset is what stops a person waiting on it forever.
        "recovery_action_code": GuidanceActionCode.RESET_WORKSPACE,
        "waiting_for": "the server is replaying the recorded trajectory",
    },
}
