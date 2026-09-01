"""Confirmation records for the store's one protected operation (§14, §15.5).

Spec v1.9 §14 (the confirmation interaction and its bindings), §15.5 (the
request / decision / cancel / checkout endpoints the store owns), §17.1 (the
status vocabulary), FR-066 ("approval is single-use... stale, expired, reused,
mismatched, denied, or cancelled confirmations shall never authorize a
mutation"), Appendix D.2 (`proceed_to_checkout`).

A confirmation is bound to *state*, not just to a workspace. §14 requires an
"authoritative state-binding hash", and the reason is the gap between asking and
deciding: nothing is held across a human's decision (ADR-0003), so the cart may
have moved while the modal was open. Approving a 20.00 total and then checking
out a 45.00 cart is precisely the outcome the binding refuses.

`consumed` is a distinct status from `approved` because approval is single-use.
Collapsing them would make a replayed approval indistinguishable from a fresh
one, which is the whole subject of AC-06's stale-and-reuse matrix.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "AUTHORIZING_STATUS",
    "TERMINAL_STATUSES",
    "Confirmation",
    "ConfirmationStatus",
]


class ConfirmationStatus(StrEnum):
    """§17.1's `confirmation_requests.status` vocabulary."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    CONSUMED = "consumed"


#: The one status that can authorize a protected mutation. A set rather than an
#: equality check so the rule reads as a rule; FR-066 lists five statuses that
#: must never authorize, and this is the complement.
AUTHORIZING_STATUS: frozenset[ConfirmationStatus] = frozenset({ConfirmationStatus.APPROVED})

#: Statuses no further decision can move.
TERMINAL_STATUSES: frozenset[ConfirmationStatus] = frozenset(
    {
        ConfirmationStatus.DENIED,
        ConfirmationStatus.EXPIRED,
        ConfirmationStatus.CANCELLED,
        ConfirmationStatus.CONSUMED,
    }
)


class Confirmation:
    """One pending or resolved request for human approval."""

    __slots__ = (
        "confirmation_id",
        "consequence",
        "consumed_at",
        "created_at",
        "decided_at",
        "expires_at",
        "state_binding_hash",
        "status",
        "workspace_id",
    )

    def __init__(
        self,
        *,
        confirmation_id: str,
        workspace_id: str,
        status: ConfirmationStatus,
        state_binding_hash: str,
        consequence: Mapping[str, Any],
        expires_at: datetime,
        created_at: datetime,
        decided_at: datetime | None = None,
        consumed_at: datetime | None = None,
    ) -> None:
        self.confirmation_id = confirmation_id
        self.workspace_id = workspace_id
        self.status = status
        self.state_binding_hash = state_binding_hash
        self.consequence = dict(consequence)
        self.expires_at = expires_at
        self.created_at = created_at
        self.decided_at = decided_at
        self.consumed_at = consumed_at

    def is_expired(self, now: datetime) -> bool:
        """True once the decision window has closed.

        Evaluated against an injected instant rather than the wall clock, so an
        expiry test does not have to sleep and a replay reaches the same verdict.
        """
        return now >= self.expires_at

    #: Statuses that lapse when the window closes. Both are still *actionable* -
    #: a pending request can be decided, an approval can be spent - so both have
    #: to stop being actionable at expiry.
    #:
    #: An approval is included deliberately. FR-062 expires an *unresolved*
    #: request, but FR-066 lists "expired" among the confirmations that "shall
    #: never authorize a mutation", and the constitution binds a protected
    #: mutation to its expiry as well as its arguments. An approval that stayed
    #: valid indefinitely would mean a human who approved a cart an hour ago,
    #: walked away, and never came back had authorized whatever happened next -
    #: which is the stale-consent case the window exists to bound.
    #:
    #: Terminal statuses are left alone: a consumed approval already happened, and
    #: relabelling a denial or a cancellation as an expiry would lose the reason
    #: the mutation was refused.
    _LAPSING = frozenset({ConfirmationStatus.PENDING, ConfirmationStatus.APPROVED})

    def effective_status(self, now: datetime) -> ConfirmationStatus:
        """The status accounting for elapsed time.

        A confirmation past its expiry *is* expired, whether or not a sweeper has
        written that yet. Reading the stored value alone would let a lapsed
        approval authorize a mutation simply because nothing had got around to
        updating the row.
        """
        if self.status in self._LAPSING and self.is_expired(now):
            return ConfirmationStatus.EXPIRED
        return self.status

    def as_document(self) -> dict[str, Any]:
        """The bounded body the store's API returns.

        Carries no cart token, no credential, and no customer data - §20.3
        forbids raw payloads in anything a tool can read, and this body reaches
        an agent through the harness.
        """
        return {
            "confirmation_id": self.confirmation_id,
            "status": str(self.status),
            "consequence": dict(self.consequence),
            "expires_at": _iso(self.expires_at),
        }


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")
