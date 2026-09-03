"""Observing ActionWitness's own canonical workspace state (§12.20, FR-171/172).

This is the independent channel for a self-witnessing run. It reads the
*observed* workspace through `/api/v1` — the same public surface any client
uses — and never through an in-process handle, a repository, or the database.
FR-171: a built-in target "shall not receive privileged access unavailable to a
third-party adapter".

**Why it implements `ScopedObservationProvider`.** Every other target's
recording workspace and observed workspace are the same, so `capture(one_id)`
means both. FR-172 pulls them apart: a self-witnessing run must observe a
workspace *other than* the one recording it. The scoped protocol is how the
harness says which is which; the plain `capture` remains for the case where a
caller has only one, and here it refuses rather than guessing, because guessing
would mean observing the run's own workspace — the loop the whole requirement
exists to prevent.

**The state is a projection, not a dump.** What a self contract asserts on is
the small set of facts §12.20's pack needs: which run exists, its status, which
contract it was armed against, whether its timeline is sealed, what the
workspace has selected, and whether a confirmation is pending. Each is read from
§15.1's published workspace response and nothing else, because FR-171 gives this
target no read a third-party adapter could not make.

A verbatim copy of the workspace response would make every unrelated field part
of the contract surface, and the first cosmetic addition to that endpoint would
start failing self runs that had nothing to do with it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from actionwitness_core.journeys.enums import RunState, WorkspacePhase
from actionwitness_core.kernel import JsonValue
from actionwitness_core.ports.models import Observation

__all__ = [
    "PROVENANCE",
    "PROVIDER_ID",
    "SCHEMA_VERSION",
    "WORKSPACE_COOKIE",
    "SelfObservationLoop",
    "SelfObservationProvider",
    "workspace_header",
]

#: §9.3's namespace for this target's state. A contract addresses
#: `target.workspace.run.status` exactly as it addresses `target.cart.total`
#: against the demo store.
NAMESPACE: Final = "target"
#: An identifier, not a dotted module path: `Observation.provider_id` is a
#: `^[a-z][a-z0-9_]*$` token, because it is compared and stored rather than
#: imported. Named for what produced the reading, which is what an evidence
#: reader is trying to learn from it.
PROVIDER_ID: Final = "self_target_observation"

#: Read from the harness's own public API. Named so an evidence reader can see
#: which channel settled the assertion, and distinct from anything a tool could
#: label itself.
PROVENANCE: Final = "harness_public_api"
SCHEMA_VERSION: Final = "1.0"

#: The harness's workspace cookie (§20.1).
#:
#: The observed workspace is addressed the way a browser addresses one: by
#: presenting its identifier as this cookie. That identifier *is* the bearer
#: credential — `WorkspaceStore.resolve` adopts a presented id only when it
#: already exists — so this is not a privileged back door but the same door
#: every client uses. `X-Workspace-Id` is deliberately not used: that header
#: belongs to the demo store's own API (§15.5), and the harness never adopts a
#: workspace a caller merely names.
WORKSPACE_COOKIE: Final = "actionwitness_workspace"


#: A workspace identifier is `ws_` plus URL-safe base64, and nothing else may be
#: put into a header this module composes by hand.
_SAFE_WORKSPACE_ID: Final = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


class SelfObservationLoop(ValueError):
    """Raised when the only workspace on offer is the one being recorded."""


class UnsafeWorkspaceIdentifier(ValueError):
    """A workspace identifier that cannot be put into a header verbatim."""


def workspace_header(workspace_id: str) -> dict[str, str]:
    """Address one workspace by presenting its cookie, as a header.

    **Why a header and not httpx's `cookies=`.** A cookie jar is per *client*,
    and this client is shared: setting the workspace on it would leave the
    previous workspace's cookie attached to the next call, so two concurrent
    self runs would read each other's target. httpx deprecated per-request
    `cookies=` for the same ambiguity. Composing the header explicitly keeps
    every request self-contained, which is the property that matters when the
    value *is* the credential.

    **The identifier is validated, not escaped.** It reaches here from a
    database column, and constitution §5 makes a persisted record untrusted
    input like any other. A value carrying a newline or a `;` would inject a
    second header or a second cookie, so one that is not a plain identifier is
    refused rather than quoted — there is no legitimate workspace identifier
    that needs quoting, so refusing loses nothing and guessing could.
    """
    if not _SAFE_WORKSPACE_ID.fullmatch(workspace_id):
        raise UnsafeWorkspaceIdentifier(
            "a workspace identifier must be an opaque URL-safe token; refusing to "
            "build a request header from this one"
        )
    return {"Cookie": f"{WORKSPACE_COOKIE}={workspace_id}"}


class SelfObservationProvider:
    """Reads one workspace's canonical state over `/api/v1`."""

    provider_id = PROVIDER_ID
    provenance = PROVENANCE

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        #: ADR-0001: injected, never constructed here — the composition root owns
        #: the lifetime, and the same provider reaches a real port in production
        #: and an ASGI app in tests with no branch of its own.
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    async def capture(self, workspace_id: str) -> Observation:
        """Refuse: one workspace is not enough to observe safely (FR-172).

        A self-witnessing run has two workspaces and this signature carries one.
        Treating it as the observed workspace would mean reading the workspace
        that is recording the run — precisely `SELF_OBSERVATION_LOOP`. So the
        provider says it cannot rather than quietly doing the wrong one; callers
        that mean the scoped question ask the scoped method.
        """
        raise SelfObservationLoop(
            "the self target observes a workspace other than the one recording it; "
            "use capture_observed"
        )

    async def capture_observed(
        self, recording_workspace_id: str, observed_workspace_id: str
    ) -> Observation:
        """Capture the observed workspace's state for the recording run."""
        if observed_workspace_id == recording_workspace_id:
            raise SelfObservationLoop(
                "a self-witnessing run may not observe its own recording workspace"
            )

        workspace = await self._get("/api/v1/workspace", workspace_header(observed_workspace_id))
        run = _run_of(workspace)

        return Observation(
            namespace=NAMESPACE,
            provider_id=PROVIDER_ID,
            provenance=PROVENANCE,
            schema_version=SCHEMA_VERSION,
            payload=_projection(workspace, run),
            # The harness publishes no monotonic version for a workspace, and
            # inventing one would let a comparison claim state moved when
            # nothing said so. FR-032's change detection falls back to the
            # content hash, which is the honest answer.
            state_version=None,
            captured_at=self._clock(),
        )

    async def _get(self, path: str, headers: dict[str, str]) -> dict[str, Any]:
        response = await self._client.get(path, headers=headers)
        response.raise_for_status()
        body = response.json()
        return body if isinstance(body, dict) else {}


def _run_of(workspace: dict[str, Any]) -> dict[str, Any]:
    active = workspace.get("active_run")
    return active if isinstance(active, dict) else {}


def _projection(workspace: dict[str, Any], run: dict[str, Any]) -> dict[str, JsonValue]:
    """The small, stable set of facts a self contract may assert on."""
    guidance = workspace.get("guidance")
    phase = guidance.get("phase") if isinstance(guidance, dict) else None
    status = _text(run.get("status"))

    return {
        "workspace": {
            "phase": phase if isinstance(phase, str) else None,
            "selected_contract_id": _text(workspace.get("selected_contract_id")),
            "selected_target_id": _text(workspace.get("selected_target_id")),
            "confirmation_pending": _awaits_a_human(phase, status),
            # The run's identity is what "arming twice does not create two runs"
            # (FR-173) rests on: arm, arm again, and compare this id across the
            # before/after snapshots. A second run would change it. Counting
            # runs would have been the obvious alternative and needs a listing
            # endpoint the harness does not publish — so the check is built from
            # what the public API actually offers rather than from a route
            # invented to make the assertion convenient.
            "run": {
                "id": _text(run.get("id")),
                "status": status,
                # Which contract the run was armed against (FR-173's "a rejected
                # contract candidate does not enter an armed contract"). §15.1's
                # active-run object publishes it, so a self contract can say the
                # armed run is bound to a contract at all — the sharper claim,
                # that it is bound to the *selected* one, is not expressible:
                # §9.4's operators compare a path to a literal JSON value, and
                # comparing two observed paths would be the expression language
                # §10.2 forbids.
                "contract_id": _text(run.get("contract_id")),
                # A run that has completed can never gain another event (§17.1),
                # which is what "a completed run's timeline is immutable" rests
                # on. Derived from the run's own terminal state rather than
                # asked of the tool that finished it.
                "sealed": _text(run.get("completed_at")) is not None,
            },
        }
    }


def _awaits_a_human(phase: object, run_status: str | None) -> bool:
    """Whether a confirmation is outstanding, read from what §15.1 publishes.

    The workspace response carries no confirmation object — `pending_confirmation`
    belongs to `GET /runs/{run_id}` — so this is derived from the state the
    endpoint does publish: §11.5's `awaiting_confirmation` phase, which the
    harness reaches exactly when a protected mutation is paused on a human
    decision (§14.1). Both the guidance phase and the run status are read
    because they are two publications of one fact, and a projection that
    depended on the presence of only one of them would go quiet if §15.1 ever
    stopped carrying it.

    This used to read a `pending_confirmation` key the workspace response never
    carries, which made the fact `false` in every observation ever taken — so
    FR-173's "verification cannot complete while a confirmation is pending"
    could not be stated as a contract term at all. Deriving it from the phase is
    a correction to that derivation, not a widening of the projection: it adds
    no new read and no new route.
    """
    awaiting = WorkspacePhase.AWAITING_CONFIRMATION.value
    return phase == awaiting or run_status == RunState.AWAITING_CONFIRMATION.value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value != "" else None
