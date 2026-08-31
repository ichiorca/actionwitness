"""Integration lane (spec §26.2).

Runnable from day one so nobody discovers at M4 that this lane was never wired.
It asserts the determinism contract every later integration test depends on:
BUILD_ORDER §9 forbids fixed sleeps and wall-clock dependence, so the injected
clock and identifier sequence have to be genuinely reproducible before the run
lifecycle is built on them.
"""

from datetime import datetime

import pytest


@pytest.mark.integration
def test_injected_clock_does_not_move_on_its_own(frozen_clock, epoch) -> None:
    first = frozen_clock.now()
    second = frozen_clock.now()
    assert first == second == epoch
    assert first.tzinfo is not None, "persisted instants are timezone-aware UTC"


@pytest.mark.integration
def test_injected_clock_advances_only_when_a_test_advances_it(frozen_clock) -> None:
    start = frozen_clock.now()
    frozen_clock.advance(90)
    assert (frozen_clock.now() - start).total_seconds() == 90


@pytest.mark.integration
def test_a_naive_start_instant_is_refused(clock_factory) -> None:
    """A naive datetime in persisted evidence is a defect, so it fails loudly."""
    with pytest.raises(ValueError, match="timezone-aware"):
        clock_factory(datetime(2026, 1, 1))


@pytest.mark.integration
def test_identifier_sequences_repeat_exactly_across_runs(id_sequence_factory) -> None:
    """Two identical runs must produce identical evidence, identifiers included."""
    first = id_sequence_factory()
    second = id_sequence_factory()
    produced = [(first.next("run"), first.next("workspace")) for _ in range(3)]
    repeated = [(second.next("run"), second.next("workspace")) for _ in range(3)]
    assert produced == repeated
    assert produced[0] == ("run-0001", "workspace-0001")


@pytest.mark.integration
def test_workspace_directories_are_isolated_per_test(workspace_dir) -> None:
    """The workspace is the isolation boundary; tests never share one."""
    assert workspace_dir.is_dir()
    assert list(workspace_dir.iterdir()) == []
