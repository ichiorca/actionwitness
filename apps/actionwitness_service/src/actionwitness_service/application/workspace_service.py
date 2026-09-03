"""Workspace state transitions behind §15.1's four routes.

FR-013 defines reset, and the sentence to read slowly is the last one: reset
"shall cancel nonterminal runs, benchmarks, pairings, and unresolved
confirmations ... and **preserve completed artifacts and the selected contract**
so the workspace returns to `ContractReady`."

**Reset is not delete.** A reset that cleared everything would be simpler to
write and would destroy the evidence this product exists to keep. The
cancellation half and the retention half are equally normative, and the
retention half is the one a plausible implementation gets wrong — so it has its
own tests rather than being assumed.

`purge_completed` is the *opt-in* second thing, and it is the only path that
removes terminal evidence. §15.1 scopes it to "this workspace's completed
runs/evals/benchmarks/artifacts after preserving built-in templates", which the
schema gives for free: a template belongs to no workspace, so nothing scoped to
one can reach it.

Every method here takes a `UnitOfWork`. Cancelling a run, appending its
cancellation event, and clearing the workspace's pointer to it are one
transaction or they are a workspace that believes it has no active run while a
run believes it is still running.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from actionwitness_core.journeys.enums import EventActor, OutcomeEventType, RunState
from actionwitness_core.journeys.guidance import GuidanceState
from actionwitness_core.journeys.transitions import is_terminal

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.guidance_service import current_guidance
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import EventRepository

__all__ = [
    "NONTERMINAL_RUN_STATES",
    "UNRESOLVED_CONFIRMATION_STATUSES",
    "ResetOutcome",
    "WorkspaceService",
]

#: Derived from the core's own transition table rather than listed by hand, so a
#: state added in a later milestone is classified correctly without anyone
#: remembering to update this module (§16).
NONTERMINAL_RUN_STATES: Final[frozenset[str]] = frozenset(
    str(state.value) for state in RunState if not is_terminal(state)
)
TERMINAL_RUN_STATES: Final[frozenset[str]] = frozenset(
    str(state.value) for state in RunState if is_terminal(state)
)

#: §17.1 `confirmation_requests.status`. "Unresolved" is pending — a decided or
#: consumed confirmation is a fact about what a human already chose, and reset
#: must not rewrite it.
UNRESOLVED_CONFIRMATION_STATUSES: Final[frozenset[str]] = frozenset({"pending"})


@dataclass(frozen=True)
class ResetOutcome:
    """What a reset actually did, so a route can report it and a test assert it."""

    runs_cancelled: int
    confirmations_cancelled: int
    runs_purged: int
    artifacts_purged: int
    contract_retained: str | None


class WorkspaceService:
    """§15.1's state changes, each inside the caller's transaction."""

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    # -- reading -------------------------------------------------------------

    async def status(self) -> Mapping[str, Any]:
        """§15.1's `GET /workspace` payload."""
        row = await self._work.fetch_one(
            "SELECT * FROM workspaces WHERE id = ?", (self._workspace_id,)
        )
        if row is None:  # pragma: no cover - the middleware creates it first
            raise ApiError(ApiErrorCode.HARNESS_ERROR, "The workspace disappeared mid-request.")

        workspace = dict(row)
        active_run = None
        if workspace["active_run_id"]:
            active = await self._work.fetch_one(
                "SELECT id, status, target_id, contract_id, started_at, completed_at "
                "FROM runs WHERE id = ? AND workspace_id = ?",
                (workspace["active_run_id"], self._workspace_id),
            )
            active_run = dict(active) if active else None

        guidance = await self.guidance()
        return {
            "workspace_id": self._workspace_id,
            "selected_target_id": workspace["selected_target_id"],
            "selected_contract_id": workspace["selected_contract_id"],
            "scenario_mode": workspace["scenario_mode"],
            "failure_profile": workspace["failure_profile"],
            # FR-172's other workspace, when this one is recording a
            # self-witnessing run. `None` for every ordinary run, and that is
            # the useful reading: it says "this run observed a target, not
            # itself", which is otherwise invisible.
            #
            # Safe to publish here and only here. The value is a bearer
            # identifier, but the workspace it names is this workspace's own
            # owned child, and the caller has already presented this
            # workspace's cookie to reach the response — so it reveals nothing
            # to anyone who could not already reach it, and lets an operator
            # open the observed workspace to see what the run was watching.
            "observed_workspace_id": workspace["observed_workspace_id"],
            "active_run": active_run,
            # §15.1 asks for "authoritative guidance, and one safe
            # `next_action`". Both come from the same `GuidanceState`, because
            # §26.1 requires the banner, the tool result, and the audit trail to
            # resolve from one server derivation — two would agree in testing
            # and diverge exactly when a person and an agent disagree about
            # whose turn it is.
            "guidance": guidance.model_dump(mode="json"),
            "next_action": guidance.next_action(),
        }

    async def guidance(self) -> GuidanceState:
        """This workspace's current guidance (FR-120).

        Delegates to the one projection every surface uses, so the banner a
        person reads and the `next_action` a tool returns cannot come from two
        different readings of the same state.
        """
        return await current_guidance(self._work, self._workspace_id)

    async def active_run(self) -> Mapping[str, Any] | None:
        row = await self._work.fetch_one(
            "SELECT * FROM runs WHERE workspace_id = ? AND status IN "
            f"({','.join('?' for _ in NONTERMINAL_RUN_STATES)}) ORDER BY started_at DESC",
            (self._workspace_id, *sorted(NONTERMINAL_RUN_STATES)),
        )
        return None if row is None else dict(row)

    # -- selection before arming ---------------------------------------------

    async def select_failure_profile(self, profile: str | None) -> None:
        """FR-011: "The selected option must be chosen before arming."

        Enforced as a refusal while a nonterminal run exists, because FR-012
        makes an armed run's fault profile immutable — "changing any value
        requires reset and creates a new run; completed evidence is never
        relabeled". Allowing the change here would relabel a run in flight.
        """
        await self._require_no_active_run("failure profile")
        await self._work.execute(
            "UPDATE workspaces SET failure_profile = ? WHERE id = ?",
            (profile, self._workspace_id),
        )

    async def select_scenario_mode(self, mode: str, supported: Sequence[str]) -> None:
        """§15.1: `pre_fix` or `post_fix` "when supported by the active adapter".

        The list of supported modes comes from the adapter's descriptor, not from
        a constant here. §9.1 is explicit that the harness "validates the
        selected value against `TargetDescriptor.supported_scenario_modes` but
        neither interprets mode names nor implements a fault" — hardcoding
        `pre_fix`/`post_fix` would make a support-ticket target impossible.
        """
        await self._require_no_active_run("scenario mode")
        if mode not in supported:
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                f"The active target does not support scenario mode {mode!r}.",
                details=[
                    {
                        "path": "scenario_mode",
                        "message": f"supported: {', '.join(sorted(supported))}",
                    }
                ],
            )
        await self._work.execute(
            "UPDATE workspaces SET scenario_mode = ? WHERE id = ?", (mode, self._workspace_id)
        )

    # -- reset ---------------------------------------------------------------

    async def reset(self, *, purge_completed: bool = False) -> ResetOutcome:
        """FR-013. Cancel what is in flight; keep what is finished."""
        confirmations = await self._cancel_unresolved_confirmations()
        runs = await self._cancel_nonterminal_runs()
        await self._cancel_live_shopify_pairing()

        purged_runs = purged_artifacts = 0
        if purge_completed:
            purged_runs, purged_artifacts = await self._purge_completed()

        # The workspace returns to `ContractReady`: the pointer to the active run
        # is cleared, and `selected_contract_id` is deliberately untouched.
        row = await self._work.fetch_one(
            "SELECT selected_contract_id FROM workspaces WHERE id = ?", (self._workspace_id,)
        )
        await self._work.execute(
            "UPDATE workspaces SET active_run_id = NULL WHERE id = ?", (self._workspace_id,)
        )

        return ResetOutcome(
            runs_cancelled=runs,
            confirmations_cancelled=confirmations,
            runs_purged=purged_runs,
            artifacts_purged=purged_artifacts,
            contract_retained=row["selected_contract_id"] if row else None,
        )

    async def _cancel_nonterminal_runs(self) -> int:
        """Move each in-flight run to `cancelled` and say so in its timeline.

        The event is appended before the status changes for a reason worth
        stating: the append reads the run's event count, and a run that had
        already been relabelled would be recording a cancellation of something
        that, by its own status, was never running.
        """
        placeholders = ",".join("?" for _ in NONTERMINAL_RUN_STATES)
        rows = await self._work.fetch_all(
            f"SELECT id FROM runs WHERE workspace_id = ? AND status IN ({placeholders})",
            (self._workspace_id, *sorted(NONTERMINAL_RUN_STATES)),
        )
        events = EventRepository(self._work)
        for row in rows:
            await events.append(
                row["id"],
                {
                    "event_type": str(OutcomeEventType.RUN_CANCELLED.value),
                    "actor": str(EventActor.HUMAN.value),
                    "redacted_payload": {"reason": "workspace_reset"},
                },
            )
            await self._work.execute(
                "UPDATE runs SET status = ?, completed_at = ? WHERE id = ? AND workspace_id = ?",
                (
                    str(RunState.CANCELLED.value),
                    self._work.now(),
                    row["id"],
                    self._workspace_id,
                ),
            )
        return len(rows)

    async def _cancel_live_shopify_pairing(self) -> None:
        """FR-013's "pairings", and §16.5's `cancelled` terminal state.

        Two writes, and the second is the one that matters. Moving the pairing to
        `cancelled` releases §17.1's one-live-pairing slot so the operator can
        start another trial. Clearing `bridge_session_token_hash` is what makes
        the reset *reach the storefront tab that is still open*: a theme holding
        a live bridge credential would otherwise keep submitting carts against a
        trial the operator ended, and §20.2 requires the harness to "fail closed
        when either credential is stale". With no stored digest there is nothing
        for `secrets.compare_digest` to match, so the next bridge request is
        refused rather than silently captured into a cancelled pairing.

        The pairing token hash is deliberately left alone: the pairing is
        terminal, so `_authorized` refuses it on the state before it ever reaches
        a comparison, and deleting a column FR-111 requires to be *the* record of
        what was issued would remove evidence to no benefit.

        Imported inside the method because `run_service` imports this module at
        module scope and `shopify_pairing` imports `run_service` - a top-level
        import here closes that ring. The states still come from §16.5's one
        definition rather than from a second list written out below.
        """
        from actionwitness_service.application.shopify_pairing import (
            TERMINAL_PAIRING_STATUSES,
            PairingStatus,
        )

        terminal = sorted(str(status.value) for status in TERMINAL_PAIRING_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        await self._work.execute(
            "UPDATE shopify_pairings SET status = ?, completed_at = ?, "
            "bridge_session_token_hash = NULL "
            f"WHERE workspace_id = ? AND status NOT IN ({placeholders})",
            (
                str(PairingStatus.CANCELLED.value),
                self._work.now(),
                self._workspace_id,
                *terminal,
            ),
        )
        # The workspace's pointer goes with it, exactly as `active_run_id` does
        # below: a workspace still naming a cancelled pairing as its active one
        # is the same disagreement between a pointer and the row it points at
        # that FR-013 removes for runs.
        await self._work.execute(
            "UPDATE workspaces SET active_shopify_pairing_id = NULL WHERE id = ?",
            (self._workspace_id,),
        )

    async def _cancel_unresolved_confirmations(self) -> int:
        """Cancel pending consent, and record the cancellation on its run.

        A decided confirmation is left alone: it is a record of what a human
        already chose, and reset must not rewrite consent that was given or
        refused (constitution §5 — an agent cannot broaden or revoke its own).
        """
        placeholders = ",".join("?" for _ in UNRESOLVED_CONFIRMATION_STATUSES)
        rows = await self._work.fetch_all(
            "SELECT id, run_id, correlation_id FROM confirmation_requests "
            f"WHERE workspace_id = ? AND status IN ({placeholders})",
            (self._workspace_id, *sorted(UNRESOLVED_CONFIRMATION_STATUSES)),
        )
        events = EventRepository(self._work)
        for row in rows:
            await self._work.execute(
                "UPDATE confirmation_requests SET status = 'cancelled', decided_at = ? "
                "WHERE id = ? AND workspace_id = ?",
                (self._work.now(), row["id"], self._workspace_id),
            )
            await events.append(
                row["run_id"],
                {
                    "event_type": str(OutcomeEventType.CONFIRMATION_CANCELLED.value),
                    "actor": str(EventActor.HUMAN.value),
                    "correlation_id": row["correlation_id"],
                    "redacted_payload": {"reason": "workspace_reset"},
                },
            )
        return len(rows)

    async def _purge_completed(self) -> tuple[int, int]:
        """§15.1's opt-in removal of this workspace's terminal evidence.

        Scoped to `workspace_id`, which is what "after preserving built-in
        templates" comes to in practice: a template belongs to no workspace, so
        a statement scoped to one cannot reach it. There is no
        `AND is_template = 0` here to get wrong.
        """
        placeholders = ",".join("?" for _ in TERMINAL_RUN_STATES)
        terminal = tuple(sorted(TERMINAL_RUN_STATES))

        artifacts = await self._work.execute(
            "DELETE FROM artifacts WHERE workspace_id = ? AND run_id IN "
            f"(SELECT id FROM runs WHERE workspace_id = ? AND status IN ({placeholders}))",
            (self._workspace_id, self._workspace_id, *terminal),
        )
        artifacts_removed = artifacts.rowcount

        runs = await self._work.execute(
            f"DELETE FROM runs WHERE workspace_id = ? AND status IN ({placeholders})",
            (self._workspace_id, *terminal),
        )
        return runs.rowcount, artifacts_removed

    async def _require_no_active_run(self, what: str) -> None:
        if await self.active_run() is not None:
            raise ApiError(
                ApiErrorCode.RUN_IN_PROGRESS,
                f"The {what} may only be changed before arming. Reset the workspace first.",
            )
