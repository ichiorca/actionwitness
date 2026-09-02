"""Request-scoped dependencies (FR-006, ADR-0003).

The single rule this module exists to enforce: **the workspace comes from the
server-issued cookie and from nowhere else.** There is deliberately no
dependency here that reads a workspace from a path parameter, a query string, a
body field, or a header. FR-006 and §20.1 make the cookie the only authorization
input, and the reliable way to keep it that way is for no other way to exist.

Note what is *not* provided: a request-scoped `UnitOfWork`. A transaction opened
as a dependency would live for the whole request, including any I/O the handler
does — exactly the "held across a wait" that ADR-0003 forbids. Handlers open
their own, around the work and nothing else.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry
from actionwitness_service.application.artifacts import ArtifactStore
from actionwitness_service.application.template_catalogue import TemplateCatalogue
from actionwitness_service.config import ServiceSettings
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.locks import WorkspaceLocks

__all__ = [
    "ArtifactsDependency",
    "CatalogueDependency",
    "DatabaseDependency",
    "LocksDependency",
    "RegistryDependency",
    "SettingsDependency",
    "WorkspaceDependency",
]


def _workspace_id(request: Request) -> str:
    """The cookie-resolved workspace for this request.

    Absent only on a path the middleware exempts, which a stateful endpoint is
    not — so reaching this branch means a route was mounted under an exempt
    prefix, and failing closed is the right answer to a routing mistake.
    """
    workspace_id: str | None = getattr(request.state, "workspace_id", None)
    if not workspace_id:
        raise ApiError(
            ApiErrorCode.HARNESS_ERROR,
            "The request reached a stateful endpoint without a workspace.",
        )
    return workspace_id


def _database(request: Request) -> Database:
    return request.app.state.database


def _locks(request: Request) -> WorkspaceLocks:
    return request.app.state.locks


def _registry(request: Request) -> AdapterRegistry:
    return request.app.state.adapters


def _artifacts(request: Request) -> ArtifactStore:
    return request.app.state.artifacts


def _catalogue(request: Request) -> TemplateCatalogue:
    """The instantiable templates this deployment composed at startup.

    Built once rather than per request, and from app state rather than by
    importing an integration here: which targets are available is a
    composition decision (§21.1), and a generic dependency that reached for
    a commerce module would make every route depend on one.
    """
    return request.app.state.templates


def _settings(request: Request) -> ServiceSettings:
    """The resolved module configuration.

    Read from app state rather than the environment: 004 resolves settings once
    at startup, and a handler re-reading `os.environ` could disagree with the
    module report the same deployment already published.
    """
    return request.app.state.settings


#: Declared at module scope, not inside a router function. Under
#: `from __future__ import annotations` every annotation is a string resolved
#: against module globals, so a locally-defined alias resolves to nothing and
#: FastAPI silently reinterprets the parameter as a query parameter — which
#: turns an authorization boundary into a 422. Learned the hard way in 003.
WorkspaceDependency = Annotated[str, Depends(_workspace_id)]
DatabaseDependency = Annotated[Database, Depends(_database)]
LocksDependency = Annotated[WorkspaceLocks, Depends(_locks)]
RegistryDependency = Annotated[AdapterRegistry, Depends(_registry)]
ArtifactsDependency = Annotated[ArtifactStore, Depends(_artifacts)]
CatalogueDependency = Annotated[TemplateCatalogue, Depends(_catalogue)]
SettingsDependency = Annotated[ServiceSettings, Depends(_settings)]
