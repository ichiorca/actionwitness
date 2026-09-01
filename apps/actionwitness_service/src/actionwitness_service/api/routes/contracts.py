"""Contract template, read, and selection routes (spec v1.9 §15.2).

| Method | Endpoint                        |
|--------|---------------------------------|
| `GET`  | `/contracts/templates`          |
| `GET`  | `/contracts/{contract_id}`      |
| `POST` | `/contracts/{contract_id}/select` |

§15.2's other three endpoints — `POST /contracts` (instantiate from a template),
`/from-candidates`, and `/published` — belong to M4 and M8 and are deliberately
absent rather than stubbed.

There is no request body on any of these. FR-024 forbids combining a contract
with a different target, and the surest way to honour that is to give the
selection route nothing to combine: the target comes from the contract's own
immutable `target_id`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path

from actionwitness_service.api.dependencies import (
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    WorkspaceDependency,
)
from actionwitness_service.application.contract_service import ContractService

__all__ = ["router"]

router = APIRouter(prefix="/contracts", tags=["contracts"])

#: Bounded because it reaches a `WHERE` clause, a log line, and an error
#: message. An unbounded path parameter is a way to put a megabyte into all
#: three (§20.2).
ContractId = Annotated[str, Path(min_length=1, max_length=128)]


@router.get("/templates")
async def list_templates(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    registry: RegistryDependency,
) -> dict[str, Any]:
    """The built-in templates, which belong to no workspace (FR-020, FR-023)."""
    async with database.reading() as work:
        templates = await ContractService(work, workspace_id, registry).list_templates()
    return {"templates": templates}


@router.get("/{contract_id}")
async def read_contract(
    contract_id: ContractId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    registry: RegistryDependency,
) -> dict[str, Any]:
    """One immutable contract record (§15.2).

    Scoped to the caller's workspace or to a global template, so an identifier
    learned from elsewhere reads as a 404 (FR-006).
    """
    async with database.reading() as work:
        return dict(await ContractService(work, workspace_id, registry).read(contract_id))


@router.post("/{contract_id}/select")
async def select_contract(
    contract_id: ContractId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    registry: RegistryDependency,
) -> dict[str, Any]:
    """FR-024: exactly one active contract, and its target selected with it."""
    async with locks.hold(workspace_id), database.transaction() as work:
        return dict(await ContractService(work, workspace_id, registry).select(contract_id))
