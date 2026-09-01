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


@pytest.mark.parametrize("run_state", [RunState.ERROR, RunState.CANCELLED])
def test_an_errored_or_cancelled_run_returns_the_workspace_to_ready(
    run_state: RunState,
) -> None:
    """Reset retains the selected contract (FR-013), so there is nothing to
    choose again — the workspace is ready to arm, not back at the start."""
    # Arrange / Act / Assert
    assert phase_for(has_contract=True, run_state=run_state) is WorkspacePhase.CONTRACT_READY


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
