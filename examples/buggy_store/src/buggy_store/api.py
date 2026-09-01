"""The store's versioned HTTP API (spec v1.9 §15.5).

Mounted at `/demo/api/v1` in the composed image and served on its own port when
the store runs alone. §15.5 is explicit about who calls it: "only the Buggy
Store's own ordinary human UI and `integrations.buggy_store` call this surface.
Browser WebMCP handlers first call the harness invocation endpoint... no core or
harness React code calls demo service objects."

Every request body is a Pydantic model that forbids unknown fields, and every
failure leaves through one envelope. Both matter more than they look:

* This is a *public* surface reachable from a browser. "The frontend already
  checked it" is on the constitution's list of excuses, so the store revalidates
  everything the schema promises.
* §15.8 forbids internal details reaching a browser tool. An unhandled exception
  would become a 500 carrying a traceback, so `StoreError` is mapped explicitly
  and nothing else is allowed to escape a handler.

**Workspace scoping.** The endpoint table in §15.5 does not name a mechanism, so
`X-Workspace-Id` is project-allocated. It is the store's *isolation* boundary,
not an authorization one: the harness decides who may reach this surface at all,
and §20.1's rule that a workspace ID is never an authorization mechanism is a
statement about the harness's cookie, which sits in front of this.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from buggy_store.catalog import MAX_SEARCH_RESULTS
from buggy_store.errors import StoreError
from buggy_store.failure_injection import IMPLEMENTED_PROFILES, FaultProfile, ScenarioMode
from buggy_store.money import format_amount
from buggy_store.repository import StoreRepository
from buggy_store.service import (
    DEFAULT_EXPIRY_SECONDS,
    MAX_EXPIRY_SECONDS,
    MAX_REQUEST_ID,
    MIN_EXPIRY_SECONDS,
    MIN_REQUEST_ID,
    StoreService,
)

__all__ = ["API_PREFIX", "create_app", "main"]

#: §15.5 mounts the store under this prefix in the composed deployment.
API_PREFIX: Final = "/demo/api/v1"

#: Project-allocated; see the module docstring.
WORKSPACE_HEADER: Final = "X-Workspace-Id"

type WorkspaceId = Annotated[str, StringConstraints(min_length=1, max_length=128)]
type RequestId = Annotated[
    str, StringConstraints(min_length=MIN_REQUEST_ID, max_length=MAX_REQUEST_ID)
]


class StoreRequest(BaseModel):
    """Base for every request body: unknown fields are refused, not ignored."""

    model_config = ConfigDict(extra="forbid")


class CartMutationRequest(StoreRequest):
    """Appendix D.2's `update_cart` arguments."""

    product_id: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    quantity: Annotated[int, Field(ge=0, le=5)]
    request_id: RequestId


class DiscountRequest(StoreRequest):
    """Appendix D.2's `apply_discount` arguments. No request ID by design."""

    code: Annotated[str, StringConstraints(min_length=1, max_length=32)]


class ConfirmationRequestBody(StoreRequest):
    """Opening a confirmation (§14.1). Expiry is bounded by FR-062."""

    expires_in_seconds: Annotated[int, Field(ge=MIN_EXPIRY_SECONDS, le=MAX_EXPIRY_SECONDS)] = (
        DEFAULT_EXPIRY_SECONDS
    )


class ScenarioRequest(StoreRequest):
    """Selecting the scenario mode and fault profile (FR-011, FR-017).

    Both arrive as plain strings and are validated by the service against the
    closed enums, so an unimplemented-but-recognised profile can be refused with
    its own code rather than as an unknown value.
    """

    scenario_mode: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    fault_profile: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "none"


class DecisionRequest(StoreRequest):
    """§14 step 4. There is no default: a decision is made, never assumed."""

    approved: bool


class CheckoutRequest(StoreRequest):
    """Appendix D.2's `proceed_to_checkout` arguments."""

    confirmation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    request_id: RequestId


async def _workspace_id(
    x_workspace_id: Annotated[str, Header(alias=WORKSPACE_HEADER, min_length=1, max_length=128)],
) -> str:
    """Every stateful request names its workspace; there is no implicit default.

    A default would silently pool two shoppers into one cart the first time a
    caller forgot the header, which is the isolation failure this whole
    milestone is built to avoid. An *empty* header is refused for the same
    reason: it is not a missing header, so it would sail past a presence check
    and key a real workspace on the empty string.
    """
    return x_workspace_id


#: Declared at module scope, not inside `create_app`. `from __future__ import
#: annotations` turns every annotation into a string, and FastAPI resolves those
#: against module globals - a local alias is invisible there, and the parameter
#: silently degrades into a query argument that no caller sends.
WorkspaceDependency = Annotated[str, Depends(_workspace_id)]


def create_app(*, database_path: Path | str, service: StoreService | None = None) -> FastAPI:
    """Build the store's ASGI application.

    `service` is injectable so tests can supply an injected clock and identifier
    source without reaching into the app afterwards - and so ADR-0001's
    `ASGITransport` path exercises the same object graph production uses.
    """
    repository = StoreRepository(database_path)
    resolved = service or StoreService(repository)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ADR-0003: the migration runner runs once at startup. Repositories
        # never create their own tables.
        await repository.initialize()
        yield

    app = FastAPI(
        title="Buggy Store",
        version="1.0",
        summary="A deterministic demo storefront with injectable failure profiles.",
        lifespan=lifespan,
    )
    app.state.service = resolved

    @app.exception_handler(StoreError)
    async def _store_error(request: Request, error: StoreError) -> JSONResponse:
        """One envelope for every deliberate failure (§15.8).

        Registered rather than repeated per route: a handler that formatted its
        own error would eventually format one differently, and the adapter
        branches on this shape.
        """
        return JSONResponse(status_code=error.http_status, content=error.as_envelope())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only. Carries no state, no version, and no configuration."""
        return {"status": "ok"}

    @app.get(f"{API_PREFIX}/store/catalog")
    async def read_catalog(query: str = "", max_results: int = 3) -> dict[str, Any]:
        products = (
            resolved.search(query, max_results)
            if query
            else resolved.search("mug notebook tote", MAX_SEARCH_RESULTS)
        )
        return {
            "products": [
                {
                    "product_id": product.product_id,
                    "line_key": product.line_key,
                    "name": product.name,
                    "price": format_amount(product.price),
                    "stock": product.stock,
                }
                for product in products
            ]
        }

    @app.get(f"{API_PREFIX}/store/scenario")
    async def read_scenario(workspace_id: WorkspaceDependency) -> dict[str, Any]:
        """The current selection, with its label and description (FR-017).

        FR-017 asks the configuration panel to show "a concise explanation,
        active fault behavior, and supported modes", so the store publishes all
        three rather than making every caller carry its own copy of §13.3.
        """
        scenario = await resolved.read_scenario(workspace_id)
        return {
            **scenario.as_document(),
            "supported_scenario_modes": [mode.value for mode in ScenarioMode],
            "implemented_fault_profiles": sorted(item.value for item in IMPLEMENTED_PROFILES),
            "recognized_fault_profiles": [item.value for item in FaultProfile],
        }

    @app.post(f"{API_PREFIX}/store/scenario")
    async def select_scenario(
        workspace_id: WorkspaceDependency, body: ScenarioRequest
    ) -> dict[str, Any]:
        """Select the mode and profile, reseeding mutable state (FR-018).

        Project-allocated: §15.5's endpoint table predates the adapter needing a
        way to perform `ManagedTargetAdapter.prepare`, which §9.1 defines as
        restoring a fixture *and* selecting a scenario. Both happen here.
        """
        scenario = await resolved.select_scenario(
            workspace_id, body.scenario_mode, body.fault_profile
        )
        return scenario.as_document()

    @app.get(f"{API_PREFIX}/store/cart")
    async def read_cart(workspace_id: WorkspaceDependency) -> dict[str, Any]:
        state = await resolved.read_state(workspace_id)
        return {
            "state_version": state.state_version,
            "cart": state.target_state.cart.canonical_document(),
            "order": state.target_state.order.canonical_document(),
        }

    @app.get(f"{API_PREFIX}/store/state")
    async def read_state(workspace_id: WorkspaceDependency) -> dict[str, Any]:
        """The complete canonical state document (§13.2).

        Project-allocated alongside §15.5's "read cart state". The observation
        provider needs the *whole* target state, not the cart: §13.2 includes
        `preferences` precisely so a journey can change a path no cart contract
        asserts, and an observation that could not see it would make §12.16's
        undeclared-change detection structurally impossible.
        """
        state = await resolved.read_state(workspace_id)
        return state.canonical_document()

    @app.post(f"{API_PREFIX}/store/cart/mutations")
    async def mutate_cart(
        workspace_id: WorkspaceDependency, body: CartMutationRequest
    ) -> dict[str, Any]:
        outcome = await resolved.update_cart(
            workspace_id, body.product_id, body.quantity, body.request_id
        )
        return dict(outcome.response)

    @app.post(f"{API_PREFIX}/store/discount")
    async def apply_discount(
        workspace_id: WorkspaceDependency, body: DiscountRequest
    ) -> dict[str, Any]:
        outcome = await resolved.apply_discount(workspace_id, body.code)
        return dict(outcome.response)

    @app.post(f"{API_PREFIX}/store/checkout/confirmations", status_code=201)
    async def open_confirmation(
        workspace_id: WorkspaceDependency, body: ConfirmationRequestBody
    ) -> dict[str, Any]:
        confirmation = await resolved.request_confirmation(
            workspace_id, expires_in_seconds=body.expires_in_seconds
        )
        return confirmation.as_document()

    @app.post(f"{API_PREFIX}/store/checkout/confirmations/{{confirmation_id}}/decision")
    async def decide_confirmation(
        workspace_id: WorkspaceDependency, confirmation_id: str, body: DecisionRequest
    ) -> dict[str, Any]:
        confirmation = await resolved.decide_confirmation(
            workspace_id, confirmation_id, approved=body.approved
        )
        return confirmation.as_document()

    @app.delete(f"{API_PREFIX}/store/checkout/confirmations/{{confirmation_id}}")
    async def cancel_confirmation(
        workspace_id: WorkspaceDependency, confirmation_id: str
    ) -> dict[str, Any]:
        confirmation = await resolved.cancel_confirmation(workspace_id, confirmation_id)
        return confirmation.as_document()

    @app.post(f"{API_PREFIX}/store/checkout")
    async def checkout(workspace_id: WorkspaceDependency, body: CheckoutRequest) -> dict[str, Any]:
        outcome = await resolved.checkout(
            workspace_id,
            confirmation_id=body.confirmation_id,
            request_id=body.request_id,
        )
        return dict(outcome.response)

    return app


def main() -> int:
    """Console entry point for the standalone store (`buggy-store`).

    Runs the store on its own, with no assurance package installed and no
    harness reachable - which is the point of §26.7 and the exit gate's first
    item.
    """
    import os

    import uvicorn

    database = os.environ.get("BUGGY_STORE_DATABASE", "buggy-store.sqlite3")
    port = int(os.environ.get("BUGGY_STORE_PORT", "8001"))
    uvicorn.run(create_app(database_path=database), host="127.0.0.1", port=port)
    return 0


if __name__ == "__main__":  # pragma: no cover - console entry point
    raise SystemExit(main())
