"""Observer isolation for a self-witnessing run (§12.20, FR-172).

FR-172 is three sentences and this module is all three:

    A self-witnessing run shall observe a workspace other than the one recording
    it. The server shall reject an attempt to arm a `self` contract whose
    observed workspace is its own recording workspace, with
    `SELF_OBSERVATION_LOOP`. Recursion depth shall be capped at one: a
    self-witnessing run may not itself be the target of another self-witnessing
    run.

It lives in one file rather than spread across the services that arm, invoke,
and verify, because a rail enforced in three places is a rail somebody adds a
fourth call site to and forgets.

**Which targets this applies to is a protocol question, not a name check.** The
recognition test below is `isinstance(provider, ScopedObservationProvider)` —
does this target need to be told *which* workspace to observe? — rather than
`target_id == "self"`. A string comparison would silently exempt the next
adapter with the same shape, and the property that matters is the shape: a
provider that takes two workspace identifiers is one that could be handed the
same identifier twice.

**Why the observed workspace is minted, not named.** An operator-supplied
identifier would make FR-172's first sentence a validation rule over an input,
and validation rules are exactly what an attacker supplies inputs to defeat.
Minting it server-side makes "other than the one recording it" true by
construction, and — because the mint is an owned child (constitution §2's
isolation boundary) — a self run can never reach a workspace belonging to
somebody else, which is a stronger property than FR-172 asks for and the one the
rest of the product already depends on.

The explicit refusals stay anyway. A guard that holds only because of how the
value is currently produced stops holding the day somebody produces it
differently, and by then nothing is checking.
"""

from __future__ import annotations

from typing import Any, Final

from actionwitness_core.journeys.enums import WorkspaceKind
from actionwitness_core.ports import ScopedObservationProvider, ScopedTargetAdapter
from actionwitness_core.ports.models import Observation

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.workspaces import WorkspaceStore
from actionwitness_service.persistence.database import Database, UnitOfWork

__all__ = [
    "SELF_TARGET_ID",
    "bound_adapter",
    "capture_scoped",
    "capture_target_state",
    "ensure_observed_workspace",
    "observes_a_separate_workspace",
]

#: The descriptor id `integrations.self_target` advertises. Used for messages a
#: human reads, never as the recognition test — see the module docstring.
SELF_TARGET_ID: Final = "self"


def observes_a_separate_workspace(adapter: Any) -> bool:
    """Whether this target must be told which workspace to observe."""
    return isinstance(adapter.observation_provider(), ScopedObservationProvider)


async def ensure_observed_workspace(
    work: UnitOfWork, workspaces: WorkspaceStore, recording_workspace_id: str
) -> str:
    """The workspace a self-witnessing run observes, minted once per recorder.

    Returns the existing one when this workspace has already been given one, so
    arming a second self run observes the same workspace the first one did —
    which is what makes FR-173's "arming twice does not create two runs" an
    assertion about the harness rather than an artefact of a fresh observed
    workspace being handed out each time.

    Raises `SELF_OBSERVATION_LOOP` for FR-172's two forbidden shapes: a workspace
    that is itself somebody's observation target trying to arm a self run, and a
    stored observed identifier that has become the recording one.
    """
    row = await work.fetch_one(
        "SELECT kind, observed_workspace_id FROM workspaces WHERE id = ?",
        (recording_workspace_id,),
    )
    if row is None:  # pragma: no cover - the middleware creates it first
        raise ApiError(ApiErrorCode.HARNESS_ERROR, "The workspace disappeared mid-request.")

    # FR-172's third sentence. The depth cap is a kind check rather than a
    # counter: a workspace of this kind exists only because some other run is
    # observing it, so arming a self run here would make it both observer and
    # observed — the second link the requirement forbids.
    if row["kind"] == str(WorkspaceKind.OBSERVED.value):
        raise ApiError(
            ApiErrorCode.SELF_OBSERVATION_LOOP,
            "This workspace is already being observed by a self-witnessing run, so it "
            "cannot record one of its own. Self-witnessing is capped at one level: the "
            "harness may witness itself, but not witness itself witnessing itself.",
            details=[{"path": "workspace", "message": "already an observed workspace"}],
        )

    observed = row["observed_workspace_id"]
    if isinstance(observed, str) and observed != "":
        # FR-172's second sentence, against the stored value. Unreachable while
        # `create_observed_workspace` is the only writer, and checked because
        # "unreachable" is a claim about today's callers.
        if observed == recording_workspace_id:
            raise ApiError(
                ApiErrorCode.SELF_OBSERVATION_LOOP,
                "This workspace is recorded as observing itself. A self-witnessing run "
                "must observe a workspace other than the one recording it, so nothing "
                "was armed.",
                details=[{"path": "observed_workspace_id", "message": "names its own workspace"}],
            )
        return observed

    return await workspaces.create_observed_workspace(work, recording_workspace_id)


async def capture_target_state(
    adapter: Any, recording_workspace_id: str, observed_workspace_id: str | None
) -> Observation:
    """Capture authoritative state from the right workspace, for any target.

    The single seam every service reads canonical state through. An ordinary
    target has one workspace and its provider takes one; a self-witnessing target
    has two and its provider takes both. Callers pass what they have and this
    decides, so a service that forgot the distinction gets a refusal rather than
    a silently self-observing run.
    """
    provider = adapter.observation_provider()
    if not isinstance(provider, ScopedObservationProvider):
        return await provider.capture(recording_workspace_id)

    if not observed_workspace_id:
        raise ApiError(
            ApiErrorCode.SELF_OBSERVATION_LOOP,
            "The selected target observes a workspace separate from the one recording "
            "the run, and this workspace has none. Arm the run through the workspace "
            "that owns it rather than observing the recording workspace.",
            details=[{"path": "observed_workspace_id", "message": "no observed workspace"}],
        )
    if observed_workspace_id == recording_workspace_id:
        raise ApiError(
            ApiErrorCode.SELF_OBSERVATION_LOOP,
            "A self-witnessing run may not observe its own recording workspace; the "
            "observation would be of the very state the run is producing.",
            details=[{"path": "observed_workspace_id", "message": "names its own workspace"}],
        )
    return await provider.capture_observed(recording_workspace_id, observed_workspace_id)


async def bound_adapter(database: Database, adapter: Any, workspace_id: str) -> Any:
    """The adapter this workspace's run should act through.

    Every ordinary adapter is returned untouched. One that declares
    `ScopedTargetAdapter` is bound to the workspace this run was provisioned to
    observe, *by the server*, before the agent's arguments are anywhere near the
    dispatch — which is what stops an agent from naming the workspace recording
    its own run and driving it.

    The refusals are the same two `capture_target_state` gives, because the two
    channels have to agree about which workspace this run is entitled to touch.
    A run permitted to act on a workspace it may not observe would be able to
    change state that no verdict could ever see.
    """
    if not isinstance(adapter, ScopedTargetAdapter):
        return adapter

    observed = await _observed_workspace_of(database, workspace_id)
    if not observed:
        raise ApiError(
            ApiErrorCode.SELF_OBSERVATION_LOOP,
            "The selected target acts on a workspace separate from the one recording "
            "the run, and this workspace has none. Nothing was dispatched.",
            details=[{"path": "observed_workspace_id", "message": "no observed workspace"}],
        )
    if observed == workspace_id:
        raise ApiError(
            ApiErrorCode.SELF_OBSERVATION_LOOP,
            "A self-witnessing run may not act on its own recording workspace; the "
            "call would change the very state the run is recording.",
            details=[{"path": "observed_workspace_id", "message": "names its own workspace"}],
        )
    return adapter.observing(observed)


async def _observed_workspace_of(database: Database, workspace_id: str) -> str | None:
    """Read in its own short transaction, released before any I/O (ADR-0003)."""
    async with database.reading() as work:
        row = await work.fetch_one(
            "SELECT observed_workspace_id FROM workspaces WHERE id = ?", (workspace_id,)
        )
    return row["observed_workspace_id"] if row is not None else None


async def capture_scoped(database: Database, adapter: Any, workspace_id: str) -> Observation:
    """`capture_target_state`, looking the observed workspace up from storage.

    For the services that observe *after* arming — invocation and verification —
    which hold a workspace identifier and no configuration record. The lookup is
    skipped entirely for an ordinary target, so the common path costs no extra
    read; only a target that needs the second workspace pays for finding it.

    Read in its own short transaction and released before the capture, because
    the capture is I/O and ADR-0003 forbids a transaction spanning one.
    """
    observed: str | None = None
    if observes_a_separate_workspace(adapter):
        observed = await _observed_workspace_of(database, workspace_id)
    return await capture_target_state(adapter, workspace_id, observed)
