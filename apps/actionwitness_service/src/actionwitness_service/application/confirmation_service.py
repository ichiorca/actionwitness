"""Human confirmation of a protected mutation (§14, FR-060–FR-066).

A confirmation is the harness asking a person to authorize one exact action, and
the constitution is unambiguous about who may answer: "an agent cannot create,
broaden, or approve its own consent." That is enforced here and at the decision
endpoint, not in the dialog — a rule that lives only in the UI is a rule any
client can skip.

**What the approval is bound to is the whole security property.** §14 makes the
workspace cookie the authorization boundary and the `confirmation_id` merely an
identifier, so an approval that was not bound to the exact material state could
be replayed against a different one. Every request therefore records:

- the workspace and run it belongs to, which scope who may decide it;
- the invocation's correlation id, which is what the core's
  `requires_confirmation` policy matches an approval to a mutation by (FR-060);
- a `state_binding_hash` — the content hash of the **independently observed**
  canonical state, not of anything the tool reported; and
- an expiry, from the contract's own `requires_confirmation` policy.

**The consequence summary is derived from the adapter's declared effects, not
from knowledge of the target.** §14.1 wants a human to see "cart version and
exact total" for the Buggy Store, but the harness may not know what a cart is
(§9.1). So the summary is the observed state pruned to the effect paths the
adapter declares for this tool (§13.4) — which yields the cart for a cart tool
and the right thing for a target nobody has written yet.

Nothing here holds a transaction across a human decision. A 60-second write
lock would stall every other workspace (ADR-0003), and the revalidation at
consumption time is what makes holding one unnecessary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from actionwitness_core.contracts.enums import PolicyType
from actionwitness_core.evidence.effects import bounded, effect_context
from actionwitness_core.journeys.enums import ConfirmationStatus
from actionwitness_core.kernel import JsonValue
from actionwitness_core.ports.models import Observation
from actionwitness_core.security.canonical import content_hash
from actionwitness_core.security.redaction import RedactionPolicy

from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import new_id

__all__ = [
    "CONFIRMATION_EVENT_RESERVATION",
    "ConfirmationRequirement",
    "ConfirmationService",
    "confirmation_requirement",
    "consequence_summary",
]

#: Events a protected invocation adds beyond an ordinary one: the request, the
#: decision, and nothing else — the terminal event is already counted. Reserved
#: against FR-008's ceiling so a run cannot be left unable to record the
#: decision it is waiting for, which would strand it awaiting a confirmation it
#: can never resolve.
CONFIRMATION_EVENT_RESERVATION: Final = 2


@dataclass(frozen=True, slots=True)
class ConfirmationRequirement:
    """One `requires_confirmation` policy, as it applies to one tool."""

    tool: str
    timeout_seconds: int


def confirmation_requirement(
    contract_document: Mapping[str, Any] | None, tool_name: str
) -> ConfirmationRequirement | None:
    """Whether this run's contract protects `tool_name`, and for how long.

    Read from the contract rather than from a list of tool names the harness
    keeps, because "which actions need a human" is a statement about the
    journey being judged, not about the target. A harness that decided this
    itself would be deciding what is consequential on the operator's behalf.
    """
    if not contract_document:
        return None
    for policy in contract_document.get("policies") or []:
        if not isinstance(policy, Mapping):
            continue
        if policy.get("type") != PolicyType.REQUIRES_CONFIRMATION.value:
            continue
        if str(policy.get("tool")) != tool_name:
            continue
        # FR-062 fixes the default and the range; the model has already
        # validated it, so an absent value here means "the default applied".
        return ConfirmationRequirement(
            tool=tool_name, timeout_seconds=int(policy.get("timeout_seconds", 60))
        )
    return None


def consequence_summary(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    observed: Observation,
    effect_paths: Sequence[str],
    policy: RedactionPolicy | None = None,
) -> dict[str, JsonValue]:
    """What the human is shown, derived without target knowledge.

    The state fragment is pruned to the paths the adapter declares this tool
    affects (§13.4), so a person sees the part of the world the action is about
    and not the whole observation. Bounded, because this is rendered in a modal
    and stored beside an event.
    """
    return {
        "action": tool_name,
        "arguments": bounded(dict(arguments)),  # type: ignore[arg-type]
        "state_version": observed.state_version,
        "affects": bounded(  # type: ignore[arg-type]
            effect_context(list(effect_paths), observed.as_context(), policy=policy) or {}
        ),
    }


class ConfirmationService:
    """Reads and writes confirmation requests, always workspace-scoped."""

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    async def open(
        self,
        *,
        run_id: str,
        correlation_id: str,
        tool_name: str,
        state_binding_hash: str,
        consequence: Mapping[str, JsonValue],
        expires_at: datetime,
    ) -> str:
        """Insert one pending request. Inside the caller's transaction.

        The caller's transaction is the invocation's own start transaction, so
        the request and the start event it belongs to commit together. A
        confirmation without its start event would be consent for an action the
        timeline never records being attempted.
        """
        confirmation_id = new_id("cnf")
        await self._work.execute(
            """
            INSERT INTO confirmation_requests (
                id, workspace_id, run_id, correlation_id, tool_name,
                state_binding_hash, consequence_summary_json, status,
                expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                confirmation_id,
                self._workspace_id,
                run_id,
                correlation_id,
                tool_name,
                state_binding_hash,
                json.dumps(dict(consequence), sort_keys=True),
                str(ConfirmationStatus.PENDING.value),
                expires_at.isoformat(),
                self._work.now(),
            ),
        )
        return confirmation_id

    async def get(self, confirmation_id: str) -> Mapping[str, Any] | None:
        """One request this workspace owns, or nothing."""
        row = await self._work.fetch_one(
            "SELECT * FROM confirmation_requests WHERE id = ? AND workspace_id = ?",
            (confirmation_id, self._workspace_id),
        )
        return None if row is None else dict(row)

    async def pending_for_run(self, run_id: str) -> Mapping[str, Any] | None:
        """The run's unresolved request, if it has one.

        `pending` is a stored status rather than a computed one, so an expired
        request still reads as pending here. That is deliberate: expiry is
        *decided* at the moment someone acts on it (§14.8), and a reader that
        silently reclassified it would make the expiry event unattributable.
        """
        row = await self._work.fetch_one(
            """
            SELECT * FROM confirmation_requests
             WHERE workspace_id = ? AND run_id = ? AND status = ?
             ORDER BY created_at DESC, id DESC LIMIT 1
            """,
            (self._workspace_id, run_id, str(ConfirmationStatus.PENDING.value)),
        )
        return None if row is None else dict(row)

    async def mark(
        self, confirmation_id: str, status: ConfirmationStatus, *, consumed: bool = False
    ) -> bool:
        """Move a request out of `pending`, exactly once.

        The `status = 'pending'` predicate is the mechanism, not a guard: two
        decisions racing on one request both reach this statement, and only one
        can match. A read-then-write would let both through and record two
        decisions for one consent.
        """
        columns = "status = ?, decided_at = ?"
        values: list[Any] = [str(status.value), self._work.now()]
        if consumed:
            columns += ", consumed_at = ?"
            values.append(self._work.now())
        cursor = await self._work.execute(
            f"UPDATE confirmation_requests SET {columns} "
            "WHERE id = ? AND workspace_id = ? AND status = ?",
            (*values, confirmation_id, self._workspace_id, str(ConfirmationStatus.PENDING.value)),
        )
        return cursor.rowcount == 1

    async def consume_approved(self, confirmation_id: str) -> bool:
        """Spend an approval, exactly once (FR-066).

        Separate from `mark` because it moves from `approved` rather than from
        `pending`: an approval is spent by the mutation it authorized, and only
        after that mutation is known to have happened.
        """
        cursor = await self._work.execute(
            "UPDATE confirmation_requests SET status = ?, consumed_at = ? "
            "WHERE id = ? AND workspace_id = ? AND status = ?",
            (
                str(ConfirmationStatus.CONSUMED.value),
                self._work.now(),
                confirmation_id,
                self._workspace_id,
                str(ConfirmationStatus.APPROVED.value),
            ),
        )
        return cursor.rowcount == 1


def binding_hash(observed: Observation) -> str:
    """The hash an approval is bound to: the independently observed state.

    Not the tool's reported state and not the arguments. §14 asks a human to
    approve an action against the world as it *is*, and the only account of
    that the harness trusts is its own observation (constitution §5: "a tool's
    self-report is evidence, never proof").
    """
    return content_hash(dict(observed.payload))


def expiry_from(now: datetime, requirement: ConfirmationRequirement) -> datetime:
    """When this request lapses. FR-062's contract-configured timeout."""
    return now + timedelta(seconds=requirement.timeout_seconds)
