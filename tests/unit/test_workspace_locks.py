"""004-T4 — per-workspace admission control (ADR-0003, FR-007).

The interesting assertions are the two that a simpler implementation would also
pass while being wrong:

* a **global** lock serializes everything and satisfies every "same workspace
  serializes" test. FR-007 needs the counterpart — different workspaces run
  concurrently — so that is tested with two tasks that would deadlock the test
  if they were serialized;
* sweeping keys "when the lock reads unlocked" races a waiter that has already
  taken a reference to the lock object, and the result is two locks for one
  workspace and two mutations admitted at once. The concurrency test below fails
  against that implementation and passes against reference counting.
"""

from __future__ import annotations

import asyncio

import pytest
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.persistence.locks import ACQUIRE_TIMEOUT_SECONDS, WorkspaceLocks

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_the_timeout_matches_the_database_busy_timeout() -> None:
    """Waiting longer here than SQLite would makes admission control the slow path."""
    # Arrange / Act / Assert
    assert ACQUIRE_TIMEOUT_SECONDS == 5.0


async def test_mutations_in_one_workspace_serialize() -> None:
    # Arrange
    locks = WorkspaceLocks()
    order: list[str] = []

    async def mutate(name: str) -> None:
        async with locks.hold("ws_a"):
            order.append(f"{name}:enter")
            await asyncio.sleep(0)
            order.append(f"{name}:exit")

    # Act
    await asyncio.gather(mutate("first"), mutate("second"))

    # Assert — no interleaving: each enter is followed by its own exit.
    assert order == ["first:enter", "first:exit", "second:enter", "second:exit"]


async def test_mutations_in_different_workspaces_proceed_concurrently() -> None:
    """FR-007. A single global lock would deadlock this test at the barrier."""
    # Arrange
    locks = WorkspaceLocks()
    both_inside = asyncio.Event()
    arrived = 0

    async def mutate(workspace_id: str) -> None:
        nonlocal arrived
        async with locks.hold(workspace_id):
            arrived += 1
            if arrived == 2:
                both_inside.set()
            # Neither task may leave until the other has also entered, which is
            # only possible if the two workspaces do not share a lock.
            await asyncio.wait_for(both_inside.wait(), timeout=1.0)

    # Act
    await asyncio.gather(mutate("ws_a"), mutate("ws_b"))

    # Assert
    assert both_inside.is_set()


async def test_only_one_holder_is_admitted_while_callers_keep_arriving() -> None:
    """The reference-counting test, and the reason the key set is not swept.

    A sweep-when-unlocked implementation drops the key the moment a holder
    releases — `asyncio.Lock.release` wakes the next waiter's future but leaves
    `locked()` false until that waiter actually resumes. A caller arriving in
    that window finds no key, builds a *second* lock, and is admitted alongside
    the waiter that is about to acquire the first one.

    Arrivals are staggered rather than started together, because callers that
    all take their reference up front never reach the window: the naive
    implementation admits one holder under `gather` and three under this.
    """
    # Arrange
    locks = WorkspaceLocks()
    inside = 0
    peak = 0

    async def mutate() -> None:
        nonlocal inside, peak
        async with locks.hold("ws_a"):
            inside += 1
            peak = max(peak, inside)
            for _ in range(3):
                await asyncio.sleep(0)
            inside -= 1

    # Act — one new caller per event-loop turn, so arrivals land throughout the
    # queue's lifetime rather than all before its first release.
    tasks = []
    for _ in range(30):
        tasks.append(asyncio.create_task(mutate()))
        await asyncio.sleep(0)
    await asyncio.gather(*tasks)

    # Assert
    assert peak == 1
    assert len(locks) == 0


async def test_a_key_exists_only_while_it_is_in_use() -> None:
    """Anonymous workspaces are cheap to create; an unbounded map is a leak."""
    # Arrange
    locks = WorkspaceLocks()

    # Act / Assert
    assert len(locks) == 0
    async with locks.hold("ws_a"):
        assert len(locks) == 1
        assert locks.is_held("ws_a")
    assert len(locks) == 0
    assert not locks.is_held("ws_a")


async def test_many_workspaces_leave_nothing_behind() -> None:
    # Arrange
    locks = WorkspaceLocks()

    # Act
    for index in range(50):
        async with locks.hold(f"ws_{index}"):
            pass

    # Assert
    assert len(locks) == 0
    assert locks.release_idle() == 0


async def test_a_contended_lock_times_out_with_the_retryable_code() -> None:
    """The same stable code SQLite contention produces: the workspace was busy."""
    # Arrange — a holder that outlives the waiter's very short timeout.
    locks = WorkspaceLocks(timeout_seconds=0.01)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder() -> None:
        async with locks.hold("ws_a"):
            holder_entered.set()
            await release_holder.wait()

    holding = asyncio.create_task(holder())
    await holder_entered.wait()

    # Act
    with pytest.raises(ApiError) as caught:
        async with locks.hold("ws_a"):
            pytest.fail("the second holder must not have been admitted")

    # Assert
    assert caught.value.code is ApiErrorCode.WORKSPACE_LOCK_TIMEOUT
    assert caught.value.as_envelope()["error"]["retryable"] is True

    # Cleanup
    release_holder.set()
    await holding


async def test_a_timed_out_waiter_leaves_no_reference_behind() -> None:
    """A refused waiter must not pin the key it never acquired."""
    # Arrange
    locks = WorkspaceLocks(timeout_seconds=0.01)
    holder_entered = asyncio.Event()
    release_holder = asyncio.Event()

    async def holder() -> None:
        async with locks.hold("ws_a"):
            holder_entered.set()
            await release_holder.wait()

    holding = asyncio.create_task(holder())
    await holder_entered.wait()

    # Act
    with pytest.raises(ApiError):
        async with locks.hold("ws_a"):
            pass
    release_holder.set()
    await holding

    # Assert
    assert len(locks) == 0


async def test_a_failure_inside_the_hold_still_releases_it() -> None:
    """A domain rejection must not leave the workspace locked out forever."""

    # Arrange
    class Rejected(Exception):
        pass

    locks = WorkspaceLocks()

    # Act
    with pytest.raises(Rejected):
        async with locks.hold("ws_a"):
            raise Rejected

    # Assert
    assert not locks.is_held("ws_a")
    assert len(locks) == 0
    async with locks.hold("ws_a"):
        pass


async def test_a_cancelled_holder_still_releases_it() -> None:
    """Cancellation propagates through I/O (constitution §5); the lock goes with it."""
    # Arrange
    locks = WorkspaceLocks()
    entered = asyncio.Event()

    async def mutate() -> None:
        async with locks.hold("ws_a"):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(mutate())
    await entered.wait()

    # Act
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Assert
    assert not locks.is_held("ws_a")
    assert len(locks) == 0
