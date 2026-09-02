"""One target-tool invocation (FR-031, FR-032, FR-033, FR-036, FR-008, §15.3).

The pipeline, and why its boundaries fall where they do:

1. **Validate** the arguments against the tool's published schema. Before
   anything is written, because an argument the tool never accepts should not
   produce a start event claiming an invocation began.
2. **Observe** canonical state. I/O, so no lock and no transaction is held
   (ADR-0003).
3. **Reserve and start** — one transaction: re-check the run state, reserve the
   event budget (FR-008), move `armed` to `running` on the first action, and
   append the start event. FR-031: "every selected-target tool invocation,
   including reads, shall record a start event **before business logic
   executes**."
4. **Dispatch** through the adapter. I/O.
5. **Observe again**, immediately. This is the independent read that a verdict
   can rest on, and taking it here rather than at verification is what makes
   per-call false-success evidence possible at all (§12.2).
6. **Terminate** — one transaction, **exactly one** terminal event.

**The self-report and the observation are recorded in different places, on
purpose.** `ToolExecutionResult.state_version_after` is the version the *tool's
own response body* claimed; the `events.state_version_after` column holds what
the observation provider *independently saw*. FR-032 calls the column value
"canonical", and canonical means observed. A tool that reports success while
changing nothing therefore produces an event whose reported status says
`success` and whose observed state hash is unchanged — which is the disagreement
this entire product exists to surface. Collapsing the two into one column would
delete the evidence.

**Exactly one terminal event.** Two would make the timeline ambiguous about what
happened; zero would make a hung invocation indistinguishable from one that
never started. Dispatch and observation are both allowed to fail, and every one
of those paths converges on a single terminal append.

**A human approval is claimed before dispatch, never after it.** FR-066 asks for
the approval to be validated and consumed "in the same transaction as its
mutation", and the mutation is remote I/O that ADR-0003 forbids holding a
transaction across. So the claim is its own bounded transaction — one
conditional `UPDATE ... WHERE status = 'approved'`, committed and its lock
released before the adapter is touched — and it sits immediately before step 4.
Two resumes of one approval then race on the database rather than on the target:
one claim matches a row, every other matches none and is refused without
dispatching. Reading the approval and consuming it afterwards, with the whole of
observation and dispatch in between, is what let two resumes both spend one
person's consent.

The tradeoff is deliberate, and it points the safe way. If the dispatch then
fails, the consent is already burned and a human has to approve again. That is
worse for the person and better for the store: an approval left live after a
failure is an approval two mutations can share, and asking somebody to press the
button twice is a smaller harm than placing two orders they authorized once.

`generic` is meant literally: nothing here branches on a tool name. The adapter
publishes specs and executes; §9.1's protocols are the only thing this knows
about the target, and a branch on `update_cart` here would put commerce
semantics in the harness.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from actionwitness_core.evidence.effects import (
    bounded,
    effect_context,
    effect_evidence,
    redacted_observation,
)
from actionwitness_core.evidence.enums import ToolNamespace
from actionwitness_core.journeys.enums import (
    ConfirmationStatus,
    EventActor,
    OutcomeEventType,
    RunState,
)
from actionwitness_core.journeys.transitions import validate_run_transition
from actionwitness_core.ports.enums import RetrySemantics
from actionwitness_core.ports.models import Observation, TargetToolSpec, ToolExecutionResult
from actionwitness_core.ports.schemas import validate_arguments
from actionwitness_core.security.limits import MAX_TOOL_RESULT_CHARS
from actionwitness_core.security.redaction import RedactionPolicy, redact

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry
from actionwitness_service.application.authorization import WorkspaceScope, not_found
from actionwitness_service.application.confirmation_service import (
    CONFIRMATION_EVENT_RESERVATION,
    ConfirmationRequirement,
    ConfirmationService,
    arguments_hash,
    binding_hash,
    confirmation_requirement,
    consequence_summary,
    expiry_from,
)
from actionwitness_service.application.guidance_service import GuidanceRecorder, current_guidance
from actionwitness_service.application.limits import WorkspaceCeilings
from actionwitness_service.application.surface_service import SurfaceService
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.locks import WorkspaceLocks
from actionwitness_service.persistence.repositories import EventRepository, new_id

__all__ = ["INVOCABLE_RUN_STATES", "InvocationOutcome", "InvocationService"]

#: Server-side only, and never the structured request line: this logger carries
#: tracebacks, which §21.5's closed field set exists to keep out of the shipped
#: log stream.
_logger = logging.getLogger("actionwitness.invocation")

#: The run states in which a target action is accepted. §11.5: `Armed` and
#: `Running` publish target tools; `Verifying` publishes "status only", and
#: FR-038 makes a late action lose with `RUN_ALREADY_VERIFYING`.
INVOCABLE_RUN_STATES: Final[frozenset[str]] = frozenset(
    {str(RunState.ARMED.value), str(RunState.RUNNING.value)}
)

#: `verification_started`, the final `snapshot_captured`, and
#: `verification_completed` — the events verification writes whatever the
#: contract says (§16.1). Per-check events are counted on top of these.
_VERIFICATION_FIXED_EVENTS: Final = 3


@dataclass(frozen=True)
class InvocationOutcome:
    """What the caller is told. The self-report, labelled as one."""

    invocation_id: str
    sequence_number: int
    #: `None` while a protected action waits on a human: §14 keeps the tool's
    #: promise pending, and an invocation that has not finished has no terminal
    #: event to name. The one-terminal-event rule is preserved, not relaxed —
    #: the terminal event is written when the decision resolves the invocation.
    terminal_event: str | None
    reported_status: str | None
    reported_summary: str
    error_code: str | None
    duration_ms: int
    #: Independently observed, not reported. Named so the two cannot be confused
    #: by a reader of this object either.
    observed_state_version: str | None
    observed_state_changed: bool
    next_action: Mapping[str, object]
    #: Set only when the invocation paused for consent. The caller renders the
    #: dialog from this and keeps its tool promise pending.
    confirmation: Mapping[str, object] | None = None

    @property
    def awaiting_confirmation(self) -> bool:
        return self.confirmation is not None


class InvocationService:
    """Runs one invocation end to end."""

    def __init__(
        self,
        database: Database,
        registry: AdapterRegistry,
        locks: WorkspaceLocks,
        *,
        clock: Callable[[], datetime] | None = None,
        id_source: Callable[[], str] | None = None,
    ) -> None:
        self._database = database
        self._registry = registry
        self._locks = locks
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_source = id_source or (lambda: new_id("inv"))

    async def invoke(
        self,
        workspace_id: str,
        run_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        tool_identity_hash: str | None = None,
    ) -> InvocationOutcome:
        # 1 — the run must be this workspace's and open to actions.
        async with self._database.reading() as work:
            run = await self._invocable_run(work, workspace_id, run_id)
            # §20.3: the contract's own paths are applied "in addition to
            # defaults", so the policy is read from the contract this run was
            # armed against rather than from whatever is selected now.
            document = await self._contract_document(work, run)
            policy = _redaction_policy_of(document)
            # §14: which actions need a human is a statement about the journey
            # being judged, so it is read from the armed contract rather than
            # from a list of tool names the harness keeps.
            requirement = confirmation_requirement(document, tool_name)
            # FR-008 counts every persisted event, and verification will write
            # one per assertion and per policy. Holding that budget back now is
            # what keeps the ceiling true without ever truncating a verdict.
            reservation = await self._verification_reservation(work, run)
        if requirement is not None:
            # The request and the decision still have to fit inside FR-008's
            # ceiling. A run that could not record the decision it is waiting
            # for would be stranded awaiting a confirmation it can never
            # resolve, which is worse than refusing the action outright.
            reservation += CONFIRMATION_EVENT_RESERVATION
        adapter = self._registry.adapter(str(run["target_adapter_id"]))
        spec = _require_published(adapter, tool_name)

        # Arguments are validated before anything is written: an argument the
        # tool never accepts must not produce a start event claiming otherwise.
        checked = validate_arguments(dict(spec.input_schema), arguments, tool_name=tool_name)

        # A protected tool that already carries a live approval is a *resumed*
        # invocation, not a new one: §14.14 has the invoking page call back after
        # the human decides. It reuses the original correlation and request ids,
        # because FR-060 matches an approval to the mutation it authorized by
        # correlation id, and a fresh id would make the approval look like
        # consent for nothing in particular.
        approved: Mapping[str, Any] | None = None
        if requirement is not None:
            async with self._database.reading() as work:
                approved = await self._live_approval(work, workspace_id, run_id, tool_name)

        invocation_id = self._id_source()
        if approved is not None:
            # Constitution §5 binds a confirmation to "the workspace, run,
            # action, arguments, and expiry". The row already scopes the first
            # three; this is the fourth, checked here — before anything is
            # observed and long before anything is dispatched.
            _require_approved_arguments(approved, checked)
            correlation_id = str(approved["correlation_id"])
            # **No second start event, and no second identity.** This is the
            # *same* invocation, paused for consent and now resumed — §10.3
            # builds the observed trajectory from start events, so writing
            # another would make one logical action appear twice and fail a
            # contract that expected the journey it actually performed.
            #
            # The request id is read back off that start event rather than
            # derived from anything here, for the reason `_resumed_start`
            # records: it is the key the target deduplicates on, so a resumed
            # half that minted its own would present a first attempt for a
            # request the timeline had already named.
            started_sequence, request_id = await self._resumed_start(run_id, correlation_id)
        else:
            correlation_id = invocation_id
            request_id = _target_request_id(spec, arguments, invocation_id)

        # 2 — canonical state before the call. I/O, outside every lock.
        before = await self._observe(adapter, workspace_id, policy)

        # §14.7's revalidation. ADR-0003 held nothing across the human's decision,
        # so the world may have moved; the approval described the state a person
        # was shown, and if that changed their consent no longer describes what
        # is about to happen.
        if approved is not None and binding_hash(before) != str(approved["state_binding_hash"]):
            return await self._stale_approval(workspace_id, run_id, approved, before=before)

        # 3 — reserve, transition, and record the start (FR-031, FR-008). A
        # resumed invocation already did this before it paused, so it only has
        # to be told that nothing is pending for it.
        pending: Mapping[str, Any] | None = None
        if approved is None:
            started_sequence, pending = await self._start_or_trip(
                workspace_id,
                run_id,
                spec,
                invocation_id=invocation_id,
                correlation_id=correlation_id,
                request_id=request_id,
                tool_identity_hash=tool_identity_hash,
                before=before,
                policy=policy,
                arguments=checked,
                verification_reservation=reservation,
                requirement=requirement,
            )

        # A protected action stops here. Nothing is dispatched, so no mutation
        # can precede the consent that authorizes it — which is the property
        # AC-06 checks and the reason this returns before the adapter is
        # touched rather than after.
        if pending is not None:
            return await self._paused(
                workspace_id,
                run_id,
                invocation_id=invocation_id,
                started_sequence=started_sequence,
                before=before,
                pending=pending,
            )

        # FR-066's atomic claim, and the last thing that happens before the
        # adapter is touched. A resume that loses this race is refused here
        # rather than dispatched, so one approval can never stand behind two
        # mutations.
        if approved is not None:
            await self._claim_approval(workspace_id, approved)

        # 4 and 5 — dispatch, then observe immediately. Both may fail; every
        # path below converges on exactly one terminal event.
        result: ToolExecutionResult | None = None
        failure: str | None = None
        started_at = self._clock()
        try:
            result = await adapter.execute(
                workspace_id,
                tool_name,
                checked,
                _context(
                    workspace_id,
                    run_id,
                    invocation_id,
                    request_id,
                    correlation_id,
                    human_consent_granted=approved is not None,
                ),
            )
        except Exception as exc:
            # FR-033: persisted as an error event "without leaking internal
            # details to the agent". The type is recorded; the message is not.
            failure = type(exc).__name__

        after = await self._observe_or_none(adapter, workspace_id, policy)
        duration_ms = max(0, int((self._clock() - started_at).total_seconds() * 1000))

        # 6 — exactly one terminal event.
        return await self._terminate(
            workspace_id,
            run_id,
            spec,
            invocation_id=invocation_id,
            correlation_id=correlation_id,
            request_id=request_id,
            started_sequence=started_sequence,
            before=before,
            after=after,
            result=result,
            failure=failure,
            duration_ms=duration_ms,
            policy=policy,
            arguments=checked,
        )

    # -- steps ---------------------------------------------------------------

    async def _start_or_trip(
        self, *args: Any, **kwargs: Any
    ) -> tuple[int, Mapping[str, Any] | None]:
        """`_start`, with its refusal raised after the commit that recorded it.

        Split out so the raise happens outside the `async with`: raising inside
        would roll back the boundary event that explains the stop, which is the
        bug 004-T8 caught and this must not reintroduce.

        The refusal is *carried out* rather than inferred from `sequence == 0`.
        Two paths now stop a start before it records one — FR-008's event
        ceiling and FR-169's identity mismatch — and inferring the reason from
        the sentinel would report whichever one the code happened to name,
        which is how the identity refusal first surfaced as a ceiling error.
        """
        sequence, pending, refusal = await self._start(*args, **kwargs)
        if refusal is not None:
            raise refusal
        return sequence, pending

    async def _invocable_run(
        self, work: UnitOfWork, workspace_id: str, run_id: str
    ) -> Mapping[str, Any]:
        """FR-036: events belong only to the currently armed run of this workspace."""
        run = await WorkspaceScope(work, workspace_id).run(run_id)
        status = str(run["status"])
        if status in INVOCABLE_RUN_STATES:
            return run
        if status == str(RunState.VERIFYING.value):
            # FR-038's rejection. It "creates no finding and no
            # `tool_execution_error`" — so it is raised here, before any event
            # is written, rather than recorded as a failed invocation.
            raise ApiError(
                ApiErrorCode.RUN_ALREADY_VERIFYING,
                "Verification has started for this run; no further target actions are accepted.",
            )
        raise ApiError(
            ApiErrorCode.RUN_TIMELINE_SEALED,
            f"This run is {status} and accepts no further target actions.",
        )

    async def _contract_document(
        self, work: UnitOfWork, run: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """The contract this run was *armed against*, or `None`.

        Not whatever the workspace has selected now: FR-025 locks the armed
        contract, and reading the current one would judge this run's evidence
        by a rule it was never run under — and, since §14's confirmation
        requirement is read from the same document, would let a contract
        swapped mid-run remove a consent gate.
        """
        contract_id = run.get("contract_id")
        if not contract_id:
            return None
        row = await work.fetch_one(
            "SELECT document_json FROM contracts WHERE id = ?", (str(contract_id),)
        )
        if row is None:  # pragma: no cover - the contract is immutable once armed
            return None
        document: dict[str, Any] = json.loads(row["document_json"])
        return document

    async def _verification_reservation(self, work: UnitOfWork, run: Mapping[str, Any]) -> int:
        """How many events verification will need for this run's contract.

        `verification_started`, the final `snapshot_captured`, one
        `assertion_evaluated` per assertion, one `policy_evaluated` per policy,
        and `verification_completed` (§16.1). Exact rather than a margin,
        because the contract is fixed at arming (FR-012) — a guess would either
        waste budget or fail to prevent the overrun it exists to prevent.
        """
        contract_id = run.get("contract_id")
        if not contract_id:
            return _VERIFICATION_FIXED_EVENTS
        row = await work.fetch_one(
            "SELECT document_json FROM contracts WHERE id = ?", (str(contract_id),)
        )
        if row is None:  # pragma: no cover - the contract is immutable once armed
            return _VERIFICATION_FIXED_EVENTS
        document = json.loads(row["document_json"])
        checks = len(document.get("assertions") or []) + len(document.get("policies") or [])
        return _VERIFICATION_FIXED_EVENTS + checks

    async def _observe(
        self, adapter: Any, workspace_id: str, policy: RedactionPolicy
    ) -> Observation:
        """Capture, then redact before anything is hashed or stored (§20.3)."""
        observation = await adapter.observation_provider().capture(workspace_id)
        return redacted_observation(observation, policy)

    async def _observe_or_none(
        self, adapter: Any, workspace_id: str, policy: RedactionPolicy
    ) -> Observation | None:
        """The post-call read, which is allowed to fail.

        Constitution §5 makes an observation failure an explicit non-pass rather
        than a degradation — but the *invocation* still terminated, and refusing
        to record its terminal event would leave the timeline claiming a call
        that never ended. So the failure is recorded as an absent observation
        and the verdict deals with it.
        """
        try:
            return await self._observe(adapter, workspace_id, policy)
        except Exception:
            # Broad on purpose: an adapter may fail in any way its transport
            # does, and none of those ways should stop the invocation being
            # recorded as terminated. What makes this safe is the counterpart
            # test — an honest mutation must report `state_changed: true`, so a
            # bug that made every observation fail cannot pass silently. It has
            # already caught one.
            #
            # Logged because the *verdict* is the same for every cause — absent
            # observation, explicit non-pass — while the operator's response is
            # not: a target that is down, a target that is misconfigured, and a
            # defect in this adapter are three different problems that reach
            # this line looking identical. The traceback goes to the same
            # server-side channel as an unhandled request failure, never into
            # the response or the structured request line (§21.5).
            _logger.warning(
                "observation failed for workspace %s; recorded as absent",
                workspace_id,
                exc_info=True,
            )
            return None

    async def _identity_mismatch(
        self,
        work: UnitOfWork,
        workspace_id: str,
        run_id: str,
        tool_name: str,
        presented: str | None,
    ) -> dict[str, Any] | None:
        """Whether the presented identity disagrees with the armed baseline.

        `None` in three cases, and each is deliberate:

        * the caller presented no hash — the field is optional in §15.3, and a
          client that cannot compute one must still be able to invoke;
        * no baseline was captured — §16.1 already fails `stable_tool_surface`
          closed for that run, and refusing every invocation as well would make
          an un-instrumented browser unable to use the product at all;
        * the baseline has no entry for this tool — it appeared mid-run, which
          is an `added` delta for the surface policy to judge, not a reason to
          refuse the call the agent is making right now.

        Each of those is a *narrower* claim than "the definitions match". They
        are separated so that adding a fourth is a deliberate act rather than a
        widening of an existing one.
        """
        if presented is None:
            return None
        baseline = await SurfaceService(work, workspace_id).baseline(run_id)
        if baseline is None:
            return None
        entry = baseline.by_name(ToolNamespace.TARGET).get(tool_name)
        if entry is None:
            return None

        expected = entry.identity()
        if expected.identity_hash == presented:
            return None
        return {
            "reason": "the tool definition changed since the run was armed",
            "tool_name": tool_name,
            "expected_identity_hash": expected.identity_hash,
            "presented_identity_hash": presented,
            # The armed definition, so a reader can see what the agent thought
            # it was calling. The current one is not available here — the caller
            # sent a hash, not a definition — which is why FR-167's capture is
            # the other half of this evidence.
            "armed_definition": entry.canonical_document(),
        }

    async def _start(
        self,
        workspace_id: str,
        run_id: str,
        spec: TargetToolSpec,
        *,
        invocation_id: str,
        correlation_id: str,
        request_id: str,
        tool_identity_hash: str | None,
        before: Observation,
        policy: RedactionPolicy,
        arguments: Mapping[str, Any],
        verification_reservation: int,
        requirement: ConfirmationRequirement | None = None,
    ) -> tuple[int, Mapping[str, Any] | None, ApiError | None]:
        """Reserve the budget, open the run, and record the start (FR-031, FR-008).

        The event-budget refusal is *returned* by the ceiling rather than raised,
        because FR-008 requires the boundary event and the run's move to `error`
        to be committed. So it is carried out of the transaction and raised
        after the commit — the same shape 004-T8 established.
        """
        refusal: ApiError | None = None

        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            run = await self._invocable_run(work, workspace_id, run_id)
            refusal = await WorkspaceCeilings(work, workspace_id).trip_if_event_budget_exhausted(
                run_id, reserved=verification_reservation
            )
            if refusal is not None:
                # The transaction still commits: it is carrying the boundary
                # event that explains why this run stopped.
                return 0, None, refusal

            if str(run["status"]) == str(RunState.ARMED.value):
                # §11.5: "Armed --> Running: first target action". Validated
                # through the core's table rather than assumed, so an illegal
                # transition is refused by the one authority on transitions.
                validate_run_transition(RunState.ARMED, RunState.RUNNING)
                await work.execute(
                    "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ?",
                    (str(RunState.RUNNING.value), run_id, workspace_id),
                )
                await GuidanceRecorder(work, workspace_id).transition(
                    await current_guidance(work, workspace_id), run_id=run_id
                )

            # FR-169's pre-invocation identity check. Inside this transaction so
            # the mismatch event and the refusal commit together — a refusal
            # whose evidence did not land would be an accusation with nothing
            # behind it.
            #
            # Refused, not merely recorded. The agent chose this tool from a
            # description that no longer describes it, so dispatching anyway
            # would spend a human's consent on something other than what was
            # consented to. FR-169 additionally requires the policy to fail on
            # this "even if no `toolchange` event was observed", which the
            # recorded event is what makes possible.
            mismatch = await self._identity_mismatch(
                work, workspace_id, run_id, spec.name, tool_identity_hash
            )
            if mismatch is not None:
                await EventRepository(work).append(
                    run_id,
                    {
                        "event_type": str(OutcomeEventType.TOOL_IDENTITY_MISMATCH.value),
                        "actor": str(EventActor.HARNESS.value),
                        "tool_name": spec.name,
                        "tool_identity_hash": tool_identity_hash,
                        "correlation_id": correlation_id,
                        "request_id": request_id,
                        "redacted_payload": mismatch,
                    },
                )
                refusal = ApiError(
                    ApiErrorCode.TOOL_IDENTITY_MISMATCH,
                    "This tool's definition changed since the run was armed.",
                    details=[
                        {
                            "path": "tool_identity_hash",
                            "message": "does not match the armed baseline",
                        }
                    ],
                )
                return 0, None, refusal

            started = await EventRepository(work).append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.TOOL_INVOCATION_STARTED.value),
                    "actor": str(EventActor.AGENT.value),
                    "tool_name": spec.name,
                    "tool_identity_hash": tool_identity_hash,
                    "correlation_id": correlation_id,
                    "request_id": request_id,
                    # Observed, not reported: nothing has been dispatched yet,
                    # so there is no self-report to confuse this with.
                    "state_version_before": before.state_version,
                    "state_hash_before": before.content_hash(),
                    "redacted_payload": {
                        "invocation_id": invocation_id,
                        "side_effect": str(spec.side_effect.value),
                        "retry": str(spec.retry.value),
                        # FR-032's redacted inputs, recorded on the *start*
                        # event: they are what the call was made with, and they
                        # are known before it returns. §20.3 requires the
                        # redaction to happen before persistence, so it happens
                        # here rather than to an already-stored row.
                        "arguments": bounded(redact(dict(arguments), policy)),
                    },
                },
            )

            if requirement is None:
                return started, None, None

            # §14.1: the request is created here, in the *same* transaction as
            # the start event it belongs to. A confirmation without its start
            # event would be consent for an action the timeline never records
            # being attempted; a start event without its confirmation would be
            # an invocation nothing can ever resolve.
            confirmations = ConfirmationService(work, workspace_id)
            expires_at = expiry_from(self._clock(), requirement)
            consequence = consequence_summary(
                tool_name=spec.name,
                arguments=redact(dict(arguments), policy),
                observed=before,
                effect_paths=[str(path) for path in spec.effect_paths],
                policy=policy,
            )
            confirmation_id = await confirmations.open(
                run_id=run_id,
                correlation_id=correlation_id,
                tool_name=spec.name,
                # Bound to what was independently observed, never to what a
                # tool said: an approval is consent about the world as it is.
                state_binding_hash=binding_hash(before),
                # And bound to the exact arguments the request was raised for.
                # The observed state alone cannot tell one checkout from
                # another that would leave the same cart behind, so without
                # this an approval shown for one set of inputs would authorize
                # any other set (constitution §5). Hashed from the *validated*
                # arguments, which are the ones that will be dispatched — the
                # redacted copy above is what a person reads, and redaction is
                # lossy on purpose.
                arguments_hash=arguments_hash(arguments),
                consequence=consequence,
                expires_at=expires_at,
            )

            # §11.5: `Running --> AwaitingConfirmation`. Through the core's
            # table, so the one authority on transitions approves it.
            validate_run_transition(RunState.RUNNING, RunState.AWAITING_CONFIRMATION)
            await work.execute(
                "UPDATE runs SET status = ? WHERE id = ? AND workspace_id = ?",
                (str(RunState.AWAITING_CONFIRMATION.value), run_id, workspace_id),
            )

            await EventRepository(work).append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.CONFIRMATION_REQUESTED.value),
                    # The *agent* asked; the human decides. Recording the
                    # requester as the human would make the timeline say
                    # somebody consented to being asked.
                    "actor": str(EventActor.AGENT.value),
                    "tool_name": spec.name,
                    "correlation_id": correlation_id,
                    "request_id": request_id,
                    "status": str(ConfirmationStatus.PENDING.value),
                    "state_version_before": before.state_version,
                    "state_hash_before": before.content_hash(),
                    "redacted_payload": {
                        "confirmation_id": confirmation_id,
                        "expires_at": expires_at.isoformat(),
                        "timeout_seconds": requirement.timeout_seconds,
                        "consequence": consequence,
                    },
                },
            )

            # FR-120: the active actor is now the human approver, and the
            # banner, the tool result, and the action history all read this one
            # projection rather than each deciding for themselves.
            await GuidanceRecorder(work, workspace_id).transition(
                await current_guidance(work, workspace_id), run_id=run_id
            )

            return (
                started,
                {
                    "confirmation_id": confirmation_id,
                    "expires_at": expires_at.isoformat(),
                    "consequence": consequence,
                    "correlation_id": correlation_id,
                    "tool_name": spec.name,
                },
                None,
            )

    async def _resumed_start(self, run_id: str, correlation_id: str) -> tuple[int, str]:
        """The sequence and request id of the start event this invocation wrote.

        A paused invocation recorded its start before asking for consent, so
        the resumed half is a continuation rather than a new call. Reusing that
        sequence keeps the timeline's one-start-one-terminal shape intact.

        Reusing that `request_id` keeps the *idempotency key* intact across the
        pause, which matters more. It is the key the target deduplicates on
        (`_target_request_id`), so a resumed half that derived its own would
        dispatch under an identifier the target had never seen and record a
        terminal event naming a request its own start event never made — three
        different answers to "which request was this?" for one logical action.

        A missing start event is raised rather than papered over. It and the
        confirmation row are written in one transaction, so an approval without
        one means the database disagrees with itself; returning a sequence of
        zero and a fabricated identifier would point both a reader and the
        target at a request that never happened, which is worse than stopping.
        """
        async with self._database.reading() as work:
            row = await work.fetch_one(
                "SELECT sequence_number, request_id FROM events WHERE run_id = ? "
                "AND correlation_id = ? AND event_type = ? ORDER BY sequence_number LIMIT 1",
                (
                    run_id,
                    correlation_id,
                    str(OutcomeEventType.TOOL_INVOCATION_STARTED.value),
                ),
            )
        if row is None or not row["request_id"]:
            # §15.8 keeps internal detail out of a client's hands, so the code
            # is the whole of what the caller learns.
            raise ApiError(
                ApiErrorCode.HARNESS_ERROR, "The harness could not complete the request."
            )
        return int(row["sequence_number"]), str(row["request_id"])

    async def _live_approval(
        self, work: UnitOfWork, workspace_id: str, run_id: str, tool_name: str
    ) -> Mapping[str, Any] | None:
        """An approval this run holds that has not been spent yet (FR-066).

        `approved` and not `consumed`: an approval is spent by the mutation it
        authorized and can never authorize a second one, which is what makes
        "approve once" true rather than aspirational.

        This read finds a *candidate*, not a claim. Two resumes can both see the
        same row here and both be right about what they saw; the row is won by
        `_claim_approval` immediately before dispatch, and everything between
        the two is a check that may still refuse.
        """
        row = await work.fetch_one(
            """
            SELECT * FROM confirmation_requests
             WHERE workspace_id = ? AND run_id = ? AND tool_name = ? AND status = ?
             ORDER BY decided_at DESC, id DESC LIMIT 1
            """,
            (workspace_id, run_id, tool_name, str(ConfirmationStatus.APPROVED.value)),
        )
        return None if row is None else dict(row)

    async def _claim_approval(self, workspace_id: str, approved: Mapping[str, Any]) -> None:
        """Win the approval, or refuse the invocation (FR-066).

        FR-066 wants the approval "atomically validate[d] and consume[d] in the
        same transaction as its mutation". The mutation is remote I/O and
        ADR-0003 forbids a transaction spanning it, so the atomic part is
        pulled forward into one bounded transaction of its own: a single
        conditional update whose `status = 'approved'` predicate is the
        mechanism rather than a guard. Every concurrent resume reaches it, and
        exactly one can match a row.

        ADR-0003's rule about locks is respected rather than bent. This holds
        the workspace lock only for the length of that one statement, and
        releases it before the adapter is called — nothing is held across the
        dispatch.

        Losing is not a fault in the caller and not a transport failure: the
        approval was spent by the resume that won, and this one is refused with
        a rejected-intent code so no client reads it as safe to repeat. A retry
        cannot succeed, because there is no consent left for it to spend; the
        way forward is to ask for the action again and let a human decide again.
        """
        confirmation_id = str(approved["id"])
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            claimed = await ConfirmationService(work, workspace_id).claim_approved(confirmation_id)
        if not claimed:
            raise _approval_already_spent()

    async def _stale_approval(
        self,
        workspace_id: str,
        run_id: str,
        approved: Mapping[str, Any],
        *,
        before: Observation,
    ) -> InvocationOutcome:
        """The approved state moved before the approval was spent (§14.7).

        Fails closed and *cancels* the approval rather than leaving it live. A
        human approved a particular cart; carrying that consent forward onto a
        different one is precisely the replay the binding hash exists to
        prevent, and leaving it pending would let the next attempt succeed
        against state nobody agreed to.

        Nothing is dispatched, so no mutation occurs.

        **The cancellation is recorded only if it actually happened.** The
        `status = 'approved'` predicate can match nothing, and there is one
        ordinary way for it to: a concurrent resume claimed the approval and its
        mutation is what moved the state this one just observed. That approval
        was spent, not cancelled, and appending "cancelled because the state
        changed after approval" to an append-only evidence chain would put a
        false account of a human's consent into the record permanently. So the
        losing resume is refused exactly as a lost claim is refused, and the
        timeline keeps one true story about where the consent went.
        """
        confirmation_id = str(approved["id"])
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            cursor = await work.execute(
                "UPDATE confirmation_requests SET status = ?, decided_at = ? "
                "WHERE id = ? AND workspace_id = ? AND status = ?",
                (
                    str(ConfirmationStatus.CANCELLED.value),
                    work.now(),
                    confirmation_id,
                    workspace_id,
                    str(ConfirmationStatus.APPROVED.value),
                ),
            )
            if cursor.rowcount != 1:
                # Raised inside the transaction on purpose: it rolls back, and
                # there is nothing here worth committing — no cancellation
                # occurred and no event describes one.
                raise _approval_already_spent()
            await EventRepository(work).append(
                run_id,
                {
                    "event_type": str(OutcomeEventType.CONFIRMATION_CANCELLED.value),
                    "actor": str(EventActor.HARNESS.value),
                    "tool_name": str(approved["tool_name"]),
                    "correlation_id": str(approved["correlation_id"]),
                    "status": str(ConfirmationStatus.CANCELLED.value),
                    "state_hash_before": before.content_hash(),
                    "redacted_payload": {
                        "confirmation_id": confirmation_id,
                        "reason": "state_changed_after_approval",
                        "mutated": False,
                    },
                },
            )
            guidance = await current_guidance(work, workspace_id)

        return InvocationOutcome(
            invocation_id=confirmation_id,
            sequence_number=0,
            terminal_event=str(OutcomeEventType.CONFIRMATION_CANCELLED.value),
            reported_status=None,
            reported_summary=(
                "The approved state changed before the action ran, so the approval was "
                "cancelled and nothing was done. Ask for the action again to review the "
                "current state."
            ),
            error_code="stale_approval",
            duration_ms=0,
            observed_state_version=before.state_version,
            observed_state_changed=False,
            next_action=guidance.next_action(),
        )

    async def _paused(
        self,
        workspace_id: str,
        run_id: str,
        *,
        invocation_id: str,
        started_sequence: int,
        before: Observation,
        pending: Mapping[str, Any],
    ) -> InvocationOutcome:
        """What the agent is told while a human decides.

        Deliberately *not* an error. The action has not failed and has not
        succeeded; it is waiting, and §14.3 keeps the caller's tool promise
        pending. Reporting a failure here would teach an agent to retry, which
        is exactly the behaviour a consent gate exists to prevent.
        """
        async with self._database.reading() as work:
            guidance = await current_guidance(work, workspace_id)
        return InvocationOutcome(
            invocation_id=invocation_id,
            sequence_number=started_sequence,
            terminal_event=None,
            reported_status=None,
            reported_summary=(
                f"{pending['tool_name']} is paused for a human decision. No change has been made."
            ),
            error_code=None,
            duration_ms=0,
            observed_state_version=before.state_version,
            observed_state_changed=False,
            next_action=guidance.next_action(),
            confirmation=dict(pending),
        )

    async def _terminate(
        self,
        workspace_id: str,
        run_id: str,
        spec: TargetToolSpec,
        *,
        invocation_id: str,
        correlation_id: str,
        request_id: str,
        started_sequence: int,
        before: Observation,
        after: Observation | None,
        result: ToolExecutionResult | None,
        failure: str | None,
        duration_ms: int,
        policy: RedactionPolicy,
        arguments: Mapping[str, Any],
    ) -> InvocationOutcome:
        terminal = (
            OutcomeEventType.TOOL_INVOCATION_FAILED if result is None else result.terminal_event
        )
        observed_changed = after is not None and after.content_hash() != before.content_hash()

        payload: dict[str, Any] = {
            "invocation_id": invocation_id,
            "side_effect": str(spec.side_effect.value),
            "started_at_sequence": started_sequence,
            # The self-report, kept together and labelled. Everything under this
            # key is what the tool said about itself; nothing under it is
            # evidence of what happened (constitution §4).
            "reported": _reported(result, failure, policy),
            # The engine's view of the same reading: shaped as an evaluation
            # context so FR-055 can resolve an assertion's own path against it
            # (`RunEvent.post_call_effect_state`). Stored alongside the audit
            # view rather than instead of it — one is read by a person, the
            # other by the classifier.
            "post_call_effect_state": effect_context(
                spec.effect_paths,
                None if after is None else after.as_context(),
                policy=policy,
            ),
            # FR-032's declared target-effect evidence, so idempotency and
            # false-success evidence "do not depend on tool-return text or later
            # actions". An adapter that declares no effect paths gets an empty
            # mapping rather than a guess (§12.2).
            "effects": effect_evidence(
                spec.effect_paths,
                before=before.as_context(),
                after=None if after is None else after.as_context(),
                policy=policy,
            ),
            # The independent read. `observed` and `reported` are siblings so a
            # reader cannot mistake one for the other.
            "observed": {
                "available": after is not None,
                "state_changed": observed_changed,
            },
        }

        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            sequence = await EventRepository(work).append(
                run_id,
                {
                    "event_type": str(terminal.value),
                    "actor": str(EventActor.AGENT.value),
                    "tool_name": spec.name,
                    "correlation_id": correlation_id,
                    "request_id": request_id,
                    "status": str(terminal.value),
                    # FR-032: required on a completion, absent on the failed and
                    # cancelled event types, which carry their outcome in the
                    # event name.
                    "reported_status": (
                        None
                        if result is None or result.reported_status is None
                        else str(result.reported_status.value)
                    ),
                    "duration_ms": duration_ms,
                    # "canonical state_version_before and state_version_after" —
                    # canonical means observed. The tool's own claim lives in
                    # `reported` above and never in these columns.
                    "state_version_before": before.state_version,
                    "state_hash_before": before.content_hash(),
                    "state_version_after": None if after is None else after.state_version,
                    "state_hash_after": None if after is None else after.content_hash(),
                    "redacted_payload": payload,
                },
            )
            # **No approval is spent here.** It was claimed before dispatch, by
            # `_claim_approval`, because that is the only ordering under which
            # two concurrent resumes cannot both reach the adapter. Spending it
            # in this transaction read well — the approval and the mutation it
            # authorized committing together — but the read that found the
            # approval was minutes and one network round trip earlier, and
            # nothing stopped a second resume finding the same row in between.

            # FR-121's compact `next_action`, derived from the workspace as it
            # now stands rather than from the phase this handler assumed. An
            # invocation that tripped the event ceiling leaves the run in
            # `error`, and telling the caller to carry on invoking would be a
            # server inventing a next action no state supports.
            guidance = await current_guidance(work, workspace_id)

        return InvocationOutcome(
            invocation_id=invocation_id,
            sequence_number=sequence,
            terminal_event=str(terminal.value),
            reported_status=(
                None
                if result is None or result.reported_status is None
                else str(result.reported_status.value)
            ),
            reported_summary="" if result is None else result.reported_summary,
            error_code=failure if result is None else result.error_code,
            duration_ms=duration_ms,
            observed_state_version=None if after is None else after.state_version,
            observed_state_changed=observed_changed,
            next_action=guidance.next_action(),
        )


def _reported(
    result: ToolExecutionResult | None,
    failure: str | None,
    policy: RedactionPolicy | None = None,
) -> dict[str, Any]:
    """The tool-reported channel, serialized under one key.

    A failure that never produced a result still belongs here: what the harness
    knows is that the call did not come back, and that is a fact about the tool
    channel rather than about the target's state.
    """
    if result is None:
        return {
            "status": None,
            "summary": "",
            # The exception's *type*, never its message (FR-033, §20).
            "error_code": failure,
            "state_version_after": None,
        }
    return {
        "status": None if result.reported_status is None else str(result.reported_status.value),
        # Bounded and redacted: §23.3 keeps the tool's own text out of storage
        # at full length, and a self-report is as likely to carry a secret as
        # any other untrusted string.
        "summary": bounded(redact(result.reported_summary, policy), limit=MAX_TOOL_RESULT_CHARS),
        "error_code": result.error_code,
        # Deliberately kept: the version the tool *claimed*, beside the version
        # that was observed. A disagreement between them is evidence.
        "state_version_after": result.state_version_after,
    }


def _approval_already_spent() -> ApiError:
    """The one refusal for "somebody else got there first".

    Both places that can lose an approval — the claim before dispatch and the
    stale-state cancellation — raise this same code, because from the caller's
    side they are one fact: the consent this resume was going to use is gone.
    Splitting them would ask a client to tell apart two race outcomes it cannot
    act on differently.

    Not retryable, and the registry is what says so. Repeating the request
    cannot succeed: there is no consent left to spend, and the only way forward
    is to ask for the action again so a human decides again.
    """
    return ApiError(
        ApiErrorCode.PRECONDITION_FAILED,
        "This approval has already been spent, so nothing was done. Ask for the action "
        "again if it is still wanted, and a human will be asked afresh.",
        details=[{"path": "confirmation_id", "message": "the approval is already spent"}],
    )


def _require_approved_arguments(approved: Mapping[str, Any], arguments: Mapping[str, Any]) -> None:
    """Refuse a resume whose arguments are not the ones a human approved.

    Constitution §5 binds a confirmation to "the workspace, run, action,
    arguments, and expiry". The row's workspace, run, and tool columns scope the
    first three; `state_binding_hash` covers the world the action was described
    against and, by design, nothing else — `binding_hash` hashes the observation
    payload alone. So the arguments were the one part of the binding nothing
    checked, and an approval shown for one set of inputs authorized every other
    set that left the observed state looking the same: one person's consent
    replayed onto an action they were never shown.

    **A stored `NULL` is refused, not accepted.** It means the row predates the
    column that records what was approved, so nobody can say what the human saw.
    Treating an unknown binding as a matching one would make the rail weakest
    exactly where its evidence is missing, and §5 makes an ambiguity an explicit
    non-pass rather than a degradation to success.

    The approval is left live rather than cancelled, which is the opposite of
    what `_stale_approval` does and deliberately so. There, the world moved and
    the consent no longer describes anything; here, the consent still describes
    perfectly well the arguments a person actually read. Burning it would punish
    the human for the agent's substitution.
    """
    recorded = approved.get("arguments_hash")
    if not isinstance(recorded, str) or not recorded:
        raise ApiError(
            ApiErrorCode.PRECONDITION_FAILED,
            "This approval does not record which arguments were approved, so it cannot "
            "authorize any. Ask for the action again so a human is shown what would happen.",
            details=[{"path": "arguments", "message": "the approval records no arguments"}],
        )
    if arguments_hash(arguments) != recorded:
        raise ApiError(
            ApiErrorCode.PRECONDITION_FAILED,
            "These are not the arguments a human approved, so nothing was done. Ask for "
            "the action again with the arguments that should be shown.",
            details=[{"path": "arguments", "message": "does not match the approved arguments"}],
        )


def _redaction_policy_of(document: Mapping[str, Any] | None) -> RedactionPolicy:
    """The run's redaction policy: defaults plus the contract's own paths.

    A run without a contract still gets the defaults. §20.3 applies contract
    paths "in addition to defaults", so there is no configuration that turns
    redaction off.
    """
    if not document:
        return RedactionPolicy()
    paths = ((document.get("redaction") or {}).get("paths")) or []
    return RedactionPolicy.from_paths([str(path) for path in paths])


def _require_published(adapter: Any, tool_name: str) -> TargetToolSpec:
    """The allowlist, checked before anything else (§20.2, FR-015).

    A name the adapter never published is a 404 rather than a validation error:
    the tool does not exist for this caller, and saying anything more precise
    would describe a surface they were not shown.
    """
    for spec in adapter.tool_specs():
        if spec.name == tool_name:
            return spec
    raise not_found()


def _context(
    workspace_id: str,
    run_id: str,
    invocation_id: str,
    request_id: str,
    correlation_id: str,
    *,
    human_consent_granted: bool = False,
) -> Any:
    from actionwitness_core.ports.models import ExecutionContext

    return ExecutionContext(
        workspace_id=workspace_id,
        run_id=run_id,
        invocation_id=invocation_id,
        request_id=request_id,
        correlation_id=correlation_id,
        # Constitution §5 gives every logical mutation a stable key. One
        # invocation is one logical intent, so the invocation's own identity is
        # that key — a retry of the same intent reuses it by reusing the id.
        idempotency_key=invocation_id,
        actor=EventActor.AGENT,
        human_consent_granted=human_consent_granted,
    )


def _target_request_id(
    spec: TargetToolSpec, arguments: Mapping[str, Any], invocation_id: str
) -> str:
    """The idempotency key this call actually presents to the target.

    A tool whose retry semantics are `idempotent_by_request_id` carries the key
    in its own arguments, and that is the key the *target* deduplicates on. The
    event has to record the same one, because FR-063 judges "repeating one
    request ID" against canonical state — and a harness that recorded a fresh
    identifier per call would make every repeat look like a first attempt, so
    `idempotent_by_request_id` could never fail however the target behaved.

    It also keeps a live run and its replay consistent: 007's replayer already
    prefers the recorded argument, so without this a case cut from a live run
    would classify differently from the run it was cut from — which AC-15
    forbids.

    Every other tool keeps the generated identifier. Those declare no caller
    key, and inventing a shared one would make unrelated calls look like
    retries of each other.
    """
    if spec.retry is RetrySemantics.IDEMPOTENT_BY_REQUEST_ID:
        supplied = arguments.get("request_id")
        if isinstance(supplied, str) and supplied:
            return supplied
    return f"req_{invocation_id}"
