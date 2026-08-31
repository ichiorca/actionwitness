"""Contract lane (spec §26.1).

Contract parsing and limits land in M1. What is assertable now is the vocabulary
the contract layer will validate against: a contract names run states and event
types, and those names come from the shared registry rather than being retyped.
"""

import pytest
from actionwitness_core.journeys.enums import RunState


@pytest.mark.contracts
def test_run_state_vocabulary_is_closed_and_addressable_by_value() -> None:
    """A value outside the enum is invalid input, not an unknown extension."""
    assert RunState("armed") is RunState.ARMED
    with pytest.raises(ValueError):
        RunState("almost_armed")


@pytest.mark.contracts
def test_terminal_verdict_states_are_distinguishable_from_harness_error() -> None:
    """A harness that could not run is never reported as a business verdict."""
    verdicts = {RunState.PASSED, RunState.PASSED_WITH_WARNINGS, RunState.FAILED}
    assert RunState.ERROR not in verdicts
    assert RunState.CANCELLED not in verdicts


@pytest.mark.contracts
def test_proposal_states_carry_no_verdict_state() -> None:
    """A proposal run judges no contract, so it must not reach a verdict state."""
    proposal = {RunState.PROPOSING, RunState.CAPTURING, RunState.PROPOSED}
    verdicts = {RunState.PASSED, RunState.PASSED_WITH_WARNINGS, RunState.FAILED}
    assert proposal.isdisjoint(verdicts)
