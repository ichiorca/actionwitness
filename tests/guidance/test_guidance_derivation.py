"""005-T2 — the server-derived guidance projection (FR-120, FR-121, FR-122).

§26.1's locked decision: "Guidance state and `next_action` shall be derived from
the same server lifecycle state for the UI, WebMCP responses, and audit trail."
So the assertions that matter are about *singularity* — one derivation, three
serializations — and about the copy rules §12.13 fixes.

The copy tests are not style checks. FR-122 says "copy shall not imply that an
agent can make a human decision", and the failure mode is a sentence like
"approve the pending action" addressed to the agent. That is a real product
defect, and it is invisible to every functional test.
"""

from __future__ import annotations

import pytest
from actionwitness_core.journeys.enums import (
    GUIDANCE_ACTION_DESCRIPTIONS,
    GuidanceActionCode,
    GuidanceActor,
    RunState,
    WorkspacePhase,
)
from actionwitness_core.journeys.guidance import (
    COPY_VERSION,
    GuidanceState,
    derive_guidance,
    phase_for,
)

pytestmark = [pytest.mark.guidance]


# --- totality ---------------------------------------------------------------


@pytest.mark.parametrize("phase", list(WorkspacePhase))
def test_every_phase_derives_complete_guidance(phase: WorkspacePhase) -> None:
    """FR-120: *every* nonterminal workspace state produces one `GuidanceState`.

    A phase with no entry would render an empty banner, which is worse than a
    wrong one because nobody can tell it is empty by looking.
    """
    # Arrange / Act
    guidance = derive_guidance(phase)

    # Assert — the four sentences FR-120 requires are all present and non-empty.
    assert guidance.phase is phase
    assert guidance.headline
    assert guidance.instruction
    assert guidance.reason
    assert guidance.expected_consequence


@pytest.mark.parametrize("run_state", list(RunState))
def test_every_run_state_projects_onto_a_phase(run_state: RunState) -> None:
    """A run state with no phase would leave a live run with no guidance."""
    # Arrange / Act
    phase = phase_for(has_contract=True, run_state=run_state)

    # Assert
    assert isinstance(phase, WorkspacePhase)


def test_a_workspace_without_a_contract_is_asked_for_one() -> None:
    # Arrange / Act
    guidance = derive_guidance(phase_for(has_contract=False, run_state=None))

    # Assert
    assert guidance.phase is WorkspacePhase.NO_CONTRACT
    assert guidance.action_code is GuidanceActionCode.SELECT_CONTRACT


def test_a_contract_without_a_run_is_asked_to_arm() -> None:
    # Arrange / Act
    guidance = derive_guidance(phase_for(has_contract=True, run_state=None))

    # Assert
    assert guidance.phase is WorkspacePhase.CONTRACT_READY
    assert guidance.action_code is GuidanceActionCode.ARM_RUN


def test_a_live_run_state_wins_over_the_contract_selection() -> None:
    """A workspace holding a contract *and* a running run is running."""
    # Arrange / Act / Assert
    assert phase_for(has_contract=True, run_state=RunState.RUNNING) is WorkspacePhase.RUNNING


@pytest.mark.parametrize(
    ("run_state", "expected"),
    [
        (RunState.ERROR, WorkspacePhase.ERROR),
        (RunState.CANCELLED, WorkspacePhase.CANCELLED),
    ],
)
def test_a_run_that_ended_without_a_verdict_keeps_its_own_phase(
    run_state: RunState, expected: WorkspacePhase
) -> None:
    """**This test's premise was changed deliberately.**

    It used to assert that both states project onto `contract_ready`, on the
    reasoning that reset retains the selected contract (FR-013) so there is
    nothing left to choose. The *next action* part of that reasoning is still
    right — and both new phases still ask for a run to be armed or for the
    report to be read, not for a contract.

    What was wrong was treating "the next action is the same" as "the state is
    the same". A run the harness abandoned mid-verification produced a banner
    reading "Arm the run.", with no headline, reason, or consequence
    acknowledging that anything had gone wrong. §22 requires an observation
    failure to produce "an explicit non-pass result" that "never degrades to
    success", and the surface a person actually reads was quietly degrading it
    to a fresh start. Collapsing the two states also made them indistinguishable
    from each other, so "the harness broke" and "you cancelled this" were shown
    the same words.

    §11.5 supports the split: its diagram omits both nodes for readability but
    states outright that they exist — "Reset remains available from every state
    under FR-013 and Section 16, including `error` and `cancelled`."
    """
    # Arrange / Act / Assert
    assert phase_for(has_contract=True, run_state=run_state) is expected


@pytest.mark.parametrize("phase", [WorkspacePhase.ERROR, WorkspacePhase.CANCELLED])
def test_a_run_that_ended_without_a_verdict_says_so_in_words(
    phase: WorkspacePhase,
) -> None:
    """The point of the split is the copy, so the copy is what is asserted.

    A headline that could be reused for a healthy workspace would leave the
    split doing nothing a reader can see.
    """
    # Arrange / Act
    guidance = derive_guidance(phase)

    # Assert — the sentences name the non-outcome rather than the next chore.
    assert guidance.headline != derive_guidance(WorkspacePhase.CONTRACT_READY).headline
    text = f"{guidance.headline} {guidance.reason}".lower()
    assert "verdict" in text
    # And a way out is named: both phases are recoverable by reset (FR-013).
    assert guidance.recovery_action_code is GuidanceActionCode.RESET_WORKSPACE


def test_an_errored_run_is_never_described_as_a_pass() -> None:
    """§22: an observation failure "produces an explicit non-pass result; it
    never degrades to success"."""
    # Arrange / Act
    guidance = derive_guidance(WorkspacePhase.ERROR)

    # Assert
    text = f"{guidance.headline} {guidance.instruction} {guidance.expected_consequence}".lower()
    assert "passed" not in text
    assert "nothing here is a pass" in guidance.reason.lower()


# --- one derivation, three surfaces -----------------------------------------


@pytest.mark.parametrize("phase", list(WorkspacePhase))
def test_the_compact_next_action_is_this_object_narrowed(phase: WorkspacePhase) -> None:
    """FR-121's `next_action` must agree with the banner, so it is a projection
    of the same object rather than a second derivation."""
    # Arrange
    guidance = derive_guidance(phase)

    # Act
    compact = guidance.next_action()

    # Assert
    assert compact["actor"] == str(guidance.active_actor.value)
    assert compact["instruction"] == guidance.instruction
    assert compact["requires_human_input"] is guidance.requires_human_input
    expected = None if guidance.action_code is None else str(guidance.action_code.value)
    assert compact["action_code"] == expected


def test_the_next_action_carries_exactly_what_fr_121_names() -> None:
    """ "actor, action code, instruction, and whether human input is required."

    Asserted as an exact key set: an extra field here becomes a field some
    surface starts depending on, and the compact form exists to be small.
    """
    # Arrange / Act
    compact = derive_guidance(WorkspacePhase.ARMED).next_action()

    # Assert
    assert set(compact) == {"actor", "action_code", "instruction", "requires_human_input"}


def test_guidance_is_immutable() -> None:
    """It is evidence once recorded; a caller must not be able to edit it."""
    # Arrange
    guidance = derive_guidance(WorkspacePhase.ARMED)

    # Act / Assert
    with pytest.raises(Exception):  # noqa: B017 - pydantic's frozen error
        guidance.headline = "something else"  # type: ignore[misc]


def test_deriving_the_same_phase_twice_gives_the_same_guidance() -> None:
    """Determinism, so a replay produces the guidance the original run showed."""
    # Arrange / Act
    first = derive_guidance(WorkspacePhase.RUNNING, correlation_id="run_1")
    second = derive_guidance(WorkspacePhase.RUNNING, correlation_id="run_1")

    # Assert
    assert first == second


# --- the copy rules §12.13 fixes --------------------------------------------


def test_only_a_human_approver_is_ever_asked_to_decide_a_confirmation() -> None:
    """FR-122: copy must not imply an agent can make a human decision.

    §26.1 locked decision 32 reserves confirmation decisions to a person, and the
    way that promise breaks is a sentence, not a type error.
    """
    # Arrange / Act
    deciding = [
        phase
        for phase in WorkspacePhase
        if derive_guidance(phase).action_code is GuidanceActionCode.DECIDE_CONFIRMATION
    ]

    # Assert
    assert deciding
    for phase in deciding:
        assert derive_guidance(phase).active_actor is GuidanceActor.HUMAN_APPROVER


def test_no_instruction_addressed_to_the_agent_asks_it_to_approve() -> None:
    """The concrete failure FR-122 describes, checked against the actual copy."""
    # Arrange
    forbidden = ("approve", "authorize", "authorise", "consent to")

    # Act / Assert
    for phase in WorkspacePhase:
        guidance = derive_guidance(phase)
        if guidance.active_actor is not GuidanceActor.AGENT:
            continue
        text = f"{guidance.headline} {guidance.instruction} {guidance.expected_consequence}".lower()
        assert not any(word in text for word in forbidden), (
            f"{phase.value} tells the agent it may approve something"
        )


def test_a_waiting_phase_says_what_it_is_waiting_for() -> None:
    """FR-124: "while an actor is waiting, the interface shall say what condition
    is pending" — a silent disabled state is the failure being avoided."""
    # Arrange / Act / Assert
    for phase in WorkspacePhase:
        guidance = derive_guidance(phase)
        if guidance.action_code is GuidanceActionCode.WAIT:
            assert guidance.waiting_for, f"{phase.value} waits without saying for what"


def test_the_system_is_the_active_actor_only_while_it_is_working() -> None:
    """Naming `system` when a person is expected to act would leave both of them
    waiting for the other."""
    # Arrange
    system_phases = {
        phase
        for phase in WorkspacePhase
        if derive_guidance(phase).active_actor is GuidanceActor.SYSTEM
    }

    # Act / Assert — every one of them is a wait, not a request for input.
    for phase in system_phases:
        guidance = derive_guidance(phase)
        assert guidance.action_code is GuidanceActionCode.WAIT
        assert guidance.requires_human_input is False


def test_human_input_is_flagged_wherever_a_person_is_the_active_actor() -> None:
    """FR-121's `requires_human_input` exists so an agent need not infer it."""
    # Arrange
    human = {GuidanceActor.OPERATOR, GuidanceActor.HUMAN_APPROVER}

    # Act / Assert — a phase asking a person for a decision says so. Review
    # phases are terminal reading, so they do not block on input.
    blocking = {
        GuidanceActionCode.SELECT_CONTRACT,
        GuidanceActionCode.ARM_RUN,
        GuidanceActionCode.DECIDE_CONFIRMATION,
        GuidanceActionCode.CURATE_CANDIDATES,
    }
    for phase in WorkspacePhase:
        guidance = derive_guidance(phase)
        if guidance.action_code in blocking:
            assert guidance.active_actor in human
            assert guidance.requires_human_input is True


def test_the_copy_version_is_recorded_so_history_stays_readable() -> None:
    """§12.13: display-copy changes "never rewrite historical actor, action code,
    correlation, or outcome evidence" — which only works if the version shown is
    stored with the message."""
    # Arrange / Act / Assert
    assert COPY_VERSION
    assert isinstance(COPY_VERSION, str)


def test_a_correlation_id_is_carried_when_one_is_given() -> None:
    # Arrange / Act
    guidance = derive_guidance(WorkspacePhase.ARMED, correlation_id="run_42")

    # Assert
    assert guidance.correlation_id == "run_42"
    assert isinstance(guidance, GuidanceState)


# --- cancellation (AC-21's "safe recovery", §14.9) --------------------------


def test_cancelling_is_offered_where_a_pending_confirmation_exists() -> None:
    """AC-21 asks every blocking transition to name a safe recovery, and the
    blocking transition is `awaiting_confirmation`: a person is being asked for
    a decision they may be unable to make.

    §14.9 makes cancelling a distinct outcome from denial — "nobody refused the
    action, the request simply stopped being answerable" — so it is the recovery
    rather than a second primary action.
    """
    # Arrange / Act
    guidance = derive_guidance(WorkspacePhase.AWAITING_CONFIRMATION)

    # Assert
    assert guidance.action_code is GuidanceActionCode.DECIDE_CONFIRMATION
    assert guidance.recovery_action_code is GuidanceActionCode.CANCEL_CONFIRMATION


def test_cancelling_is_offered_nowhere_a_pending_confirmation_cannot_exist() -> None:
    """Guidance may not name a capability the API does not have.

    `DELETE /runs/{run_id}/confirmations/{confirmation_id}` is the only endpoint
    that cancels, it routes to `Decision.CANCEL`, and `DecisionService._pending`
    refuses any request whose status is not `pending`. There is no run-level
    cancel endpoint at all — so `running` and `verifying`, where an operator
    might most want one, get a reset instead, which is a thing the server can
    actually do.
    """
    # Arrange
    offering = {
        phase
        for phase in WorkspacePhase
        if GuidanceActionCode.CANCEL_CONFIRMATION
        in {derive_guidance(phase).action_code, derive_guidance(phase).recovery_action_code}
    }

    # Act / Assert
    assert offering == {WorkspacePhase.AWAITING_CONFIRMATION}


def test_cancelling_is_never_the_primary_action() -> None:
    """FR-122: copy must not imply that abandoning the request is what is being
    asked for. The decision is the ask; cancelling is the way out of it."""
    # Arrange / Act / Assert
    for phase in WorkspacePhase:
        assert derive_guidance(phase).action_code is not GuidanceActionCode.CANCEL_CONFIRMATION


# --- recovery coverage (FR-120's "optional recovery action") ----------------


#: The phases where something is genuinely in flight or blocked, so "if this
#: stalls" describes a real situation a person can be in. Written as a literal
#: set rather than derived, because deriving it from the same table it checks
#: would make the test agree with any answer.
_RECOVERABLE_PHASES = {
    WorkspacePhase.PROPOSING,
    WorkspacePhase.CANDIDATES,
    WorkspacePhase.ARMED,
    WorkspacePhase.RUNNING,
    WorkspacePhase.AWAITING_CONFIRMATION,
    WorkspacePhase.VERIFYING,
    WorkspacePhase.ERROR,
    WorkspacePhase.CANCELLED,
    WorkspacePhase.EVAL_READY,
    WorkspacePhase.EVAL_RUNNING,
}


@pytest.mark.parametrize("phase", sorted(_RECOVERABLE_PHASES))
def test_every_stallable_phase_names_a_way_out(phase: WorkspacePhase) -> None:
    """Recovery used to be set on three phases out of thirteen, which meant a
    verification that hung, a curation nobody wanted to finish, and a replay
    whose process died all presented a person with no named exit."""
    # Arrange / Act / Assert
    assert derive_guidance(phase).recovery_action_code is not None


@pytest.mark.parametrize(
    "phase",
    sorted(set(WorkspacePhase) - _RECOVERABLE_PHASES),
)
def test_a_phase_with_nothing_stuck_invents_no_recovery(phase: WorkspacePhase) -> None:
    """The null is deliberate, not an omission.

    A reached verdict did not stall, and a workspace with no run has nothing to
    release. Reset is legal in both (FR-013 makes it legal everywhere) and would
    change nothing a person could observe — so offering it under "if this
    stalls" would teach a reader that the label means nothing.
    """
    # Arrange / Act / Assert
    assert derive_guidance(phase).recovery_action_code is None


def test_a_recovery_is_never_the_action_it_recovers_from() -> None:
    """A recovery identical to the primary action is not a way out of anything."""
    # Arrange / Act / Assert
    for phase in WorkspacePhase:
        guidance = derive_guidance(phase)
        if guidance.recovery_action_code is None:
            continue
        assert guidance.recovery_action_code is not guidance.action_code


@pytest.mark.parametrize("phase", list(WorkspacePhase))
def test_every_recovery_code_has_copy_a_person_can_read(phase: WorkspacePhase) -> None:
    """The banner renders the recovery as a sentence, and the sentence comes
    from `GUIDANCE_ACTION_DESCRIPTIONS`. A code with no description would put a
    raw enum token back in front of a human being."""
    # Arrange
    code = derive_guidance(phase).recovery_action_code

    # Act / Assert
    if code is not None:
        assert GUIDANCE_ACTION_DESCRIPTIONS[code].strip()


# --- the regression-eval phases §11.5 draws ---------------------------------


def test_a_verdict_alone_does_not_reach_the_eval_phases() -> None:
    """§11.5's edge is `Failed --> EvalReady: eval created`, not `Failed -->
    EvalReady`. Until a case exists there is nothing to replay."""
    # Arrange / Act / Assert
    assert phase_for(has_contract=True, run_state=RunState.FAILED) is WorkspacePhase.FAILED
    assert (
        phase_for(has_contract=True, run_state=RunState.PASSED_WITH_WARNINGS)
        is WorkspacePhase.PASSED_WITH_WARNINGS
    )


@pytest.mark.parametrize("run_state", [RunState.FAILED, RunState.PASSED_WITH_WARNINGS])
def test_a_generated_case_moves_a_verdict_to_eval_ready(run_state: RunState) -> None:
    """The `eval created` edge. Before this projection existed, `eval_ready` had
    guidance no reader could ever be shown and the server could not emit
    `run_regression_eval` however many cases a workspace held."""
    # Arrange / Act
    phase = phase_for(has_contract=True, run_state=run_state, regression_case_ready=True)

    # Assert
    assert phase is WorkspacePhase.EVAL_READY
    assert derive_guidance(phase).action_code is GuidanceActionCode.RUN_REGRESSION_EVAL


@pytest.mark.parametrize("run_state", [RunState.FAILED, RunState.PASSED_WITH_WARNINGS])
def test_an_open_replay_moves_eval_ready_to_eval_running(run_state: RunState) -> None:
    """The `replay started` edge, and it outranks `eval_ready`: a replay in
    flight is what the reader is waiting on."""
    # Arrange / Act
    phase = phase_for(
        has_contract=True,
        run_state=run_state,
        regression_case_ready=True,
        regression_replay_open=True,
    )

    # Assert
    assert phase is WorkspacePhase.EVAL_RUNNING
    assert derive_guidance(phase).action_code is GuidanceActionCode.WAIT


def test_a_passed_run_is_never_offered_a_replay() -> None:
    """§15.4 generates a case "from a failed or warning-bearing run", so a case
    cannot exist for a clean pass. Offering the replay anyway would name an
    artifact the API refuses to create."""
    # Arrange / Act / Assert
    assert (
        phase_for(
            has_contract=True,
            run_state=RunState.PASSED,
            regression_case_ready=True,
            regression_replay_open=True,
        )
        is WorkspacePhase.PASSED
    )


def test_the_eval_flags_do_not_reopen_a_run_that_is_still_in_flight() -> None:
    """A workspace can hold a case from an earlier run while a new one is
    running. The live run wins: §11.5 draws the eval edges out of terminal
    verdicts only."""
    # Arrange / Act / Assert
    assert (
        phase_for(
            has_contract=True,
            run_state=RunState.RUNNING,
            regression_case_ready=True,
            regression_replay_open=True,
        )
        is WorkspacePhase.RUNNING
    )


def test_every_phase_the_projection_can_reach_has_guidance() -> None:
    """The converse of the totality test, and the one that catches a dead entry.

    `derive_guidance` being total over `WorkspacePhase` says every entry renders;
    it says nothing about whether anyone can get there. `eval_ready` and
    `eval_running` passed that test for as long as they were unreachable.
    """
    # Arrange — every phase `phase_for` can produce, across its whole input space.
    reachable = {
        phase_for(has_contract=has_contract, run_state=None) for has_contract in (True, False)
    }
    for run_state in RunState:
        for case_ready in (False, True):
            for replay_open in (False, True):
                reachable.add(
                    phase_for(
                        has_contract=True,
                        run_state=run_state,
                        regression_case_ready=case_ready,
                        regression_replay_open=replay_open,
                    )
                )

    # Act / Assert — no phase is registered that nothing can produce.
    assert reachable == set(WorkspacePhase)
