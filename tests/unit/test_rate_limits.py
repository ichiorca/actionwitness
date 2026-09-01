"""004-T9 — FR-009's token buckets and client keying.

"120 API requests per minute with a burst of 30" fixes two different numbers,
and the two plausible misreadings each pass a naive test:

* capacity 120 — a client fires 120 requests instantly, which is not a burst
  of 30;
* refill 30/minute — a compliant client is throttled to a quarter of its
  allowance.

Both are ruled out below by testing the burst and the sustained rate separately.

The keying tests are security tests. §20.1: "never trust an arbitrary
client-supplied forwarding header." A limiter that believed one would be
opt-out — a header per request and every attacker is a fresh client.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from actionwitness_service.application import rate_limits as fr009
from actionwitness_service.application.rate_limits import RateLimiter, TokenBucket, client_key

pytestmark = [pytest.mark.unit]

START = datetime(2026, 1, 1, tzinfo=UTC)


class Clock:
    """A clock the test moves by hand. Nothing here waits on real time."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_fr_009_numbers_are_transcribed_exactly() -> None:
    # Arrange / Act / Assert
    assert fr009.REQUESTS_PER_MINUTE == 120
    assert fr009.REQUEST_BURST == 30
    assert fr009.WORKSPACE_CREATIONS_PER_HOUR == 10


def test_the_burst_is_thirty_not_one_hundred_and_twenty() -> None:
    """Rules out capacity == REQUESTS_PER_MINUTE."""
    # Arrange
    clock = Clock()
    limiter = RateLimiter(clock=clock)

    # Act — 30 instant requests, then one more, with no time passing.
    allowed = sum(1 for _ in range(fr009.REQUEST_BURST) if limiter.allow_request("peer") is None)
    thirty_first = limiter.allow_request("peer")

    # Assert
    assert allowed == fr009.REQUEST_BURST
    assert thirty_first is not None


def test_the_sustained_rate_is_two_per_second_not_thirty_per_minute() -> None:
    """Rules out refill == REQUEST_BURST / 60.

    After draining the burst, half a second must buy exactly one token. At
    30/minute it would buy a quarter of one, and the request would be refused.
    """
    # Arrange
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for _ in range(fr009.REQUEST_BURST):
        limiter.allow_request("peer")

    # Act
    clock.advance(0.5)

    # Assert
    assert limiter.allow_request("peer") is None
    assert limiter.allow_request("peer") is not None


def test_a_full_minute_restores_the_whole_burst_and_no_more() -> None:
    """The bucket is capped: idling for an hour does not bank 7,200 requests."""
    # Arrange
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for _ in range(fr009.REQUEST_BURST):
        limiter.allow_request("peer")

    # Act
    clock.advance(3600)

    # Assert
    allowed = sum(1 for _ in range(200) if limiter.allow_request("peer") is None)
    assert allowed == fr009.REQUEST_BURST


def test_one_clients_exhaustion_does_not_limit_another() -> None:
    # Arrange
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for _ in range(fr009.REQUEST_BURST + 5):
        limiter.allow_request("peer_a")

    # Act / Assert
    assert limiter.allow_request("peer_b") is None


def test_workspace_creation_has_its_own_stricter_bucket() -> None:
    """Ten per hour, and spending the request bucket does not spend this one."""
    # Arrange
    clock = Clock()
    limiter = RateLimiter(clock=clock)

    # Act
    allowed = sum(
        1
        for _ in range(fr009.WORKSPACE_CREATIONS_PER_HOUR)
        if limiter.allow_workspace_creation("peer") is None
    )
    eleventh = limiter.allow_workspace_creation("peer")

    # Assert
    assert allowed == fr009.WORKSPACE_CREATIONS_PER_HOUR
    assert eleventh is not None
    # The ordinary request bucket is untouched by creations.
    assert limiter.allow_request("peer") is None


def test_the_creation_bucket_refills_over_an_hour_not_a_minute() -> None:
    # Arrange
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for _ in range(fr009.WORKSPACE_CREATIONS_PER_HOUR):
        limiter.allow_workspace_creation("peer")

    # Act / Assert — a minute buys nothing; six minutes buys one.
    clock.advance(60)
    assert limiter.allow_workspace_creation("peer") is not None
    clock.advance(300)
    assert limiter.allow_workspace_creation("peer") is None


def test_retry_after_is_never_zero() -> None:
    """A client told to retry immediately fails immediately, which is a loop."""
    # Arrange
    bucket = TokenBucket(capacity=1, refill_per_second=1000.0, tokens=0.0, updated_at=1.0)

    # Act / Assert
    assert bucket.retry_after_seconds() >= 1


def test_retry_after_reflects_the_wait() -> None:
    # Arrange — one token per second, empty bucket.
    bucket = TokenBucket(capacity=30, refill_per_second=1.0, tokens=0.0, updated_at=1.0)

    # Act / Assert
    assert bucket.retry_after_seconds(cost=3) == 4  # ceiling of 3s, plus the partial second


def test_idle_buckets_are_released() -> None:
    """An anonymous public deployment must not keep one entry per address seen."""
    # Arrange
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for index in range(50):
        limiter.allow_request(f"peer_{index}")

    # Act
    clock.advance(7200)
    released = limiter.release_idle()

    # Assert
    assert released == 50
    assert len(limiter) == 0


def test_releasing_an_idle_bucket_does_not_grant_extra_allowance() -> None:
    """Forgetting a bucket gives the client a full one — which is exactly what
    it would have refilled to anyway, so the sweep cannot be gamed."""
    # Arrange
    clock = Clock()
    limiter = RateLimiter(clock=clock)
    for _ in range(fr009.REQUEST_BURST):
        limiter.allow_request("peer")

    # Act — not yet idle, so the sweep must leave it alone.
    clock.advance(1)
    released = limiter.release_idle()

    # Assert — the bucket survived, so the client has only the two tokens one
    # second bought it, not a fresh burst of thirty.
    assert released == 0
    granted = sum(1 for _ in range(fr009.REQUEST_BURST) if limiter.allow_request("peer") is None)
    assert granted == 2


# --- client keying (§20.1) --------------------------------------------------


def test_the_key_is_the_direct_peer_by_default() -> None:
    # Arrange / Act / Assert
    assert client_key("203.0.113.9") == "203.0.113.9"


def test_a_forwarding_header_from_an_untrusted_peer_is_ignored() -> None:
    """The security case. Believing it would make the limit opt-out."""
    # Arrange / Act
    key = client_key("203.0.113.9", "198.51.100.1")

    # Assert
    assert key == "203.0.113.9"


def test_a_client_cannot_become_a_new_client_by_varying_the_header() -> None:
    # Arrange / Act
    keys = {client_key("203.0.113.9", f"10.0.0.{n}") for n in range(20)}

    # Assert — one attacker, one key, however many headers they invent.
    assert keys == {"203.0.113.9"}


def test_a_trusted_proxy_supplies_the_key() -> None:
    # Arrange / Act
    key = client_key("10.0.0.1", "198.51.100.7", trusted_proxies=frozenset({"10.0.0.1"}))

    # Assert
    assert key == "198.51.100.7"


def test_only_the_last_hop_of_a_trusted_chain_is_believed() -> None:
    """Earlier entries were appended upstream of the trusted proxy — including
    by the client itself — so they are attacker-controlled."""
    # Arrange / Act
    key = client_key(
        "10.0.0.1",
        "203.0.113.66, 198.51.100.7",
        trusted_proxies=frozenset({"10.0.0.1"}),
    )

    # Assert
    assert key == "198.51.100.7"


def test_a_trusted_proxy_with_an_empty_header_falls_back_to_itself() -> None:
    # Arrange / Act / Assert
    assert client_key("10.0.0.1", "  ,  ", trusted_proxies=frozenset({"10.0.0.1"})) == "10.0.0.1"


def test_a_peerless_request_keys_as_one_shared_client() -> None:
    """A Unix-socket deployment has no peer address. Sharing one bucket limits
    more than necessary, which is the safe direction for a public service."""
    # Arrange / Act / Assert
    assert client_key(None) == "unknown"
    assert client_key(None, "198.51.100.7") == "unknown"
