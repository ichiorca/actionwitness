"""The authorized external-surface audit surface (§12.17, §15; 015-T1).

| Method | Endpoint            |
|--------|---------------------|
| `POST` | `/audits`           |
| `GET`  | `/audits/current`   |

Two endpoints and no third, deliberately. There is no "discover origins", no
"scan", and no endpoint that takes a list — §12.17 allows one operator-asserted
origin at a time and the guardrails call ActionWitness "never a crawler". An
affordance that accepted many origins would be the first half of a scanner even
if nothing yet walked the list, so it does not exist.

Nothing here contacts the audited origin. FR-160a puts the observation in the
operator's own browser, which is what keeps this whole feature clear of
server-side request forgery: the only party talking to the audited site is the
person who already has an account there.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field

from actionwitness_service.api.dependencies import (
    DatabaseDependency,
    LocksDependency,
    SettingsDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.audit_service import AuditService

__all__ = ["router"]

router = APIRouter(prefix="/audits", tags=["audits"])


class AuthorizeRequest(BaseModel):
    """One origin, and the assertion that the operator is allowed to audit it.

    `origin` is a single string rather than a list, and that is a design
    decision rather than a simplification: a list is a scan queue with a
    friendlier name.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    origin: Annotated[str, Field(min_length=1, max_length=253 + 16)]
    #: FR-160 records "the assertion timestamp and actor". The timestamp is the
    #: server's; the actor is who the operator says they are, recorded as a
    #: claim rather than trusted as an identity — this deployment has no
    #: accounts, and pretending otherwise would put a name on evidence that
    #: nothing verified.
    asserted_by: Annotated[str, Field(min_length=1, max_length=120)]
    #: An explicit, unignorable affirmation. A boolean that defaults to true
    #: would let a client authorize by omission.
    authorized: bool


@router.post("", status_code=201)
async def assert_authorization(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    settings: SettingsDependency,
    request: Annotated[AuthorizeRequest, Body()],
) -> dict[str, Any]:
    """FR-160: absent authorization there is no audit."""
    if not request.authorized:
        raise ApiError(
            ApiErrorCode.AUDIT_NOT_AUTHORIZED,
            "An audit requires an explicit authorization assertion.",
            details=[{"path": "authorized", "message": "must be true"}],
        )

    async with locks.hold(workspace_id), database.transaction() as work:
        audit = await AuditService(
            work, workspace_id, settings=settings.external_audit
        ).assert_authorization(request.origin, actor=request.asserted_by)
    return audit.as_document()


@router.get("/current")
async def read_current_audit(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """This workspace's live audit, or an explicit absence.

    `null` rather than a 404: the workspace exists and the caller may read it,
    and "you have no audit" is a different answer from "that is not yours".
    """
    async with database.reading() as work:
        audit = await AuditService(work, workspace_id, settings=settings.external_audit).current()
    return {"audit": None if audit is None else audit.as_document()}
