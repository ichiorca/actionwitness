"""FR-009's per-peer token buckets.

FR-009: "a simple per-IP token-bucket limit of 120 API requests per minute with
a burst of 30, plus a stricter limit of 10 workspace creations per hour. Health
checks and static assets are excluded. ... Limits shall return stable `429`
responses and shall never partially commit a mutation."

Read carefully, "120 per minute with a burst of 30" fixes two different numbers:
the **refill rate** is 2 tokens per second, and the **capacity** is 30. A bucket
sized 120 would let a client fire 120 requests instantly, which is not a burst
of 30; a bucket refilling at 30/minute would throttle a compliant client to a
quarter of its allowance. Both readings pass a naive test, so both are tested
against here.

**The client key is the direct peer.** §20.1: "Derive the rate-limit client key
from the direct peer or explicitly trusted platform proxy metadata; never trust
an arbitrary client-supplied forwarding header." A forwarding header is honoured
only when the direct peer is itself an operator-configured trusted proxy — which
means an unconfigured deployment ignores the header entirely, and a client that
invents one is rate-limited as itself. Trusting it unconditionally would make
the limit opt-out: one header per request and every attacker is a new client.

**Nothing here writes.** The refusal happens before a handler runs, so "never
partially commit a mutation" is satisfied by there being nothing to commit
rather than by a rollback that has to be remembered.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

__all__ = [
    "REQUESTS_PER_MINUTE",
    "REQUEST_BURST",
    "WORKSPACE_CREATIONS_PER_HOUR",
    "RateLimiter",
    "TokenBucket",
    "client_key",
]

#: FR-009's two limits, one constant per number.
REQUESTS_PER_MINUTE: Final = 120
REQUEST_BURST: Final = 30
WORKSPACE_CREATIONS_PER_HOUR: Final = 10

#: Idle buckets are dropped after this long, so an anonymous public deployment
#: does not accumulate one dictionary entry per address it has ever seen. Any
#: value at or above the time a full bucket takes to refill is safe: a client
#: whose bucket is forgotten gets a full one, which is what it would have
#: refilled to anyway.
_BUCKET_IDLE_SECONDS: Final = 3600.0


@dataclass
class TokenBucket:
    """Capacity and refill rate, evaluated lazily against an injected clock.

    Lazy rather than swept by a timer: a bucket with no traffic needs no work
    done to it, and a timer that refilled every bucket every tick would make
    idle clients cost CPU proportional to how many of them there are.
    """

    capacity: float
    refill_per_second: float
    tokens: float = field(default=0.0)
    updated_at: float = field(default=0.0)

    def take(self, now: float, cost: float = 1.0) -> bool:
        """Spend one token if there is one. Returns whether the request passes."""
        if self.updated_at == 0.0:
            self.tokens = self.capacity
            self.updated_at = now
        else:
            elapsed = max(0.0, now - self.updated_at)
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
            self.updated_at = now

        if self.tokens < cost:
            return False
        self.tokens -= cost
        return True

    def retry_after_seconds(self, cost: float = 1.0) -> int:
        """Whole seconds until `cost` tokens exist. At least 1, never 0.

        A `Retry-After: 0` invites an immediate retry that will fail again,
        which turns one refused client into a busy loop.
        """
        if self.refill_per_second <= 0:
            return 1
        missing = max(0.0, cost - self.tokens)
        return max(1, int(missing / self.refill_per_second) + 1)


class RateLimiter:
    """FR-009's two buckets, keyed per client."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._requests: dict[str, TokenBucket] = {}
        self._creations: dict[str, TokenBucket] = {}

    def __len__(self) -> int:
        return len(self._requests) + len(self._creations)

    def allow_request(self, key: str) -> TokenBucket | None:
        """`None` when the request passes; the exhausted bucket when it does not."""
        return self._take(
            self._requests,
            key,
            capacity=REQUEST_BURST,
            refill_per_second=REQUESTS_PER_MINUTE / 60.0,
        )

    def allow_workspace_creation(self, key: str) -> TokenBucket | None:
        """The stricter bucket, spent at the moment a workspace is issued.

        Its caller is the resolution path, not the request path, because only
        resolution knows whether a workspace is about to exist: a cookie naming
        a workspace that was never issued — or that FR-009's cleanup has since
        removed — creates one exactly like no cookie at all. Charging on the
        cookie's absence instead would meter nothing, since a client can present
        a different invented value every time; charging on every request would
        meter the wrong thing, since one visitor refreshing a page would exhaust
        an hour's allowance in a minute.
        """
        return self._take(
            self._creations,
            key,
            capacity=WORKSPACE_CREATIONS_PER_HOUR,
            refill_per_second=WORKSPACE_CREATIONS_PER_HOUR / 3600.0,
        )

    def release_idle(self) -> int:
        """Drop buckets nobody has used recently. Returns how many went."""
        now = self._now()
        removed = 0
        for buckets in (self._requests, self._creations):
            stale = [
                key
                for key, bucket in buckets.items()
                if now - bucket.updated_at > _BUCKET_IDLE_SECONDS
            ]
            for key in stale:
                del buckets[key]
            removed += len(stale)
        return removed

    def _take(
        self,
        buckets: dict[str, TokenBucket],
        key: str,
        *,
        capacity: float,
        refill_per_second: float,
    ) -> TokenBucket | None:
        bucket = buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(capacity=capacity, refill_per_second=refill_per_second)
            buckets[key] = bucket
        return None if bucket.take(self._now()) else bucket

    def _now(self) -> float:
        return self._clock().timestamp()


def client_key(
    peer: str | None,
    forwarded_for: str | None = None,
    *,
    trusted_proxies: frozenset[str] = frozenset(),
) -> str:
    """The rate-limit key for one request (§20.1).

    The forwarding header is consulted **only** when the direct peer is an
    operator-configured trusted proxy. Otherwise it is ignored entirely, so a
    client that invents one is limited as itself rather than as whoever it
    claimed to be.

    A peer of `None` — which the ASGI transport produces in tests, and which a
    Unix-socket deployment produces in reality — keys as a single shared
    `"unknown"` client. That errs toward limiting more than necessary, which is
    the safe direction for a public deployment.
    """
    if peer is None:
        return "unknown"
    if peer not in trusted_proxies or not forwarded_for:
        return peer

    # The *last* hop is the one the trusted proxy itself observed. Earlier
    # entries were appended by whoever was upstream of it, including the client,
    # and are therefore attacker-controlled.
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    return hops[-1] if hops else peer
