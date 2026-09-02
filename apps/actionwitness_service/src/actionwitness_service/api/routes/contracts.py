"""Contract template, read, and selection routes (spec v1.9 §15.2).

| Method | Endpoint                        |
|--------|---------------------------------|
| `GET`  | `/contracts/templates`          |
| `POST` | `/contracts`                    |
| `GET`  | `/contracts/{contract_id}`      |
| `POST` | `/contracts/{contract_id}/select` |

§15.2's remaining two endpoints — `/from-candidates` and `/published` — belong
to later milestones and are deliberately absent rather than stubbed.

`POST /contracts` is the only one that takes a body, and it takes §25.2's four
flat scalars. FR-024 forbids combining a contract with a different target, and
the surest way to honour that is to give the *selection* route nothing to
combine: the target comes from the contract's own immutable `target_id`, never
from a request.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Path
from pydantic import BaseModel, ConfigDict, StringConstraints

from actionwitness_service.api.dependencies import (
    CatalogueDependency,
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.contract_service import ContractService
from actionwitness_service.application.template_catalogue import ExpansionRejected

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
    catalogue: CatalogueDependency,
) -> dict[str, Any]:
    """The built-in templates, which belong to no workspace (FR-020, FR-023).

    Each carries the scalars it allowlists, so the declarative form can render
    exactly the controls that template accepts — a chooser offering a discount
    field for a contract with no discount term would invite a rejection the
    person could have been spared. It is a convenience and never the
    enforcement: `POST /contracts` re-checks the allowlist, because a browser
    deciding which fields are legal would be the client authorizing its own
    input.
    """
    async with database.reading() as work:
        templates = await ContractService(work, workspace_id, registry).list_templates()
    return {
        "templates": [
            {
                **template,
                "parameters": list(catalogue.parameters_for(template["source_template_id"])),
            }
            for template in templates
        ]
    }


class InstantiateContractRequest(BaseModel):
    """§25.2's flat controls, and nothing else (FR-021).

    `extra="forbid"` is the requirement "the declarative form shall never accept
    nested assertions, policies, paths, or arbitrary JSON" said in code. A model
    that ignored unknown keys would accept an `assertions` array and create a
    contract the submitter believes contains it.

    Strings are bounded here because they reach a column, a listing, and an
    error message (§20.2). The *domain* rules — which scalars this template
    allowlists, which quantities and codes are legal — belong to the expansion,
    which is the only thing that knows the template. Both refusals arrive as the
    same `CONTRACT_VALIDATION_FAILED` envelope with field-level details, so a
    client parses one shape.
    """

    model_config = ConfigDict(extra="forbid")

    template_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    contract_name: Annotated[str, StringConstraints(max_length=200)] | None = None
    quantity: int | None = None
    discount_code: Annotated[str, StringConstraints(max_length=64)] | None = None


@router.post("", status_code=201)
async def instantiate_contract(
    body: InstantiateContractRequest,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    registry: RegistryDependency,
    catalogue: CatalogueDependency,
) -> dict[str, Any]:
    """FR-021: expand one trusted template from flat scalars (§15.2).

    Two boundaries, in this order. The catalogue expands — refusing a scalar the
    template does not allowlist rather than ignoring it, because a caller told
    their contract was created would otherwise believe it constrained something
    the template never mentions. Then the core validates the result, because an
    expansion is still a document and §17 stores no contract it has not parsed.

    Nothing the caller sent becomes a contract term. Every assertion, policy,
    path and target comes from the template.
    """
    try:
        document = catalogue.expand(
            body.template_id,
            {
                "contract_name": body.contract_name,
                "quantity": body.quantity,
                "discount_code": body.discount_code,
            },
        )
    except ExpansionRejected as rejected:
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "The template could not be instantiated from those values.",
            details=[{"path": field, "message": message} for field, message in rejected.details],
        ) from rejected

    async with database.transaction() as work:
        return dict(
            await ContractService(work, workspace_id, registry).instantiate(
                document, source_template_id=body.template_id
            )
        )


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
