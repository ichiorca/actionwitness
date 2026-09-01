"""The authoritative observation provider for the Buggy Store (§9.3, §13.2).

BUILD_ORDER §7/M2: "normalize target state under `target`, with provider
`buggy_store_state` and `state_version` as observation metadata."

This is the independent channel. Everything else in the adapter carries what a
*tool said*; this carries what the store's own canonical state *is*, read back
over the store's versioned API rather than derived from any tool response. That
separation is the product: constitution §4 forbids manufacturing observed state
from a successful tool response, and the shortest route to violating it would be
an observation provider that reused the body of the call it just made.

So `capture` performs its own read. It never takes a `ToolExecutionResult`, and
there is no code path from one to an `Observation` - the core's port models make
the two unrelated types, and this module keeps them that way where the responses
are real.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from actionwitness_core.kernel import JsonValue
from actionwitness_core.ports.models import Observation

__all__ = [
    "OBSERVATION_NAMESPACE",
    "PROVENANCE",
    "PROVIDER_ID",
    "STATE_PATH",
    "BuggyStoreObservationProvider",
]

#: §9.3's conventional namespace: contract authors write `target.cart.total`.
OBSERVATION_NAMESPACE: Final = "target"

#: §9.3 names this provider for the Buggy Store integration.
PROVIDER_ID: Final = "buggy_store_state"

#: How the value was obtained. Project-allocated: §9.3 fixes
#: `platform_session_api` for Shopify's shopper-session read but names nothing
#: for a managed target. This says what it is - canonical server state read back
#: through the target's own versioned API - without implying direct database
#: access, which §9.3 is careful not to claim even for Shopify.
PROVENANCE: Final = "managed_target_api"

#: The store endpoint that returns the whole §13.2 canonical document.
STATE_PATH: Final = "/demo/api/v1/store/state"

#: Project-allocated header; the store's own isolation scope.
WORKSPACE_HEADER: Final = "X-Workspace-Id"

#: The observation payload's schema version, so a stored snapshot records which
#: shape it was captured under (constitution §4).
SCHEMA_VERSION: Final = "1.0"


class BuggyStoreObservationProvider:
    """Reads canonical store state over the target's public HTTP surface.

    Takes the same injected client the adapter does (ADR-0001), so an
    observation in a test travels the identical request path it travels in
    production and replay gets no privileged route.
    """

    provider_id = PROVIDER_ID
    namespace = OBSERVATION_NAMESPACE
    provenance = PROVENANCE

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        #: Injected so a captured snapshot's instant is reproducible in replay
        #: (constitution §1).
        self._clock = clock or (lambda: datetime.now(UTC))

    async def capture(self, workspace_id: str) -> Observation:
        """Capture this workspace's authoritative target state.

        Raises rather than returning a partial observation. Constitution §5:
        "observation failure produces an explicit non-pass result; it never
        degrades to success" - and an observation provider that returned an
        empty payload when the target was unreachable would make every `absent`
        assertion pass against a store nobody could see.
        """
        response = await self._client.get(STATE_PATH, headers={WORKSPACE_HEADER: workspace_id})
        response.raise_for_status()
        document = response.json()
        return self.normalize(document)

    def normalize(self, document: Mapping[str, Any]) -> Observation:
        """Map the store's canonical document onto the `target` namespace.

        `state_version` is lifted *out* of the payload and carried as observation
        metadata, which §9.3 requires: "provider `state_version` remains
        observation metadata rather than business payload". Leaving it inside
        would let a contract assert on `target.state_version` and turn a
        bookkeeping counter into a business value that every mutation changes.
        """
        if not isinstance(document, Mapping) or "target_state" not in document:
            raise ValueError(
                "the store returned no canonical state document; an observation must "
                "not be manufactured from a partial response"
            )
        payload: Mapping[str, JsonValue] = document["target_state"]
        version = document.get("state_version")
        return Observation(
            namespace=self.namespace,
            provider_id=self.provider_id,
            provenance=self.provenance,
            schema_version=SCHEMA_VERSION,
            payload=payload,
            state_version=None if version is None else str(version),
            captured_at=self._clock(),
        )
