"""Per-workspace admission control (ADR-0003, FR-007).

ADR-0003 describes two tiers and is explicit about which one is load-bearing:

> a keyed `asyncio.Lock` as **admission control**, and the database transaction
> as the serialization boundary. Correctness never depends on the lock.

That distinction is the whole design. The lock exists so two requests for the
same workspace queue in the process instead of racing into SQLite and each
spending five seconds discovering they collided. If this module were deleted,
`BEGIN IMMEDIATE` and the unique constraints would still produce correct
results — slower, and with more `WORKSPACE_LOCK_TIMEOUT` responses, but never
wrong. Anything that would *break* without the lock is a bug in the transaction,
not a reason to trust the lock.

Two rules follow, and both are tested:

**Different workspaces never wait on each other.** FR-007 requires concurrent
mutations in separate workspaces to proceed concurrently. A single global lock
would satisfy every correctness test in this milestone and quietly serialize the
entire service.

**Nothing is held across a wait.** A lock taken here is released before any
browser I/O, SSE delivery, or human confirmation. M5's confirmation flow is two
transactions with a person in between precisely because neither may hold this,
so `hold()` wraps a bounded unit of work and nothing else.

Cleanup is by reference count rather than by sweeping unheld keys, and the
difference matters. Deleting a key the moment its lock reads as unlocked races
with a waiter that has already read the lock object but not yet acquired it: the
key vanishes, the next caller creates a *second* lock for the same workspace,
and two mutations are admitted at once. Counting holders-and-waiters removes the
key only when nobody can still be referring to it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from actionwitness_service.api.errors import ApiError, ApiErrorCode

__all__ = ["ACQUIRE_TIMEOUT_SECONDS", "WorkspaceLocks"]

#: Matches ADR-0003's 5,000 ms SQLite busy timeout. Waiting longer here than the
#: database would wait makes admission control the slower path, which defeats it.
ACQUIRE_TIMEOUT_SECONDS: Final = 5.0


class WorkspaceLocks:
    """Keyed `asyncio.Lock`s whose key set is bounded by in-flight work.

    Not a cache and not a registry of live workspaces: a key exists only while
    somebody holds or awaits it. FR-009's workspaces are anonymous and cheap to
    create, so a map that grew one entry per workspace seen would be a slow
    memory leak driven entirely by anonymous traffic.
    """

    def __init__(self, *, timeout_seconds: float = ACQUIRE_TIMEOUT_SECONDS) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        #: Holders plus waiters per key. The key is dropped when this reaches 0,
        #: which is the only moment at which no coroutine can still be holding a
        #: reference to the lock object.
        self._users: dict[str, int] = {}
        self._timeout = timeout_seconds

    def __len__(self) -> int:
        """Keys currently tracked. Zero when nothing is in flight."""
        return len(self._locks)

    def is_held(self, workspace_id: str) -> bool:
        lock = self._locks.get(workspace_id)
        return lock is not None and lock.locked()

    @asynccontextmanager
    async def hold(self, workspace_id: str) -> AsyncIterator[None]:
        """Admit one mutation for `workspace_id`, or refuse it.

        A wait longer than the timeout becomes `WORKSPACE_LOCK_TIMEOUT` — the
        same stable, retryable code SQLite contention produces, because from the
        caller's side they are the same fact: the workspace was busy. The server
        does not retry on the caller's behalf (constitution §5).

        The lock is released and the reference dropped on every exit path,
        cancellation included.
        """
        lock = self._acquire_reference(workspace_id)
        try:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=self._timeout)
            except TimeoutError as exc:
                raise ApiError(
                    ApiErrorCode.WORKSPACE_LOCK_TIMEOUT,
                    "The workspace was busy for longer than the configured wait. "
                    "Retry the identical request under its original idempotency key.",
                ) from exc

            try:
                yield
            finally:
                lock.release()
        finally:
            self._release_reference(workspace_id)

    def release_idle(self) -> int:
        """Drop every key nobody holds or awaits. Returns how many went.

        The reference count already removes a key as its last user leaves, so
        this normally finds nothing; it exists for a periodic cleanup pass to
        call without needing to know that, and it returns a number so a test can
        show the map really is bounded rather than merely believed to be.
        """
        idle = [key for key, users in self._users.items() if users <= 0]
        for key in idle:
            self._forget(key)
        return len(idle)

    def _acquire_reference(self, workspace_id: str) -> asyncio.Lock:
        lock = self._locks.get(workspace_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[workspace_id] = lock
        self._users[workspace_id] = self._users.get(workspace_id, 0) + 1
        return lock

    def _release_reference(self, workspace_id: str) -> None:
        remaining = self._users.get(workspace_id, 0) - 1
        if remaining > 0:
            self._users[workspace_id] = remaining
            return
        self._forget(workspace_id)

    def _forget(self, workspace_id: str) -> None:
        self._locks.pop(workspace_id, None)
        self._users.pop(workspace_id, None)
