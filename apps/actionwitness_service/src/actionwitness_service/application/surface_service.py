"""Recording the browser's tool surface (FR-166 through FR-168; 014-T1/T3).

The browser is the only place `getTools()` exists, so it is the only place a
capture can come from. Everything after that is the server's, and the split is
the point:

**The page submits definitions; the server computes hashes.** A page running an
agent's tools is exactly the environment this feature exists to watch, so a
client-supplied hash would be the surface vouching for its own integrity — the
same category error as accepting a tool's success report as proof of an outcome.
`ToolDefinition.identity()` runs here, over the submitted definitions.

**The server assigns the namespace, too.** §9.11 applies stability policy to the
target partition by default, so a page that could label its own tools would mark
a poisoned look-alike `harness` and step outside the policy meant to catch it.
A name the harness itself publishes is `harness`; everything else is `target`.
That fails safe — an unknown tool appearing mid-run becomes an `added` delta in
the watched partition rather than an unwatched curiosity.

**Every capture is recorded; only changes produce deltas.** FR-167 says to
re-capture on every `toolchange` firing, and the plan's own risk note says to
debounce the re-hash rather than the recording. A capture identical to the
baseline appends `tool_surface_captured` and no delta, so the timeline shows
that the surface was *checked* at that moment — an absence of deltas then means
"looked and saw nothing", not "never looked".
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from actionwitness_core.evidence.enums import ToolNamespace
from actionwitness_core.evidence.surface import (
    SurfaceDelta,
    ToolDefinition,
    ToolSurface,
    diff_surfaces,
)
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import EventRepository

__all__ = ["HARNESS_TOOL_NAMES", "SurfaceCaptureOutcome", "SurfaceService"]

#: The harness's own registered tools (§9.11's harness partition, §11.5's table).
#:
#: Held here rather than derived from the browser, because the namespace
#: assignment is exactly what a hostile page would want to control. Kept in step
#: with the frontend by `tests/architecture/test_harness_tool_surface.py`: a
#: name missing from this set lands a legitimate harness tool in the *target*
#: partition, where its ordinary lifecycle appearance and disappearance would
#: fail the run.
#:
#: The frontend declares these in three places, because the harness registers by
#: three mechanisms. Most come from `frontend/src/tools/harnessTools.ts`;
#: `create_outcome_contract` is §25.2's *declarative* tool, which exists because
#: a visible form carries `toolname` rather than because anything called
#: `registerTool`; and `get_workspace_status` registers natively in
#: `frontend/src/tools/workspaceStatus.ts` (ADR-0002 rule 3). The browser
#: reports all of them in `getTools()` identically, so the partition has to know
#: every one — an always-on tool missing from this set sits in the *target*
#: baseline under a name the harness owns, which is a misfiling even while it
#: happens to be stable.
HARNESS_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "create_outcome_contract",
        "get_workspace_status",
        "list_contract_templates",
        "get_outcome_contract",
        "arm_outcome_contract",
        "verify_outcome",
        "get_run_findings",
        "reset_workspace",
        "create_regression_eval",
        "run_regression_eval",
        # Beyond §11.1's table, and here for the same reason as everything above
        # it: the browser reports every registered tool through `getTools()`
        # identically, so a harness tool the server does not recognise lands in
        # the *target* partition and its ordinary lifecycle appearance becomes an
        # `added` delta that fails the run. A name belongs in this set the moment
        # the frontend can register it — including the two audit tools, which the
        # judging deployment never registers because the module is off. Listing
        # them costs nothing there and is required the moment an operator
        # switches the module on.
        "get_run_timeline",
        "get_run_comparison",
        "list_regression_evals",
        "list_audit_packs",
        "get_audit_report",
        "list_benchmarks",
        "get_benchmark_summary",
    }
)


class SurfaceCaptureOutcome:
    """What one capture produced, for the route to report back."""

    __slots__ = ("baseline", "deltas", "sequence", "surface_hash")

    def __init__(
        self,
        *,
        surface_hash: str,
        baseline: bool,
        deltas: Sequence[SurfaceDelta],
        sequence: int,
    ) -> None:
        self.surface_hash = surface_hash
        self.baseline = baseline
        self.deltas = tuple(deltas)
        self.sequence = sequence

    def as_document(self) -> dict[str, Any]:
        return {
            "surface_hash": self.surface_hash,
            "baseline": self.baseline,
            "sequence_number": self.sequence,
            "deltas": [delta.canonical_document() for delta in self.deltas],
        }


class SurfaceService:
    """Records a submitted surface and derives its deltas."""

    def __init__(
        self,
        work: UnitOfWork,
        workspace_id: str,
        *,
        harness_tools: frozenset[str] = HARNESS_TOOL_NAMES,
    ) -> None:
        self._work = work
        self._workspace_id = workspace_id
        self._harness_tools = harness_tools

    async def capture(
        self, run_id: str, definitions: Sequence[Mapping[str, Any]]
    ) -> SurfaceCaptureOutcome:
        """Record one capture, appending a delta event per §9.5 difference."""
        surface = self._surface_of(definitions)
        previous = await self._latest_surface(run_id)
        is_baseline = previous is None

        events = EventRepository(self._work)
        sequence = await events.append(
            run_id,
            {
                "event_type": str(OutcomeEventType.TOOL_SURFACE_CAPTURED.value),
                # The harness recorded it; the browser reported it. `agent` would
                # claim an actor took an action, and a capture is an observation.
                "actor": str(EventActor.HARNESS.value),
                "redacted_payload": {
                    "surface_hash": surface.content_hash(),
                    "baseline": is_baseline,
                    "tool_count": len(surface.tools),
                    "surface": surface.canonical_document(),
                },
            },
        )

        deltas: tuple[SurfaceDelta, ...] = ()
        if previous is not None:
            # §9.11: the target partition is what stability policy watches. The
            # harness partition is diffed by 014-T4 against §11.5's lifecycle,
            # which is a different question with a different answer.
            deltas = diff_surfaces(previous, surface, namespace=ToolNamespace.TARGET)
            for delta in deltas:
                await events.append(
                    run_id,
                    {
                        "event_type": str(OutcomeEventType.TOOL_SURFACE_CHANGED.value),
                        "actor": str(EventActor.HARNESS.value),
                        "tool_name": delta.tool_name,
                        "redacted_payload": {
                            # `kind` is the key `surface_evidence` reads to feed
                            # the policy; the definitions beside it are FR-169's
                            # side-by-side diff.
                            "kind": delta.kind.value,
                            **delta.canonical_document(),
                        },
                    },
                )

        return SurfaceCaptureOutcome(
            surface_hash=surface.content_hash(),
            baseline=is_baseline,
            deltas=deltas,
            sequence=sequence,
        )

    async def baseline(self, run_id: str) -> ToolSurface | None:
        """The armed baseline, for FR-169's pre-invocation identity check."""
        row = await self._work.fetch_one(
            """
            SELECT redacted_payload_json
              FROM events
             WHERE run_id = ? AND event_type = ?
             ORDER BY sequence_number ASC
             LIMIT 1
            """,
            (run_id, str(OutcomeEventType.TOOL_SURFACE_CAPTURED.value)),
        )
        return None if row is None else _surface_from_payload(row["redacted_payload_json"])

    def _surface_of(self, definitions: Sequence[Mapping[str, Any]]) -> ToolSurface:
        """Build a surface from submitted definitions, assigning each namespace."""
        tools: list[ToolDefinition] = []
        for entry in definitions:
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise ApiError(
                    ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                    "Every captured tool must carry a name.",
                )
            tools.append(
                ToolDefinition(
                    name=name,
                    namespace=(
                        ToolNamespace.HARNESS
                        if name in self._harness_tools
                        else ToolNamespace.TARGET
                    ),
                    description=_text(entry.get("description")),
                    read_only_hint=_flag(entry.get("read_only_hint")),
                    untrusted_content_hint=_flag(entry.get("untrusted_content_hint")),
                    input_schema=_schema(entry.get("input_schema")),
                )
            )
        return ToolSurface(tools=tuple(tools))

    async def _latest_surface(self, run_id: str) -> ToolSurface | None:
        row = await self._work.fetch_one(
            """
            SELECT redacted_payload_json
              FROM events
             WHERE run_id = ? AND event_type = ?
             ORDER BY sequence_number DESC
             LIMIT 1
            """,
            (run_id, str(OutcomeEventType.TOOL_SURFACE_CAPTURED.value)),
        )
        return None if row is None else _surface_from_payload(row["redacted_payload_json"])


def _surface_from_payload(stored: Any) -> ToolSurface | None:
    """Rebuild a recorded surface, or `None` if the row cannot be read.

    A stored payload is trusted less than it looks: it was written by this
    service, but a malformed row is still a row, and returning `None` sends the
    caller down the no-baseline path — which §16.1 requires to fail closed
    rather than pass.
    """
    if not stored:
        return None
    try:
        payload = json.loads(stored)
        return ToolSurface.model_validate(payload["surface"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _flag(value: Any) -> bool | None:
    """A hint, or `None` when the descriptor carried none.

    Absent stays absent: a tool that stopped *declaring* itself read-only
    changed its hints, and coercing the absence to `False` would hide that.
    """
    return value if isinstance(value, bool) else None


def _schema(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
