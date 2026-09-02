"""Outcome-run routes (spec v1.9 §15.3).

| Method | Endpoint                                          | Status |
|--------|---------------------------------------------------|--------|
| `POST` | `/runs`                                           | 005-T1 |
| `POST` | `/runs/{run_id}/target-tools/{tool_name}:invoke`   | 005-T3 |
| `POST` | `/runs/{run_id}/verify`                           | 005-T6 |
| `GET`  | `/runs/{run_id}/comparison`                       | 005-T11 |
| `GET`  | `/runs/{run_id}/events`                           | 005-T12 |
| `GET`  | `/runs/{run_id}/events` (SSE)                     | 012-T7 |
| `GET`  | `/runs/{run_id}`                                  | 006-T4 |
| `GET`  | `/runs/{run_id}/findings`                         | 006-T4 |
| `GET`  | `/runs/{run_id}/report`                           | 005-T12 |
| `POST` | `/runs/{run_id}/confirmations/{id}/decision`       | 006-T2 |
| `DELETE` | `/runs/{run_id}/confirmations/{id}`             | 006-T2 |

`POST /runs/{run_id}/verify` wins FR-038's race, captures the final
observation, evaluates the contract through the core, and seals the run. The
race is settled before anything is observed, so a losing request cannot capture
a partial final snapshot.

`POST /runs` takes no contract identifier. §15.3 describes arming "a contract",
and FR-024 already made exactly one contract active in the workspace with its
target selected atomically; accepting a second identifier here would reintroduce
the combination FR-024 forbids. The run is armed against what the workspace has
selected, which is also what `GET /workspace` reports.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from actionwitness_core.evidence.surface import MAX_SURFACE_TOOLS
from actionwitness_core.reports.enums import RunMode
from actionwitness_core.security.limits import (
    MAX_TOOL_DESCRIPTION_CHARS,
    MAX_TOOL_NAME_CHARS,
)
from fastapi import APIRouter, Body, Depends, Header, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from actionwitness_service.api.dependencies import (
    ArtifactsDependency,
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.event_stream import (
    EVENT_STREAM_MEDIA_TYPE,
    open_event_stream,
    resume_cursor,
    stream_events,
    wants_event_stream,
)
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.application.comparison_service import ComparisonService
from actionwitness_service.application.confirmation_service import ConfirmationService
from actionwitness_service.application.decision_service import Decision, DecisionService
from actionwitness_service.application.findings_service import (
    DEFAULT_FINDING_LIMIT,
    MAX_FINDING_LIMIT,
    FindingsProjection,
)
from actionwitness_service.application.guidance_service import current_guidance
from actionwitness_service.application.invocation_service import InvocationService
from actionwitness_service.application.limits import EventStreamSlots
from actionwitness_service.application.report_service import ReportService
from actionwitness_service.application.run_service import RunService
from actionwitness_service.application.surface_service import SurfaceService
from actionwitness_service.application.timeline_service import (
    EVENT_PAGE_DEFAULT,
    EVENT_PAGE_MAX,
    TimelineService,
)
from actionwitness_service.application.verification_service import VerificationService

__all__ = ["router"]

router = APIRouter(prefix="/runs", tags=["runs"])


class ArmRequest(BaseModel):
    """§15.3's arming body. Unknown fields are refused, not ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Annotated[str, Field(min_length=1, max_length=32)] = RunMode.VERIFICATION.value
    #: §15.3: "optionally bind an eligible immutable `comparison_source_run_id`".
    #: Eligible means this workspace's own terminal run — checked at arming,
    #: inside the transaction, so a source that moves cannot slip through.
    comparison_source_run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


#: A frozen module-level default, so a request with no body shares one immutable
#: instance rather than constructing one per call.
_DEFAULT_ARM = ArmRequest()


@router.post("", status_code=201)
async def arm_run(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    request: Annotated[ArmRequest, Body()] = _DEFAULT_ARM,
) -> dict[str, Any]:
    """FR-030. Returns 201 with the `run_id`, or refuses and writes nothing."""
    armed = await RunService(database, registry, locks).arm(
        workspace_id,
        mode=request.mode,
        comparison_source_run_id=request.comparison_source_run_id,
    )
    return {
        "run_id": armed.run_id,
        "status": armed.status,
        "contract_id": armed.contract_id,
        "target_id": armed.target_id,
        "scenario_mode": armed.scenario_mode,
        "failure_profile": armed.failure_profile,
        "initial_snapshot": {
            "state_version": armed.state_version,
            "content_hash": armed.snapshot_content_hash,
        },
    }


class InvokeRequest(BaseModel):
    """§15.3's invocation body.

    `arguments` is deliberately an open mapping *here* and closed one layer
    down: the tool's own published schema is the authority on what it accepts,
    and duplicating that as a Pydantic model would give the harness a second
    opinion about a target's surface (§9.1).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    arguments: dict[str, Any] = Field(default_factory=dict)
    #: §15.3: "the identity of the tool definition as observed immediately
    #: before dispatch, which FR-169 compares against the armed baseline." It is
    #: recorded on the start event here; the comparison is FR-169's own task.
    tool_identity_hash: Annotated[str, Field(min_length=1, max_length=128)] | None = None


_DEFAULT_INVOKE = InvokeRequest()

RunId = Annotated[str, Path(min_length=1, max_length=128)]
ToolName = Annotated[str, Path(min_length=1, max_length=128)]


@router.post("/{run_id}/target-tools/{tool_name}:invoke")
async def invoke_target_tool(
    run_id: RunId,
    tool_name: ToolName,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    request: Annotated[InvokeRequest, Body()] = _DEFAULT_INVOKE,
) -> dict[str, Any]:
    """One allowlisted target action, recorded either side of its dispatch.

    The response separates what the tool *reported* from what was independently
    *observed*, because a client that saw only the first would have no way to
    know the two disagreed — which is the disagreement this product exists to
    surface.
    """
    outcome = await InvocationService(database, registry, locks).invoke(
        workspace_id,
        run_id,
        tool_name,
        request.arguments,
        tool_identity_hash=request.tool_identity_hash,
    )
    if outcome.awaiting_confirmation:
        # 202: accepted, not completed. A 200 would say the action finished,
        # and this one has deliberately not started — §14.3 keeps the caller's
        # tool promise pending until a human decides.
        return {
            "status": "awaiting_confirmation",
            "invocation_id": outcome.invocation_id,
            "sequence_number": outcome.sequence_number,
            "confirmation": outcome.confirmation,
            "reported": {"status": None, "summary": outcome.reported_summary},
            "observed": {
                "state_version": outcome.observed_state_version,
                "state_changed": False,
            },
            "next_action": outcome.next_action,
        }
    return {
        "status": "completed",
        "invocation_id": outcome.invocation_id,
        "sequence_number": outcome.sequence_number,
        "terminal_event": outcome.terminal_event,
        "reported": {
            "status": outcome.reported_status,
            "summary": outcome.reported_summary,
            "error_code": outcome.error_code,
        },
        "observed": {
            "state_version": outcome.observed_state_version,
            "state_changed": outcome.observed_state_changed,
        },
        "duration_ms": outcome.duration_ms,
        "next_action": outcome.next_action,
    }


class CapturedToolRequest(BaseModel):
    """One tool as `getTools()` reported it (FR-166).

    No hash and no namespace: both are the server's to compute. A page that
    could supply its own identity hash would be the tool surface vouching for
    itself, and a page that could label its own namespace would mark a poisoned
    look-alike `harness` and step outside the policy that watches the target
    partition (§9.11).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=MAX_TOOL_NAME_CHARS)]
    description: Annotated[str, Field(max_length=MAX_TOOL_DESCRIPTION_CHARS)] = ""
    read_only_hint: bool | None = None
    untrusted_content_hint: bool | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)


class CaptureSurfaceRequest(BaseModel):
    """§20.2 bounds a frontend-submitted surface at 100 tools."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: Annotated[list[CapturedToolRequest], Field(max_length=MAX_SURFACE_TOOLS)]


@router.post("/{run_id}/tool-surface", status_code=201)
async def capture_tool_surface(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    request: Annotated[CaptureSurfaceRequest, Body()],
) -> dict[str, Any]:
    """Record one observation of the browser's tool surface (FR-166, FR-167).

    Every capture is recorded, including one identical to the last. FR-167 asks
    the frontend to re-capture on every `toolchange` firing, and a capture that
    produced no event would leave the timeline unable to distinguish "looked and
    saw nothing" from "never looked" — which is the distinction §16.1 turns into
    a failing policy.

    Appended under the workspace lock and in one transaction: the capture and
    the deltas it implies describe one instant, and a reader who saw the capture
    without its deltas would believe the surface was quiet.
    """
    async with locks.hold(workspace_id), database.transaction() as work:
        await WorkspaceScope(work, workspace_id).run(run_id)
        outcome = await SurfaceService(work, workspace_id).capture(
            run_id, [tool.model_dump() for tool in request.tools]
        )
    return outcome.as_document()


@router.post("/{run_id}/verify")
async def verify_run(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
    artifacts: ArtifactsDependency,
) -> dict[str, Any]:
    """Capture final state and evaluate (§15.3).

    FastAPI is the sole transition authority (FR-038), so winning the race,
    observing, judging, and sealing all happen here rather than being split
    across a client's two calls.

    The findings are summarised rather than returned whole: §23 owns the report
    and returning a second, differently-shaped view of the same verdict would
    give a client two places to read it from.
    """
    outcome = await VerificationService(database, registry, locks, artifacts).verify(
        workspace_id, run_id
    )
    return {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "overall_result": outcome.overall_result,
        "primary_failure": outcome.primary_failure_check_id,
        "findings": {
            "total": len(outcome.findings),
            "failed": sum(1 for finding in outcome.findings if finding.failed),
        },
        "final_snapshot": {"state_version": outcome.final_state_version},
        # §23.1's five layers, and the report's own identity. The full document
        # is an immutable artifact; this is the summary a caller needs to decide
        # whether to fetch it.
        "layers": outcome.report.layers.canonical_document(),
        "counts": outcome.report.counts.canonical_document(),
        "report_content_hash": outcome.report.content_hash(),
        "next_action": outcome.next_action,
    }


@router.get("/{run_id}/comparison")
async def read_comparison(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.3: "a validated matched pre/post comparison or a structured
    ineligibility reason".

    A mismatched pair is a **200 with `comparable: false`**, not an error. FR-019
    and §23.7 both say the rerun "remains an ordinary rerun" — it is a perfectly
    good run that simply cannot be read as the other one's counterpart, and
    returning a failure would push somebody to make the pair match by weakening
    what they meant to test.
    """
    async with database.reading() as work:
        result = await ComparisonService(work, workspace_id).compare(run_id)
    return dict(result.as_document())


def _event_streams(request: Request) -> EventStreamSlots:
    """The application's live count of open SSE connections (FR-008).

    Declared here rather than in `dependencies.py` because exactly one route
    needs it and it is not part of that module's subject — the workspace
    resolution rule. Read from app state for the same reason every other
    long-lived object is: one instance per application, so two tests building
    two applications in one process do not share a ceiling.
    """
    return request.app.state.event_streams


#: Module scope, for the reason `dependencies.py` records: under
#: `from __future__ import annotations` a locally-defined alias resolves to
#: nothing and FastAPI silently reinterprets the parameter as a query parameter.
EventStreamsDependency = Annotated[EventStreamSlots, Depends(_event_streams)]


@router.get("/{run_id}/events", response_model=None)
async def read_events(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    streams: EventStreamsDependency,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=EVENT_PAGE_MAX)] = EVENT_PAGE_DEFAULT,
    accept: Annotated[str | None, Header()] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> dict[str, Any] | StreamingResponse:
    """§15.3: "ordered events after a sequence", paged or streamed.

    The bounds are declared rather than clamped. A request for `limit=500` is
    refused with §15.8's envelope instead of being quietly served 100, because a
    client that asked for 500, received 100, and was told nothing would conclude
    the timeline had ended.

    **Paging is the contract; SSE is an enhancement** (§15.3: "retain the paged
    endpoint as fallback"). A client reaches the stream only by naming
    `text/event-stream` in `Accept` — `*/*`, which `fetch` sends by default,
    gets JSON, because a caller that did not ask for a stream and cannot parse
    one would hang waiting for a body that never ends.

    The run is resolved *before* the stream opens. Once a `200` and a
    `text/event-stream` header are on the wire it is too late to send §15.8's
    envelope, so a run this workspace may not see has to fail here, as a plain
    404, rather than as an empty stream a client has to interpret.

    FR-008's two-connection ceiling is taken in the same window and in that
    order: authorization first, so a stranger's refused request never spends a
    slot the workspace could have used. Only this branch is capped — the paged
    fallback below is an ordinary request that ends when it is answered, and
    capping it would take the enhancement's failure mode and give it to the
    contract.
    """
    if wants_event_stream(accept):
        cursor = resume_cursor(last_event_id, after_sequence)
        async with database.reading() as work:
            await TimelineService(work, workspace_id).events(run_id, after_sequence=cursor, limit=1)
        body = await open_event_stream(
            streams,
            workspace_id,
            stream_events(database, workspace_id, run_id, after_sequence=cursor),
        )
        return StreamingResponse(
            body,
            media_type=EVENT_STREAM_MEDIA_TYPE,
            headers={
                # Buffering a stream defeats it: a proxy that waits for the
                # response to finish delivers a run's timeline all at once,
                # after the run is over.
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    async with database.reading() as work:
        page = await TimelineService(work, workspace_id).events(
            run_id, after_sequence=after_sequence, limit=limit
        )
    return page.as_document()


@router.get("/{run_id}/report")
async def read_report(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    artifacts: ArtifactsDependency,
) -> dict[str, Any]:
    """§15.3's JSON report, read back from the artifact it was sealed into.

    The stored bytes are hash-verified before they are served: a report that
    fails verification is refused rather than returned with a caveat, because a
    reader asking what happened would otherwise be shown a corrupted verdict.
    """
    async with database.reading() as work:
        return await ReportService(work, workspace_id, artifacts).report(run_id)


class DecisionRequest(BaseModel):
    """§14.4's choice. Named, not a boolean.

    `approve_once` says what the approval is: consent for one invocation, spent
    by it. A caller sending `true` could reasonably believe it had switched
    something on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["approve_once", "deny"]


ConfirmationId = Annotated[str, Path(min_length=1, max_length=128)]


@router.post("/{run_id}/confirmations/{confirmation_id}/decision")
async def decide_confirmation(
    run_id: RunId,
    confirmation_id: ConfirmationId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    request: Annotated[DecisionRequest, Body()],
) -> dict[str, Any]:
    """§15.3: "Human approver chooses `approve_once` or `deny`".

    The workspace cookie authorizes this, never the identifier in the path
    (§14.5) — so a `confirmation_id` learned elsewhere resolves to nothing.
    There is deliberately no way for an agent to reach this with its own
    credentials: the constitution forbids an agent approving its own consent,
    and the only thing standing between those two statements is that this
    endpoint reads the same cookie as every other.
    """
    outcome = await DecisionService(database, locks).decide(
        workspace_id, run_id, confirmation_id, request.decision
    )
    return {
        "confirmation_id": outcome.confirmation_id,
        "status": outcome.status,
        # §14.6: the response states the decision, whether any mutation
        # occurred, and who acts next.
        "mutated": outcome.mutated,
        "run_status": outcome.run_status,
        "detail": outcome.detail,
        "next_action": outcome.next_action,
    }


@router.delete("/{run_id}/confirmations/{confirmation_id}")
async def cancel_confirmation(
    run_id: RunId,
    confirmation_id: ConfirmationId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
) -> dict[str, Any]:
    """§15.3: cancel because the invocation, tab, or workspace went away.

    A cancellation is not a denial: nobody refused the action, the request
    simply stopped being answerable. Both create no order, and both are
    recorded distinctly so a reader can tell "a person said no" from "the tab
    closed" (§14.9).
    """
    outcome = await DecisionService(database, locks).decide(
        workspace_id, run_id, confirmation_id, Decision.CANCEL
    )
    return {
        "confirmation_id": outcome.confirmation_id,
        "status": outcome.status,
        "mutated": outcome.mutated,
        "run_status": outcome.run_status,
        "detail": outcome.detail,
        "next_action": outcome.next_action,
    }


@router.get("/{run_id}")
async def read_run(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
) -> dict[str, Any]:
    """§15.3: "Get run status and summary".

    The summary a client needs to decide what to do next — not the report, which
    has its own endpoint and its own hash. Two differently shaped views of one
    verdict would give a reader two places to read it from and no way to know
    which was authoritative when they disagreed.
    """
    # §14.14: a request nobody answered is expired by the server. Done on the
    # read a client polls, because otherwise the run waits on a decision that
    # can never arrive and only a reset — which discards the evidence — frees
    # it. A no-op when nothing has lapsed.
    await DecisionService(database, locks).expire_lapsed(workspace_id, run_id)

    async with database.reading() as work:
        run = await WorkspaceScope(work, workspace_id).run(run_id)
        pending = await ConfirmationService(work, workspace_id).pending_for_run(run_id)
        guidance = await current_guidance(work, workspace_id)
    return {
        "run_id": str(run["id"]),
        "status": str(run["status"]),
        "overall_result": run["overall_result"],
        "contract_id": run["contract_id"],
        "target_id": str(run["target_id"]),
        "adapter_id": str(run["target_adapter_id"]),
        "scenario_mode": run["scenario_mode"],
        "failure_profile": run["failure_profile"],
        "comparison_source_run_id": run["comparison_source_run_id"],
        "comparison_key_hash": run["comparison_key_hash"],
        "started_at": str(run["started_at"]),
        "completed_at": run["completed_at"],
        # Present only while a human is being waited on, so a client that
        # reloaded mid-decision can rebuild the dialog rather than losing it.
        "pending_confirmation": (
            None
            if pending is None
            else {
                "confirmation_id": str(pending["id"]),
                "tool_name": str(pending["tool_name"]),
                "expires_at": str(pending["expires_at"]),
                "consequence": json.loads(pending["consequence_summary_json"]),
            }
        ),
        "next_action": guidance.next_action(),
    }


@router.get("/{run_id}/findings")
async def read_findings(
    run_id: RunId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    limit: Annotated[int, Query(ge=1, le=MAX_FINDING_LIMIT)] = DEFAULT_FINDING_LIMIT,
) -> dict[str, Any]:
    """§11.4's bounded findings, for `get_run_findings` and the findings panel.

    Bounded server-side. §23.3's budget is a rule about what the harness owes an
    agent, and a client that applied it itself could simply not.
    """
    async with database.reading() as work:
        return await FindingsProjection(work, workspace_id).read(run_id, limit=limit)
