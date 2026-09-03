"""ActionWitness as its own `ManagedTargetAdapter` (§12.20, FR-171).

The dogfooding target. It drives the harness's own capabilities and observes the
harness's own canonical workspace state, and it does both the way a stranger
would: over `/api/v1`, holding nothing but an injected HTTP client.

**No privileged access, structurally.** FR-171 says a built-in target "shall not
receive privileged access unavailable to a third-party adapter; anything it
needs is a defect in the public protocol and shall be fixed there." This
distribution therefore depends on `actionwitness-core` and `httpx` and nothing
else — it cannot import a repository, a service, or the database even by
accident. The one thing it genuinely needed and the protocol lacked — saying
*which* workspace to observe, separately from the one being recorded — was
fixed in the protocol, as `ScopedObservationProvider`.

**The observed workspace is addressed by its own identifier.** That identifier
is the workspace cookie's value, which is the bearer credential the harness
already issues; presenting it is what a browser does. There is no back channel
and no impersonation: a caller who does not hold the identifier cannot reach the
workspace, which is exactly the isolation rule the rest of the product runs on.

**Why `prepare` cannot restore.** A `ManagedTargetAdapter` promises restoration
so replay works, and the honest answer here is that a workspace's history is
append-only evidence (constitution §4) — there is no public operation that
rewinds one, and there should not be. So `prepare` resets the observed workspace
to its ready state and says so; a self run is reproducible from that starting
point rather than from an arbitrary restored fixture.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Final

import httpx
from actionwitness_core.evidence.enums import ToolReportedStatus
from actionwitness_core.journeys.enums import OutcomeEventType
from actionwitness_core.ports.enums import ExecutionMode
from actionwitness_core.ports.models import (
    ExecutionContext,
    ScenarioSelection,
    TargetDescriptor,
    TargetToolSpec,
    ToolExecutionResult,
)
from actionwitness_core.security.limits import MAX_TOOL_RESULT_CHARS, bounded_summary
from integrations.self_target.observation import (
    SelfObservationProvider,
    workspace_header,
)
from integrations.self_target.tools import (
    ARM_OUTCOME_CONTRACT,
    EFFECT_MAP,
    GET_OUTCOME_CONTRACT,
    GET_RUN_FINDINGS,
    GET_WORKSPACE_STATUS,
    LIST_CONTRACT_TEMPLATES,
    RESET_WORKSPACE,
    TOOL_NAMES,
    TOOL_SPECS,
    published_names,
    spec_for,
)

__all__ = [
    "ADAPTER_ID",
    "DESCRIPTOR",
    "TARGET_ID",
    "SelfTargetAdapter",
    "UnboundSelfTarget",
    "UnknownSelfTool",
]

TARGET_ID: Final = "self"
ADAPTER_ID: Final = "integrations.self_target.adapter"

#: §9.1's descriptor.
#:
#: One scenario mode, and it is `current` rather than a `pre_fix`/`post_fix`
#: pair: the harness injects no defect into itself, so advertising a pair it
#: cannot switch between would put a control on the configuration panel that
#: changed nothing. §9.1 lets the core compare these tokens without interpreting
#: them, which is exactly why the claim has to be true — nothing downstream can
#: check it.
DESCRIPTOR: Final = TargetDescriptor(
    target_type="self",
    target_id=TARGET_ID,
    execution_mode=ExecutionMode.MANAGED,
    supported_scenario_modes=("current",),
    # `("none",)` rather than `()`, and the difference is load-bearing. An empty
    # tuple means "this adapter advertises nothing", which `TargetDescriptor.
    # injects` reads as making no claim and therefore permits *every* profile —
    # the right default for an external target that has no concept of fault
    # injection, and the wrong answer here. The harness injects no defect into
    # itself, so a self run armed with `discount_reported_but_not_applied` would
    # produce a report naming an active fault nothing produced. Naming the one
    # profile it does support turns silence into a statement.
    supported_fault_profiles=("none",),
)

#: Each tool's `/api/v1` route, relative to the observed workspace.
_ROUTES: Final[Mapping[str, tuple[str, str]]] = {
    GET_WORKSPACE_STATUS: ("GET", "/api/v1/workspace"),
    LIST_CONTRACT_TEMPLATES: ("GET", "/api/v1/contracts/templates"),
    GET_OUTCOME_CONTRACT: ("GET", "/api/v1/workspace"),
    GET_RUN_FINDINGS: ("GET", "/api/v1/workspace"),
    ARM_OUTCOME_CONTRACT: ("POST", "/api/v1/runs"),
    RESET_WORKSPACE: ("POST", "/api/v1/workspace/reset"),
}


class UnknownSelfTool(ValueError):
    """A tool name outside the published allowlist."""


class UnboundSelfTarget(RuntimeError):
    """The adapter was asked to act without being told which workspace to act on."""


class SelfTargetAdapter:
    """Drives and observes ActionWitness over its own versioned HTTP API."""

    descriptor = DESCRIPTOR
    adapter_id = ADAPTER_ID

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] | None = None,
        observed_workspace_id: str | None = None,
    ) -> None:
        #: ADR-0001: injected, never constructed here.
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._observations = SelfObservationProvider(client, clock=clock)
        #: The workspace this adapter acts on, or `None` until the server binds
        #: one. `None` is the safe starting state and stays refusable: an
        #: unbound adapter cannot act at all, so forgetting to bind produces a
        #: refusal rather than a call against whatever workspace was nearest.
        self._observed = observed_workspace_id

    # -- ScopedTargetAdapter -------------------------------------------------

    def observing(self, observed_workspace_id: str) -> SelfTargetAdapter:
        """This adapter, bound to act on one workspace (FR-172).

        A new instance sharing the client, never a mutation of this one. The
        client is owned by the composition root and shared across every request
        in the process; an adapter that remembered the last workspace it was
        pointed at would let two concurrent self runs act on each other's
        target — a cross-workspace mutation, which constitution §5 forbids
        outright.
        """
        return SelfTargetAdapter(
            self._client, clock=self._clock, observed_workspace_id=observed_workspace_id
        )

    # -- TargetAdapter -------------------------------------------------------

    def tool_specs(self) -> Sequence[TargetToolSpec]:
        return TOOL_SPECS

    def effect_map(self) -> Mapping[str, tuple[str, ...]]:
        return EFFECT_MAP

    def observation_provider(self) -> SelfObservationProvider:
        return self._observations

    # -- ManagedTargetAdapter ------------------------------------------------

    async def prepare(self, workspace_id: str, fixture: dict, scenario: ScenarioSelection) -> None:
        """Return the observed workspace to its ready state.

        Not a restore, and the difference is stated rather than hidden: a
        workspace's timeline is append-only evidence, so there is no public
        operation that rewinds one and this adapter does not pretend to have a
        private one. `fixture` names the observed workspace; anything else in it
        is ignored, because a fixture that claimed to reconstruct a history
        would be claiming something the harness cannot do.
        """
        observed = self._bound_workspace()
        response = await self._client.post(
            "/api/v1/workspace/reset", headers=workspace_header(observed)
        )
        response.raise_for_status()

    def _bound_workspace(self) -> str:
        """The workspace the server bound this adapter to.

        Refuses rather than defaulting. There is exactly one workspace it could
        plausibly fall back to — the one recording the run — and that fallback
        is the observer loop FR-172 exists to prevent, so it must be
        unreachable rather than merely unlikely.

        Note what this method is *not* reading: the tool's arguments. Those come
        from the agent under test, and an agent that could name its own
        recording workspace could drive the run observing it.
        """
        if self._observed is None:
            raise UnboundSelfTarget(
                "the self target must be bound to an observed workspace before it acts; "
                "it never falls back to the workspace recording the run"
            )
        return self._observed

    async def execute(
        self, workspace_id: str, tool_name: str, arguments: dict, context: ExecutionContext
    ) -> ToolExecutionResult:
        """Call one published harness capability against the observed workspace.

        Whatever it answers is a *report*. The verdict comes from the separate
        read in `observation.py`, and nothing here ever builds an `Observation`
        from this response — that separation is the product (constitution §4).
        """
        spec = spec_for(tool_name)
        if spec is None:
            raise UnknownSelfTool(f"{tool_name!r} is not published by the self target")

        observed = self._bound_workspace()
        method, path = _ROUTES[tool_name]

        started = self._clock()
        response = await self._client.request(method, path, headers=workspace_header(observed))
        # Raised, not reported. A transport failure or an HTTP error says the
        # call did not happen; the invocation path classifies that as
        # `tool_execution_error`, and `ToolReportedStatus` deliberately has no
        # "failed" member because a *report* is what a tool said about work it
        # did — not the absence of a report.
        response.raise_for_status()

        return ToolExecutionResult(
            tool_name=tool_name,
            terminal_event=OutcomeEventType.TOOL_INVOCATION_COMPLETED,
            reported_status=ToolReportedStatus.SUCCESS,
            # The response body, bounded. Deliberately a *summary* and not the
            # payload: §23.3 keeps the tool-context budget bounded, and a caller
            # handed a full body starts reading a self-report as state — which
            # is the substitution this product exists to catch.
            reported_summary=bounded_summary(response.text, MAX_TOOL_RESULT_CHARS).text,
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            duration_ms=_elapsed_ms(started, self._clock()),
            # No state version. §15.1's workspace response publishes none, and
            # inventing one would let FR-032's change detection claim state moved
            # on this adapter's authority rather than the harness's.
            state_version_before=None,
            state_version_after=None,
        )


def _elapsed_ms(started: datetime, finished: datetime) -> int:
    """Wall-clock milliseconds, floored at zero.

    From the injected clock rather than `perf_counter`, so a frozen clock
    reports `0` and a replayed run measures the same duration it did the first
    time (constitution §1). Clamped because a clock a test moves backwards
    should produce a boring number, not a validation error deep in the run.
    """
    return max(0, int((finished - started).total_seconds() * 1000))


def tool_names() -> tuple[str, ...]:
    return TOOL_NAMES


def names() -> tuple[str, ...]:
    return published_names()
