"""The Buggy Store `ManagedTargetAdapter` (spec v1.9 §9.1, §13, App. D.2).

BUILD_ORDER §7/M2: "implement prepare/execute/observe only through the target API
client chosen in ADR-0001", and invariant 3: "browser and replay execution use
the same `ManagedTargetAdapter` and the same versioned target API. Neither path
imports Buggy Store service objects."

This module is the boundary the whole milestone exists to establish, so what it
does *not* do matters as much as what it does:

* **It imports no store service object.** Everything travels over
  `/demo/api/v1`. `tests/architecture` proves the store cannot import the
  assurance stack; the adapter test proves the reverse direction by module graph.
* **It constructs no HTTP client.** ADR-0001 gives it one injected
  `httpx.AsyncClient` and makes the composition root own the lifetime, so the
  same adapter reaches a real port in production and an ASGI app in tests with
  no branch of its own.
* **It never builds an `Observation` from a tool response.** Observation is a
  separate read through a separate provider (constitution §4).

The one piece of real judgement here is error translation. FR-033 says "expected
denial, expiry, and cancellation of a protected action are safe terminal outcomes
and shall not be classified as `tool_execution_error`", so a refused checkout
becomes a *completed* invocation reporting `blocked_by_user` or
`blocked_by_expiry` rather than a failure. Getting that wrong would make the
product punish the safe behaviour it exists to encourage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from actionwitness_core.evidence.enums import ToolReportedStatus
from actionwitness_core.journeys.enums import OutcomeEventType
from actionwitness_core.ports.enums import ExecutionMode, SideEffectClass
from actionwitness_core.ports.models import (
    ExecutionContext,
    ScenarioSelection,
    TargetDescriptor,
    TargetToolSpec,
    ToolExecutionResult,
)
from actionwitness_core.security.limits import MAX_TOOL_RESULT_CHARS, bounded_summary
from integrations.buggy_store.observation import (
    WORKSPACE_HEADER,
    BuggyStoreObservationProvider,
)
from integrations.buggy_store.tools import (
    APPLY_DISCOUNT,
    EFFECT_MAP,
    GET_CART,
    PROCEED_TO_CHECKOUT,
    SEARCH_CATALOG,
    TOOL_NAMES,
    TOOL_SPECS,
    UPDATE_CART,
    published_names,
    spec_for,
)

__all__ = ["ADAPTER_ID", "DESCRIPTOR", "TARGET_ID", "BuggyStoreAdapter", "ToolNotAllowed"]

TARGET_ID: Final = "buggy-store"
ADAPTER_ID: Final = "integrations.buggy_store"

#: §9.1: the adapter advertises the modes it supports, and the core validates a
#: selection against this list without interpreting the names.
DESCRIPTOR: Final = TargetDescriptor(
    target_type="managed_application",
    target_id=TARGET_ID,
    execution_mode=ExecutionMode.MANAGED,
    supported_scenario_modes=("pre_fix", "post_fix"),
)

_API: Final = "/demo/api/v1/store"


class ToolNotAllowed(ValueError):
    """A tool outside the published allowlist (FR-015, §20.2).

    Raised before any request is formed. An agent that invents a tool name
    reaches the network never mind the store.
    """


class BuggyStoreAdapter:
    """Drives and observes the Buggy Store over its versioned HTTP API."""

    descriptor = DESCRIPTOR
    adapter_id = ADAPTER_ID

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        #: ADR-0001: injected, never constructed here. The composition root wires
        #: it to a configured base URL in production and to `ASGITransport` in
        #: tests, and the adapter's behaviour is identical either way.
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    # -- TargetAdapter -------------------------------------------------------

    def tool_specs(self) -> Sequence[TargetToolSpec]:
        """The five allowlisted tools of Appendix D.2."""
        return TOOL_SPECS

    def effect_map(self) -> Mapping[str, tuple[str, ...]]:
        """§13.4's declared target-effect prefixes, per tool."""
        return EFFECT_MAP

    def observation_provider(self) -> BuggyStoreObservationProvider:
        """The independent read channel. Never derived from a tool response."""
        return BuggyStoreObservationProvider(self._client, clock=self._clock)

    # -- ManagedTargetAdapter ------------------------------------------------

    async def prepare(self, workspace_id: str, fixture: dict, scenario: ScenarioSelection) -> None:
        """Restore the fixture and select the scenario (§9.1).

        The scenario is validated against this adapter's own descriptor first, so
        a mode the store never advertised is refused here rather than becoming a
        400 from the store with a less useful message.

        A non-empty fixture is refused rather than ignored. This store reseeds to
        one canonical empty state; silently dropping a fixture that asked for
        something else would make an eval replay restore the wrong starting
        point and still report success (§9.8).
        """
        scenario.validate_for(self.descriptor)
        if fixture:
            raise ValueError(
                "the Buggy Store reseeds to its canonical empty state and supports no "
                f"fixture content yet; refusing to silently drop {sorted(fixture)}"
            )
        response = await self._client.post(
            f"{_API}/scenario",
            headers={WORKSPACE_HEADER: workspace_id},
            json={
                "scenario_mode": scenario.scenario_mode,
                "fault_profile": scenario.fault_profile or "none",
            },
        )
        response.raise_for_status()

    async def execute(
        self,
        workspace_id: str,
        tool_name: str,
        arguments: dict,
        context: ExecutionContext,
    ) -> ToolExecutionResult:
        """Execute one allowlisted tool and report what the store said.

        The result is the *self-report* channel: it carries the store's status
        and a bounded summary, and it is never the basis for an assertion. The
        adapter records the canonical state version either side of a mutation so
        FR-032's idempotency and false-success evidence does not depend on the
        tool's own words.
        """
        spec = self._require_allowlisted(tool_name)
        headers = {WORKSPACE_HEADER: workspace_id}

        version_before: str | None = None
        if spec.side_effect is not SideEffectClass.READ_ONLY:
            version_before = await self._read_state_version(workspace_id)

        started = self._clock()
        response = await self._dispatch(spec, arguments, headers, context)
        duration_ms = max(0, int((self._clock() - started).total_seconds() * 1000))

        if response.is_success:
            body = response.json()
            return self._completed(
                spec,
                context,
                body,
                duration_ms=duration_ms,
                version_before=version_before,
            )
        return self._refused(
            spec, context, response, duration_ms=duration_ms, version_before=version_before
        )

    # -- dispatch ------------------------------------------------------------

    async def _dispatch(
        self,
        spec: TargetToolSpec,
        arguments: Mapping[str, Any],
        headers: Mapping[str, str],
        context: ExecutionContext,
    ) -> httpx.Response:
        """Translate one allowlisted tool call into one store request.

        The mapping is exhaustive over the allowlist, so a tool added to
        `TOOL_SPECS` without a route here fails loudly rather than falling
        through to a default that would silently do nothing.
        """
        match spec.name:
            case _ if spec.name == SEARCH_CATALOG:
                return await self._client.get(
                    f"{_API}/catalog",
                    params={
                        "query": arguments.get("query", ""),
                        "max_results": arguments.get("max_results", 3),
                    },
                )
            case _ if spec.name == GET_CART:
                return await self._client.get(f"{_API}/cart", headers=dict(headers))
            case _ if spec.name == UPDATE_CART:
                return await self._client.post(
                    f"{_API}/cart/mutations", headers=dict(headers), json=dict(arguments)
                )
            case _ if spec.name == APPLY_DISCOUNT:
                return await self._client.post(
                    f"{_API}/discount", headers=dict(headers), json=dict(arguments)
                )
            case _ if spec.name == PROCEED_TO_CHECKOUT:
                return await self._client.post(
                    f"{_API}/checkout",
                    headers=dict(headers),
                    json={
                        "confirmation_id": arguments["confirmation_id"],
                        "request_id": arguments["request_id"],
                    },
                )
        raise ToolNotAllowed(  # pragma: no cover - unreachable while the allowlist is closed
            f"{spec.name!r} is allowlisted but has no route"
        )

    # -- result translation --------------------------------------------------

    def _completed(
        self,
        spec: TargetToolSpec,
        context: ExecutionContext,
        body: Mapping[str, Any],
        *,
        duration_ms: int,
        version_before: str | None,
    ) -> ToolExecutionResult:
        status = body.get("status", "success")
        reported = (
            ToolReportedStatus.ALREADY_APPLIED
            if status == "already_applied"
            else ToolReportedStatus.SUCCESS
        )
        version_after = body.get("state_version")
        return ToolExecutionResult(
            tool_name=spec.name,
            terminal_event=OutcomeEventType.TOOL_INVOCATION_COMPLETED,
            reported_status=reported,
            reported_summary=_summarize(spec.name, body),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            duration_ms=duration_ms,
            state_version_before=version_before,
            state_version_after=None if version_after is None else str(version_after),
        )

    def _refused(
        self,
        spec: TargetToolSpec,
        context: ExecutionContext,
        response: httpx.Response,
        *,
        duration_ms: int,
        version_before: str | None,
    ) -> ToolExecutionResult:
        """Translate a store refusal, separating safe blocks from real errors.

        FR-033 draws the line: a denial, an expiry, or a cancellation of a
        protected action is an expected terminal outcome, not a tool execution
        error. Those become *completed* invocations with a blocked status, which
        is what lets §23.1's execution layer report `blocked_safely` and the
        consent policy pass rather than the run being marked broken for behaving
        correctly.
        """
        error = _error_of(response)
        code = str(error.get("code", "STORE_ERROR"))
        blocked = _blocked_status(code, error)

        if blocked is not None:
            return ToolExecutionResult(
                tool_name=spec.name,
                terminal_event=OutcomeEventType.TOOL_INVOCATION_COMPLETED,
                reported_status=blocked,
                reported_summary=bounded_summary(
                    str(error.get("message", "")), MAX_TOOL_RESULT_CHARS
                ).text,
                request_id=context.request_id,
                correlation_id=context.correlation_id,
                duration_ms=duration_ms,
                state_version_before=version_before,
                state_version_after=version_before,
            )

        return ToolExecutionResult(
            tool_name=spec.name,
            terminal_event=OutcomeEventType.TOOL_INVOCATION_FAILED,
            reported_summary=bounded_summary(
                str(error.get("message", "")), MAX_TOOL_RESULT_CHARS
            ).text,
            error_code=code.lower(),
            request_id=context.request_id,
            correlation_id=context.correlation_id,
            duration_ms=duration_ms,
            state_version_before=version_before,
            state_version_after=version_before,
        )

    # -- helpers -------------------------------------------------------------

    def _require_allowlisted(self, tool_name: str) -> TargetToolSpec:
        spec = spec_for(tool_name) if isinstance(tool_name, str) else None
        if spec is None or tool_name not in TOOL_NAMES:
            raise ToolNotAllowed(
                f"{tool_name!r} is not published by the Buggy Store adapter; "
                f"allowlisted tools are {list(published_names())}"
            )
        return spec

    async def _read_state_version(self, workspace_id: str) -> str | None:
        """The canonical version before a mutation, for FR-032 evidence."""
        response = await self._client.get(f"{_API}/state", headers={WORKSPACE_HEADER: workspace_id})
        if not response.is_success:
            return None
        version = response.json().get("state_version")
        return None if version is None else str(version)


#: Store error codes that mean "safely refused" rather than "failed" (FR-033).
_BLOCKED_CONFIRMATION_STATUSES: Final[Mapping[str, ToolReportedStatus]] = {
    "denied": ToolReportedStatus.BLOCKED_BY_USER,
    "cancelled": ToolReportedStatus.BLOCKED_BY_USER,
    "expired": ToolReportedStatus.BLOCKED_BY_EXPIRY,
}


def _blocked_status(code: str, error: Mapping[str, Any]) -> ToolReportedStatus | None:
    """Whether this refusal is a safe block, and which kind.

    Only a consent refusal qualifies, and only when the store said which kind it
    was. An unexplained `CONFIRMATION_REQUIRED` - a checkout attempted with no
    approval at all - stays a failure, because nothing was safely refused: the
    caller never asked a human.
    """
    if code != "CONFIRMATION_REQUIRED":
        return None
    status = str(error.get("details", {}).get("status", ""))
    return _BLOCKED_CONFIRMATION_STATUSES.get(status)


def _error_of(response: httpx.Response) -> Mapping[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {"code": "STORE_ERROR", "message": f"HTTP {response.status_code}"}
    error = body.get("error") if isinstance(body, Mapping) else None
    return error if isinstance(error, Mapping) else {"code": "STORE_ERROR", "message": ""}


def _summarize(tool_name: str, body: Mapping[str, Any]) -> str:
    """A compact result summary within §11.4's 1,500-character budget.

    §23.3: "WebMCP tool outputs should return only a compact summary and IDs.
    Full report content is retrieved through the visible UI or report endpoint."
    The evidence lives server-side; this is what an agent reads.
    """
    parts = [f"{tool_name}: {body.get('status', 'ok')}"]
    if "state_version" in body:
        parts.append(f"state_version={body['state_version']}")
    cart = body.get("cart")
    if isinstance(cart, Mapping):
        parts.append(f"total={cart.get('total')}")
        parts.append(f"lines={len(cart.get('items', {}))}")
    if body.get("order_id"):
        parts.append(f"order_id={body['order_id']}")
    if isinstance(body.get("products"), list):
        parts.append(f"matches={len(body['products'])}")
    return bounded_summary(" ".join(parts), MAX_TOOL_RESULT_CHARS).text
