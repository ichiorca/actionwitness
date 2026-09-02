"""actionwitness_core.ports — the public protocols a target or application implements.

These are the extension surface promised by spec v1.9 §29.2 ("documented
ManagedTargetAdapter/ExternalTargetAdapter/ObservationProvider protocol") and the
one-way boundary enforced by §26.7 / LD-7-8: the core talks ONLY to these
protocols; it never imports an integration, demo, or commerce module.

The signatures follow §9.1 verbatim, including its `async def`. That is not a
break with the constitution's synchronous core: these methods are *implemented*
by the application and the integrations, which own the I/O, and the core neither
defines nor awaits a coroutine of its own. Declaring them synchronously here
would make it impossible for an adapter that speaks HTTP to satisfy the protocol
at all.

Repository protocols are shaped by §17.1's two structural rules rather than by an
invented CRUD vocabulary: snapshots, events, findings, and contracts are
insert-only or append-only, and §17.1 says of snapshots that "the repository
exposes no update method for this table". So none is declared here, and
`tests/adapters` fails if one appears. Method *names* are project-allocated;
the absence of mutation is normative.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from actionwitness_core.contracts.models import ContractRecord
from actionwitness_core.journeys.enums import SnapshotPhase
from actionwitness_core.ports.models import (
    ExecutionContext,
    Observation,
    ScenarioSelection,
    ScenarioState,
    TargetDescriptor,
    TargetToolSpec,
    ToolExecutionResult,
)

__all__ = [
    "ContractRepository",
    "EventRepository",
    "ExternalTargetAdapter",
    "FindingRepository",
    "ManagedTargetAdapter",
    "ObservationProvider",
    "ScenarioReportingAdapter",
    "SnapshotRepository",
    "TargetAdapter",
    "UnitOfWork",
]


@runtime_checkable
class ObservationProvider(Protocol):
    """A named, trusted source of authoritative business-state observations (§9.3).

    The one source an assertion verdict may rest on (FR-044). An implementation
    that derived its payload from a tool's response would satisfy the type and
    defeat the product, which is why `Observation` and `ToolExecutionResult` are
    unrelated types with no conversion between them.
    """

    async def capture(self, workspace_id: str) -> Observation:
        """Capture authoritative state for one workspace."""
        ...


@runtime_checkable
class TargetAdapter(Protocol):
    """What every target publishes, however it is executed (§9.1)."""

    descriptor: TargetDescriptor

    def tool_specs(self) -> Sequence[TargetToolSpec]:
        """The allowlisted tools this target publishes."""
        ...

    def effect_map(self) -> Mapping[str, tuple[str, ...]]:
        """Declared target-effect path prefixes per tool (§13.4).

        An adapter may return an empty map. That costs it causal false-success
        attribution and nothing else: §12.2 forbids the harness from inferring an
        effect it was not told about.
        """
        ...

    def observation_provider(self) -> ObservationProvider:
        """The provider whose observations settle this target's assertions."""
        ...


@runtime_checkable
class ManagedTargetAdapter(TargetAdapter, Protocol):
    """A target the harness can drive AND restore (§9.1).

    Restoration is what makes replay possible, so the same adapter serves the
    browser path and the eval path (BUILD_ORDER invariant 3) and neither imports
    the target's own service objects.
    """

    async def prepare(self, workspace_id: str, fixture: dict, scenario: ScenarioSelection) -> None:
        """Restore `fixture` and select `scenario` for this workspace."""
        ...

    async def execute(
        self, workspace_id: str, tool_name: str, arguments: dict, context: ExecutionContext
    ) -> ToolExecutionResult:
        """Execute one allowlisted tool and return what it reported."""
        ...


@runtime_checkable
class ScenarioReportingAdapter(TargetAdapter, Protocol):
    """A target that can say whether its injected defect is currently on (§23.1).

    Deliberately narrow, and deliberately separate from `ManagedTargetAdapter`.
    Most targets inject nothing and have nothing to report; requiring the method
    of every adapter would mean writing a stub that answers `False` for targets
    where the question is meaningless, and a stub answering `False` is
    indistinguishable from a real answer.

    So the capability is asked for rather than assumed — `isinstance` against
    this runtime-checkable protocol — and an adapter that advertises fault
    profiles but cannot answer is refused at arming rather than defaulted. That
    refusal is the §16.1 shape: a run whose report would name an active defect
    nobody confirmed is worse than a run that does not start.
    """

    async def scenario_state(self, workspace_id: str) -> ScenarioState:
        """Report the scenario this target is running for `workspace_id`."""
        ...


@runtime_checkable
class ExternalTargetAdapter(TargetAdapter, Protocol):
    """A target the harness observes but never drives (§9.1, §12.12).

    It has no `execute`: an `external_webmcp` target runs its own tools, and
    §9.1 forbids the adapter from impersonating them "through a second
    implementation". Observations arrive through a bridge and are normalized here.
    """

    def normalize(self, payload: dict, provenance: str) -> Observation:
        """Validate and normalize a submitted payload into an observation."""
        ...

    def validate_origin(self, origin: str) -> None:
        """Refuse any origin but the exact configured one."""
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """One serialized workspace transaction (§17, ADR-0003).

    Declared so the core can *describe* a transactional boundary without owning
    one. No transaction may stay open across browser I/O or human confirmation
    (BUILD_ORDER invariant 7), which is a rule about how the application calls
    this, not something the protocol can enforce.
    """

    async def __aenter__(self) -> UnitOfWork: ...

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> bool | None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


@runtime_checkable
class ContractRepository(Protocol):
    """Insert-only contract storage (§17.1: "there is no update operation")."""

    async def add(self, record: ContractRecord) -> None: ...

    async def get(self, workspace_id: str, contract_id: str) -> ContractRecord | None: ...


@runtime_checkable
class SnapshotRepository(Protocol):
    """Insert-only snapshot storage (§17.1, FR-043)."""

    async def add(self, run_id: str, phase: SnapshotPhase, observation: Observation) -> None: ...

    async def get(self, run_id: str, phase: SnapshotPhase) -> Observation | None: ...


@runtime_checkable
class EventRepository(Protocol):
    """Append-only run timeline with monotonic sequencing (§16.1, FR-034)."""

    async def append(self, run_id: str, event: Mapping[str, object]) -> int: ...

    async def list_after(
        self, run_id: str, after_sequence: int, limit: int
    ) -> Sequence[Mapping[str, object]]: ...


@runtime_checkable
class FindingRepository(Protocol):
    """Insert-only findings for one terminal run (§17.1)."""

    async def add_all(self, run_id: str, findings: Sequence[Mapping[str, object]]) -> None: ...

    async def list_for_run(self, run_id: str) -> Sequence[Mapping[str, object]]: ...
