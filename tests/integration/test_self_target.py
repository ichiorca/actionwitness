"""ActionWitness as its own target, and the isolation that makes it safe.

Spec v1.9 §12.20:

* **FR-171** — a built-in `self` target presented through the same public
  `ManagedTargetAdapter` and `ObservationProvider` protocols an external
  integration implements, with no privileged access a third party could not
  have.
* **FR-172** — a self-witnessing run observes a workspace *other than* the one
  recording it; arming a `self` contract whose observed workspace is its own
  recording workspace is refused with `SELF_OBSERVATION_LOOP`; recursion is
  capped at one.

FR-171's structural half — that the distribution *cannot* reach the harness's
internals, whatever its code happens to call today — is a gate rather than a
behaviour, and lives in `tests/architecture/test_import_boundaries.py` with the
other boundary checks. What is here is the behavioural half: the adapter really
does drive and observe the harness through `/api/v1`.

**The tests that carry FR-172 all try to build the loop.** Each one takes a
different route to a run observing itself: through the recursion cap, through a
stored identifier, through a provider called with one workspace instead of two.
A single test of the happy path would pass against an implementation with no
guard at all, since the ordinary case never asks for the loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from actionwitness_core.evidence.enums import EvidenceSourceClassification, ToolReportedStatus
from actionwitness_core.journeys.enums import (
    EventActor,
    OutcomeEventType,
    RunState,
    WorkspaceKind,
)
from actionwitness_core.kernel import ContractError
from actionwitness_core.ports.models import ExecutionContext
from actionwitness_core.ports.schemas import validate_arguments
from actionwitness_service.api.app import API_PREFIX, create_app
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.contract_service import ContractService
from actionwitness_service.application.invocation_service import InvocationService
from actionwitness_service.application.run_service import RunService
from actionwitness_service.application.self_witness import (
    bound_adapter,
    capture_target_state,
    observes_a_separate_workspace,
)
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.locks import WorkspaceLocks
from fastapi import FastAPI
from integrations.self_target.tools import GET_RUN_FINDINGS, GET_WORKSPACE_STATUS

from integrations.self_target import (
    PROVENANCE,
    TARGET_ID,
    SelfObservationLoop,
    SelfObservationProvider,
    SelfTargetAdapter,
    UnboundSelfTarget,
    UnknownSelfTool,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

WORKSPACE = f"{API_PREFIX}/workspace"
CONTRACTS = f"{API_PREFIX}/contracts"
RUNS = f"{API_PREFIX}/runs"

#: A minimal contract naming the `self` target.
#:
#: Authored here rather than taken from FR-173's built-in pack, which does not
#: exist yet. That is deliberate and not a shortcut: these tests are about
#: whether a run against the self target is *isolated*, and a contract whose
#: assertions were interesting would make a failure ambiguous between the
#: isolation rail and the assertion. Every term below is the least the model
#: will accept.
SELF_CONTRACT: Mapping[str, Any] = {
    "schema_version": "1.0",
    "name": "self-workspace-is-readable",
    "description": "The observed workspace reports a phase.",
    "target_id": TARGET_ID,
    "intent": "Read the observed workspace's status and leave it unchanged.",
    "expected_tools": {"ordered": True, "calls": [GET_WORKSPACE_STATUS]},
    "assertions": [
        {
            "id": "phase-is-reported",
            "path": "target.workspace.phase",
            "operator": "exists",
            "severity": "critical",
        },
    ],
}


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[FastAPI]:
    """A running harness with the demo store switched off.

    Off on purpose: §21.1's credential-free path is somebody else's test, and
    leaving the store enabled here would let a self run resolve the wrong
    adapter without anything saying so.
    """
    app = create_app(
        environ={
            "HARNESS_ENV": "local",
            "BUGGY_STORE_ENABLED": "false",
            "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        },
        database_path=tmp_path / "harness.sqlite3",
    )
    async with app.router.lifespan_context(app):
        yield app


def visitor(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    )


async def _new_workspace(app: FastAPI) -> str:
    """A workspace created the way a browser creates one: by asking for it."""
    async with visitor(app) as client:
        response = await client.get(WORKSPACE)
        assert response.status_code == 200, response.text
        return str(response.json()["workspace_id"])


async def _select_self_contract(app: FastAPI, workspace_id: str) -> str:
    database: Database = app.state.database
    async with database.transaction() as work:
        service = ContractService(work, workspace_id, app.state.adapters)
        created = await service.instantiate(SELF_CONTRACT, source_template_id="test_self")
        await service.select(str(created["contract_id"]))
    return str(created["contract_id"])


def _context() -> ExecutionContext:
    """The binding an invocation carries (§9.1), built by hand.

    These tests call the adapter directly rather than through
    `InvocationService`, because what is under test is the adapter's own
    behaviour at the HTTP boundary. Every identifier here is therefore arbitrary
    and only has to be well-formed.
    """
    return ExecutionContext(
        workspace_id="recording-workspace",
        run_id="run-1",
        invocation_id="inv-1",
        request_id="req-1",
        correlation_id="cor-1",
        idempotency_key="idem-1",
        actor=EventActor.AGENT,
    )


def _run_service(app: FastAPI) -> RunService:
    return RunService(app.state.database, app.state.adapters, WorkspaceLocks())


async def _workspace_row(app: FastAPI, workspace_id: str) -> Mapping[str, Any]:
    database: Database = app.state.database
    async with database.reading() as work:
        row = await work.fetch_one("SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
    assert row is not None
    return dict(row)


# --- FR-171: registered like anything else, privileged like nothing ---------


async def test_the_self_target_is_registered_and_available(harness: FastAPI) -> None:
    # Arrange / Act
    registry = harness.state.adapters

    # Assert
    assert registry.is_available("self")
    adapter = registry.adapter("self")
    assert adapter.descriptor.target_id == TARGET_ID


async def test_the_self_target_advertises_no_fault_it_cannot_inject(harness: FastAPI) -> None:
    """§9.1: the descriptor is a claim, so it has to be the true one.

    The harness injects no defect into itself, and this is the assertion that
    an *empty* fault list would have got wrong. `TargetDescriptor.injects` reads
    silence as "makes no claim" and permits everything, which is right for an
    external target and would let a self run be armed against
    `discount_reported_but_not_applied` — a report naming an active fault
    nothing produced, which is the false claim the product exists to catch.
    """
    # Arrange / Act
    registry = harness.state.adapters

    # Assert
    assert registry.supported_fault_profiles(TARGET_ID) == ("none",)
    assert registry.supported_scenario_modes(TARGET_ID) == ("current",)
    assert not registry.injects_fault_profile(TARGET_ID, "discount_reported_but_not_applied")


async def test_the_adapter_drives_the_harness_through_its_public_api(harness: FastAPI) -> None:
    """FR-171's other half: the tools really are the harness's own routes."""
    # Arrange
    observed = await _new_workspace(harness)
    adapter: SelfTargetAdapter = harness.state.adapters.adapter("self")

    # Act
    result = await adapter.observing(observed).execute(
        "recording-workspace", GET_WORKSPACE_STATUS, {}, _context()
    )

    # Assert — the tool's own report, and nothing more. A verdict needs the
    # separate observation below; this is only what the tool said.
    assert result.tool_name == GET_WORKSPACE_STATUS
    assert result.reported_status is ToolReportedStatus.SUCCESS
    assert observed in result.reported_summary
    # Still labelled as the channel under test, whatever it says about itself.
    assert result.source_classification is EvidenceSourceClassification.TOOL_REPORTED


async def test_an_unpublished_tool_is_refused_rather_than_forwarded(harness: FastAPI) -> None:
    """The allowlist is the surface. `verify_outcome` is deliberately not on it.

    A self-witnessing run that could tell its observed workspace to verify would
    be driving the machinery recording it, which is the recursion FR-172 caps.
    """
    # Arrange
    adapter: SelfTargetAdapter = harness.state.adapters.adapter("self")

    # Act / Assert
    with pytest.raises(UnknownSelfTool):
        await adapter.execute(
            "recording-workspace",
            "verify_outcome",
            {"observed_workspace_id": "somewhere"},
            _context(),
        )


async def test_the_observation_reads_the_observed_workspace(harness: FastAPI) -> None:
    """The independent channel, and it reads the workspace it was told to."""
    # Arrange
    observed = await _new_workspace(harness)
    contract_id = await _select_self_contract(harness, observed)
    provider: SelfObservationProvider = harness.state.adapters.adapter(
        "self"
    ).observation_provider()

    # Act
    observation = await provider.capture_observed("recording-workspace", observed)

    # Assert — the projection describes the *observed* workspace's selection.
    workspace = observation.payload["workspace"]
    assert workspace["selected_contract_id"] == contract_id
    assert workspace["selected_target_id"] == TARGET_ID
    # Named so an evidence reader can see which channel settled the assertion,
    # and distinct from anything a tool could label itself.
    assert observation.provenance == PROVENANCE


async def test_the_workspace_response_names_the_workspace_it_observes(harness: FastAPI) -> None:
    """An operator can see the second workspace, and open it.

    Without this the self-witnessing story is invisible from the outside: the
    run reports a verdict about a workspace nothing in the API ever mentions.
    `None` before a self run is armed is the other half of the statement — it
    says this workspace observed a target rather than itself.
    """
    # Arrange
    recording = await _new_workspace(harness)
    await _select_self_contract(harness, recording)

    async with visitor(harness) as client:
        client.cookies.set("actionwitness_workspace", recording)
        before = (await client.get(WORKSPACE)).json()

        # Act
        await _run_service(harness).arm(recording)
        after = (await client.get(WORKSPACE)).json()

    # Assert
    assert before["observed_workspace_id"] is None
    observed = after["observed_workspace_id"]
    assert observed and observed != recording
    assert observed == (await _workspace_row(harness, recording))["observed_workspace_id"]


# --- FR-172: the workspace acted on is the server's choice, not the agent's --


async def test_the_adapter_refuses_to_act_before_a_workspace_is_bound(harness: FastAPI) -> None:
    """An unbound self adapter cannot act at all.

    There is exactly one workspace it could plausibly fall back to — the one
    recording the run — and that fallback is the loop. So the absence has to be
    an error rather than a default, which is what makes forgetting to bind a
    crash instead of a silent self-observation.
    """
    # Arrange
    adapter: SelfTargetAdapter = harness.state.adapters.adapter("self")

    # Act / Assert
    with pytest.raises(UnboundSelfTarget):
        await adapter.execute("recording-workspace", GET_WORKSPACE_STATUS, {}, _context())


async def test_no_published_tool_lets_an_agent_name_the_workspace(harness: FastAPI) -> None:
    """The subject is not an argument, so the agent cannot supply it.

    This is the hole the binding closes. If `observed_workspace_id` were a tool
    argument, the agent — the thing under test — could name the workspace
    recording its own run and drive it. §11.4's schemas therefore admit no such
    field, and `validate_arguments` rejects it before anything is dispatched.
    """
    # Arrange
    adapter: SelfTargetAdapter = harness.state.adapters.adapter("self")

    # Act / Assert
    for spec in adapter.tool_specs():
        schema = dict(spec.input_schema)
        assert schema.get("additionalProperties") is False, f"{spec.name} accepts unknown arguments"
        assert "observed_workspace_id" not in (schema.get("properties") or {}), (
            f"{spec.name} lets the agent name the workspace it acts on"
        )
        with pytest.raises(ContractError):
            validate_arguments(schema, {"observed_workspace_id": "elsewhere"}, tool_name=spec.name)


async def test_the_binding_seam_uses_storage_not_the_request(harness: FastAPI) -> None:
    """`bound_adapter` binds to what arming provisioned, and to nothing else."""
    # Arrange
    recording = await _new_workspace(harness)
    await _select_self_contract(harness, recording)
    await _run_service(harness).arm(recording)
    observed = str((await _workspace_row(harness, recording))["observed_workspace_id"])

    # Act
    bound = await bound_adapter(
        harness.state.database, harness.state.adapters.adapter("self"), recording
    )
    result = await bound.execute(recording, GET_WORKSPACE_STATUS, {}, _context())

    # Assert — the call landed on the observed workspace, not the recording one.
    assert observed in result.reported_summary
    assert recording not in result.reported_summary


async def test_the_binding_seam_refuses_an_unprovisioned_workspace(harness: FastAPI) -> None:
    """No provisioned workspace means no dispatch, rather than a default."""
    # Arrange
    workspace = await _new_workspace(harness)

    # Act / Assert
    with pytest.raises(ApiError) as refused:
        await bound_adapter(
            harness.state.database, harness.state.adapters.adapter("self"), workspace
        )
    assert refused.value.code is ApiErrorCode.SELF_OBSERVATION_LOOP


async def test_the_binding_seam_leaves_an_ordinary_adapter_alone(harness: FastAPI) -> None:
    """Recognised by protocol, so no other target changes shape."""

    # Arrange
    class _OrdinaryAdapter:
        pass

    adapter = _OrdinaryAdapter()

    # Act
    bound = await bound_adapter(harness.state.database, adapter, "w-1")

    # Assert
    assert bound is adapter


async def test_an_invocation_lands_on_the_observed_workspace(harness: FastAPI) -> None:
    """The whole path, through the real service: arm, then act.

    The end-to-end version of the binding above, and the one that would notice
    if the seam were ever dropped from the invocation pipeline. It asserts on
    *where the call landed* rather than on whether it returned 200, because a
    call that reached the recording workspace would also return 200 — and would
    be the run mutating the state it was recording.
    """
    # Arrange
    recording = await _new_workspace(harness)
    await _select_self_contract(harness, recording)
    armed = await _run_service(harness).arm(recording)
    observed = str((await _workspace_row(harness, recording))["observed_workspace_id"])

    # Act
    outcome = await InvocationService(
        harness.state.database, harness.state.adapters, harness.state.locks
    ).invoke(recording, armed.run_id, GET_WORKSPACE_STATUS, {})

    # Assert
    assert outcome.terminal_event == str(OutcomeEventType.TOOL_INVOCATION_COMPLETED.value)
    assert observed in outcome.reported_summary
    assert recording not in outcome.reported_summary


# --- FR-172: every route to the loop, refused -------------------------------


async def test_the_provider_refuses_when_given_only_one_workspace(harness: FastAPI) -> None:
    """`capture` carries one identifier, and a self run has two.

    Treating the single one as the observed workspace would read the workspace
    recording the run. The provider says it cannot rather than guessing, because
    the guess is exactly the loop.
    """
    # Arrange
    provider: SelfObservationProvider = harness.state.adapters.adapter(
        "self"
    ).observation_provider()

    # Act / Assert
    with pytest.raises(SelfObservationLoop):
        await provider.capture("some-workspace")


async def test_the_provider_refuses_to_observe_its_own_recorder(harness: FastAPI) -> None:
    # Arrange
    provider: SelfObservationProvider = harness.state.adapters.adapter(
        "self"
    ).observation_provider()

    # Act / Assert
    with pytest.raises(SelfObservationLoop):
        await provider.capture_observed("w-1", "w-1")


async def test_the_capture_seam_refuses_a_self_observing_pair(harness: FastAPI) -> None:
    """The service-side guard, which produces the specified error code.

    The provider's refusal above is a `ValueError` inside an integration; this
    one is the `SELF_OBSERVATION_LOOP` FR-172 names, which is what a caller
    actually sees.
    """
    # Arrange
    adapter = harness.state.adapters.adapter("self")

    # Act / Assert
    with pytest.raises(ApiError) as refused:
        await capture_target_state(adapter, "w-1", "w-1")
    assert refused.value.code is ApiErrorCode.SELF_OBSERVATION_LOOP


async def test_the_capture_seam_refuses_when_no_workspace_was_provisioned(
    harness: FastAPI,
) -> None:
    """Absence is refused, not defaulted.

    A `None` that fell back to the recording workspace would make the loop the
    *easy* path — reachable by forgetting an argument rather than by asking for
    it.
    """
    # Arrange
    adapter = harness.state.adapters.adapter("self")

    # Act / Assert
    with pytest.raises(ApiError) as refused:
        await capture_target_state(adapter, "w-1", None)
    assert refused.value.code is ApiErrorCode.SELF_OBSERVATION_LOOP


async def test_an_ordinary_target_is_untouched_by_the_seam(harness: FastAPI) -> None:
    """The seam recognises the protocol, not the name.

    An adapter whose provider takes one workspace goes through `capture` and
    never has an observed workspace looked up, so nothing about FR-172 changes
    how every other target is observed.
    """

    # Arrange
    class _OneWorkspaceProvider:
        def __init__(self) -> None:
            self.asked: list[str] = []

        async def capture(self, workspace_id: str) -> str:  # type: ignore[override]
            self.asked.append(workspace_id)
            return "observed"

    class _OrdinaryAdapter:
        def __init__(self) -> None:
            self.provider = _OneWorkspaceProvider()

        def observation_provider(self) -> _OneWorkspaceProvider:
            return self.provider

    adapter = _OrdinaryAdapter()

    # Act
    assert not observes_a_separate_workspace(adapter)
    captured = await capture_target_state(adapter, "w-1", None)

    # Assert
    assert captured == "observed"
    assert adapter.provider.asked == ["w-1"]


# --- FR-172 through the arming path -----------------------------------------


async def test_arming_a_self_contract_mints_a_separate_observed_workspace(
    harness: FastAPI,
) -> None:
    """FR-172's first sentence, at the moment it becomes true."""
    # Arrange
    recording = await _new_workspace(harness)
    await _select_self_contract(harness, recording)

    # Act
    armed = await _run_service(harness).arm(recording)

    # Assert
    row = await _workspace_row(harness, recording)
    observed_id = row["observed_workspace_id"]
    assert observed_id, "arming a self contract provisioned no workspace to observe"
    assert observed_id != recording

    observed = await _workspace_row(harness, observed_id)
    assert observed["kind"] == str(WorkspaceKind.OBSERVED.value)
    # Owned, so constitution §2's isolation boundary is not crossed: the
    # observed workspace belongs to the run that made it and dies with it.
    assert observed["owner_workspace_id"] == recording
    assert armed.target_id == TARGET_ID


async def test_the_baseline_snapshot_describes_the_observed_workspace(
    harness: FastAPI,
) -> None:
    """The run's evidence is about the other workspace, not about itself.

    The sharper half of the requirement. A run that provisioned a second
    workspace and then snapshotted its own state would satisfy every structural
    check above and still be the loop.
    """
    # Arrange
    recording = await _new_workspace(harness)
    contract_id = await _select_self_contract(harness, recording)

    # Act
    await _run_service(harness).arm(recording)

    # Assert
    database: Database = harness.state.database
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT s.redacted_state_json AS state, s.provenance AS provenance "
            "FROM snapshots s JOIN runs r ON r.id = s.run_id WHERE r.workspace_id = ?",
            (recording,),
        )
    assert row is not None
    assert row["provenance"] == PROVENANCE
    # The recording workspace has this contract selected; the freshly minted
    # observed workspace has none. Seeing `None` here is what proves the
    # snapshot came from the other workspace.
    assert contract_id not in row["state"]


async def test_arming_twice_observes_the_same_workspace(harness: FastAPI) -> None:
    """The observed workspace is minted once per recorder, not once per run.

    A fresh workspace each time would quietly make every self run start from an
    empty target, so FR-173's "arming twice does not create two runs" would pass
    because there was nothing left to see rather than because the harness
    behaved.
    """
    # Arrange
    recording = await _new_workspace(harness)
    await _select_self_contract(harness, recording)
    await _run_service(harness).arm(recording)
    first = (await _workspace_row(harness, recording))["observed_workspace_id"]

    database: Database = harness.state.database
    async with database.transaction() as work:
        # Release the run lease the way completing a run does, so the second
        # arm is refused for the reason under test and not for FR-039's.
        await work.execute("UPDATE workspaces SET active_run_id = NULL WHERE id = ?", (recording,))
        await work.execute(
            "UPDATE runs SET status = 'completed' WHERE workspace_id = ?", (recording,)
        )

    # Act
    await _run_service(harness).arm(recording)

    # Assert
    assert (await _workspace_row(harness, recording))["observed_workspace_id"] == first


async def test_an_observed_workspace_may_not_record_a_self_run(harness: FastAPI) -> None:
    """FR-172's recursion cap: one level, and the second link is refused."""
    # Arrange — a real observed workspace, produced by a real self run.
    recording = await _new_workspace(harness)
    await _select_self_contract(harness, recording)
    await _run_service(harness).arm(recording)
    observed_id = str((await _workspace_row(harness, recording))["observed_workspace_id"])

    await _select_self_contract(harness, observed_id)

    # Act / Assert — the observed workspace now tries to witness something too.
    with pytest.raises(ApiError) as refused:
        await _run_service(harness).arm(observed_id)
    assert refused.value.code is ApiErrorCode.SELF_OBSERVATION_LOOP


async def test_a_workspace_recorded_as_observing_itself_is_refused(harness: FastAPI) -> None:
    """FR-172's second sentence, against a stored value.

    Unreachable through the mint, which is the point: the guard has to hold
    against the column rather than against the one function that writes it, or
    it stops holding the day something else does.
    """
    # Arrange
    recording = await _new_workspace(harness)
    await _select_self_contract(harness, recording)
    database: Database = harness.state.database
    async with database.transaction() as work:
        await work.execute(
            "UPDATE workspaces SET observed_workspace_id = ? WHERE id = ?",
            (recording, recording),
        )

    # Act / Assert
    with pytest.raises(ApiError) as refused:
        await _run_service(harness).arm(recording)
    assert refused.value.code is ApiErrorCode.SELF_OBSERVATION_LOOP


# --- the whole journey, through /api/v1 alone --------------------------------


async def test_actionwitness_verifies_itself_end_to_end(harness: FastAPI) -> None:
    """§12.20's point, exercised the way a visitor would exercise it.

    Every step is an HTTP call: pick a built-in `self` contract, arm it, drive a
    published harness tool, and ask for a verdict. Nothing reaches into a
    service, so what passes here is the composed path — middleware, cookies,
    validation, the adapter, the observation provider, the evaluator — and not
    an approximation of it.

    The assertion that matters is the last one. A run can reach `completed`
    while having quietly observed the workspace recording it, and every response
    above would look identical; the check on the sealed report's own snapshot is
    what distinguishes witnessing from self-reference.
    """
    # Arrange — a self contract from the built-in pack (FR-173), selected the
    # way the workspace UI selects one.
    async with visitor(harness) as client:
        templates = (await client.get(f"{CONTRACTS}/templates")).json()["templates"]
        # Named, not "the first self template". The journey below drives two
        # specific reads, and a contract picked by position would eventually be
        # one describing a different journey — which fails for a true reason
        # that has nothing to do with what this test is about.
        chosen = next(
            t
            for t in templates
            if t["source_template_id"] == "self_completed_run_timeline_is_immutable"
        )
        recording = str((await client.get(WORKSPACE)).json()["workspace_id"])

        selected = await client.post(f"{CONTRACTS}/{chosen['contract_id']}/select")
        assert selected.status_code == 200, selected.text

        # Act — arm, act, verify.
        armed = await client.post(RUNS, json={})
        assert armed.status_code == 201, armed.text
        run_id = str(armed.json()["run_id"])

        observed = str((await client.get(WORKSPACE)).json()["observed_workspace_id"])

        # The journey the contract describes, in full. Both are reads: this
        # contract asserts that a completed run's timeline does not move, so
        # driving a mutation would be testing something else.
        for tool in (GET_RUN_FINDINGS, GET_WORKSPACE_STATUS):
            invoked = await client.post(
                f"{RUNS}/{run_id}/target-tools/{tool}:invoke", json={"arguments": {}}
            )
            assert invoked.status_code == 200, invoked.text

        verdict = await client.post(f"{RUNS}/{run_id}/verify")
        assert verdict.status_code == 200, verdict.text

    # Assert — the harness judged itself and the judgement was a pass. Asserted
    # as a specific state rather than "not an error": §16's vocabulary
    # distinguishes `passed` from `passed_with_warnings`, `failed` and `error`,
    # and a test that accepted any of them would keep passing after the run
    # started degrading.
    body = verdict.json()
    assert body["status"] == str(RunState.PASSED.value), body
    assert body["findings"]["failed"] == 0, body["findings"]

    # and it was a verdict about the *other* workspace. The report is sealed
    # evidence, so this reads what was persisted rather than what was returned.
    assert observed and observed != recording
    database: Database = harness.state.database
    async with database.reading() as work:
        rows = await work.fetch_all(
            "SELECT s.redacted_state_json AS state, s.provenance AS provenance "
            "FROM snapshots s JOIN runs r ON r.id = s.run_id WHERE r.id = ?",
            (run_id,),
        )
    assert len(rows) >= 2, "a verified run records a before and an after snapshot"
    for row in rows:
        assert row["provenance"] == PROVENANCE
        # The recording workspace's own identifier must appear in neither
        # snapshot: if it did, the run observed the state it was producing.
        assert recording not in row["state"]
