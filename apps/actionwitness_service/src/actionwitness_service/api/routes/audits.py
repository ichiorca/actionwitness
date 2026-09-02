"""The authorized external-surface audit surface (§12.17, §15; 015-T1).

| Method | Endpoint                    |
|--------|-----------------------------|
| `POST` | `/audits`                   |
| `GET`  | `/audits/current`           |
| `GET`  | `/audits/packs`             |
| `POST` | `/audits/current/evidence`  |
| `GET`  | `/audits/current/report`    |
| `POST` | `/audits/current/cancel`    |

**No endpoint here takes more than one origin, and none takes any.** That was
the original rule and it is unchanged: §12.17 allows one operator-asserted
origin at a time and the guardrails call ActionWitness "never a crawler", so an
affordance accepting many origins would be the first half of a scanner even if
nothing yet walked the list. The audited origin is named once, in the
authorization, and every later call refers to *the workspace's* audit rather
than naming a target again.

The four endpoints below the first two are what makes an authorized audit
finishable. Until they existed an operator could assert authorization, receive a
201, and then have nothing to call: the classifier and the report composer were
reachable only from tests, and the audit sat in `authorized` until the workspace
aged out — holding the workspace's one live-audit slot the whole time.

`/audits/packs` is a static catalogue, not a query: a client holds its own
`getTools()` result and decides locally which packs its surface satisfies, so
nothing here accepts a list of tools. FR-161 requires the pack to be *offered*
and selected explicitly, which is why `evidence` demands a `pack_id` rather than
matching one on the operator's behalf.

Nothing here contacts the audited origin. FR-160a puts the observation in the
operator's own browser, which is what keeps this whole feature clear of
server-side request forgery: the only party talking to the audited site is the
person who already has an account there. `evidence` is the transcript of what
that browser saw, and every part of it is treated as untrusted input.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from actionwitness_service.api.dependencies import (
    ArtifactsDependency,
    DatabaseDependency,
    LocksDependency,
    SettingsDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.artifacts import ArtifactCorrupt
from actionwitness_service.application.audit_service import AuditService
from actionwitness_service.application.audit_workflow import (
    AUDIT_REPORT,
    AUDIT_REPORT_SCHEMA_VERSION,
    offered_packs,
    run_audit_pass,
)
from actionwitness_service.config import ExternalAuditSettings

__all__ = ["router"]

router = APIRouter(prefix="/audits", tags=["audits"])

#: Names neither the path nor the hash. Together those are exactly what somebody
#: would need to forge a replacement that passed verification, so a corruption
#: refusal says what happened and not where to look.
_CORRUPT_REPORT = "The stored audit report failed its integrity check and was not served."

#: A storefront's tool list, not a payload. Ten tools is the documented Shopify
#: surface; the bound is generous against that and still small enough that an
#: enumeration cannot become a channel for bulk data.
MAX_ENUMERATED_TOOLS = 100


def _require_enabled(settings: Any) -> ExternalAuditSettings:
    """The module gate, stated once for every endpoint that needs it.

    Same refusal as an unauthorized origin, deliberately: a deployment that has
    the module switched off should not answer differently from one that has it
    on and refuses this caller, or the difference enumerates deployments.
    """
    configured = settings.external_audit
    if configured is None:
        raise ApiError(
            ApiErrorCode.AUDIT_NOT_AUTHORIZED,
            "External auditing is not enabled in this deployment.",
            details=[{"path": "origin", "message": "EXTERNAL_AUDIT_ENABLED is off"}],
        )
    return configured


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


class EvidenceRequest(BaseModel):
    """One browser's transcript of one audit pass.

    Every field is what the operator's browser saw, so every field is untrusted:
    `enumerated` is `getTools()`, `reports` is what each exercised tool *claimed*,
    and the two payloads are raw `cart.js` reads that the target adapter — not
    this model — decides whether to believe.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: FR-161: the operator's explicit selection. Required, because `match_pack`
    #: deliberately returns every match rather than choosing, and choosing here
    #: would decide on their behalf whether a write path gets exercised.
    pack_id: Annotated[str, Field(min_length=1, max_length=64)]
    #: The surface as the browser enumerated it. Bounded because an enumeration
    #: is a storefront's tool list, not a payload.
    enumerated: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=128)]],
        Field(max_length=MAX_ENUMERATED_TOOLS),
    ]
    #: What each exercised tool reported about itself. Absent means not
    #: exercised, which the classifier reports as such rather than as a pass.
    reports: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: The independent reads, before and after. `None` is meaningful and is not
    #: the same as `{}`: it says no independent channel existed, and §12.17 then
    #: requires `observation_unavailable` rather than trust in the tool's word.
    observed_before: dict[str, Any] | None = None
    observed_after: dict[str, Any] | None = None


@router.get("/packs")
async def list_audit_packs(
    workspace_id: WorkspaceDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """The built-in contract packs, for FR-161's "shall be offered".

    Requires the module to be enabled but not an audit to exist: an operator
    chooses a pack while deciding whether to audit at all, and refusing the
    catalogue until after authorization would put the choice after the
    commitment.
    """
    _require_enabled(settings)
    return {"packs": offered_packs()}


@router.post("/current/evidence", status_code=201)
async def submit_audit_evidence(
    http_request: Request,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """§12.17's audit pass: classify the browser's transcript, seal the report.

    The body is read as bytes and size-checked *before* parsing (FR-117). A
    `cart.js` payload is the one part of this request whose size an audited
    storefront controls rather than the operator, so the cap has to precede the
    JSON parser rather than sit behind it as a validator.
    """
    _require_enabled(settings)

    # Imported here, not at module scope: §21.1 requires the harness to run with
    # an integration absent from the environment entirely, and a top-level
    # import would make this whole router unimportable without it. Same seam the
    # evaluator import uses.
    from integrations.shopify.audit import MAX_CART_PAYLOAD_BYTES

    raw = await http_request.body()
    if len(raw) > MAX_CART_PAYLOAD_BYTES:
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "That audit submission is larger than this deployment accepts.",
            details=[{"path": "body", "message": f"over {MAX_CART_PAYLOAD_BYTES} bytes"}],
        )
    try:
        request = EvidenceRequest.model_validate_json(raw)
    except ValidationError as invalid:
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "That audit submission is not a valid evidence document.",
            details=[{"path": "body", "message": "does not match the evidence schema"}],
        ) from invalid

    # Read the live audit, then classify with no transaction open: the pass is
    # pure computation and the file write that follows it must not sit inside
    # `BEGIN IMMEDIATE` (ADR-0003). The workspace lock spans both, so the audit
    # cannot be completed or cancelled underneath this one.
    async with locks.hold(workspace_id):
        async with database.reading() as work:
            audit = await AuditService(
                work, workspace_id, settings=settings.external_audit
            ).require_live()

        outcome = run_audit_pass(
            authorized_origin=audit.authorized_origin,
            pack_id=request.pack_id,
            enumerated=request.enumerated,
            reports=request.reports,
            observed_before=request.observed_before,
            observed_after=request.observed_after,
        )
        written = artifacts.write(
            workspace_id,
            audit.audit_id,
            dict(outcome.report),
            artifact_type=AUDIT_REPORT,
            schema_version=AUDIT_REPORT_SCHEMA_VERSION,
        )

        async with database.transaction() as work:
            artifact_id = await artifacts.record(
                work,
                workspace_id,
                None,
                written,
                metadata={
                    "audit_id": audit.audit_id,
                    "pack_id": outcome.pack_id,
                    "authorized_origin": audit.authorized_origin,
                },
            )
            completed = await AuditService(
                work, workspace_id, settings=settings.external_audit
            ).complete(audit, pack_id=outcome.pack_id, report_artifact_id=artifact_id)

    return {
        "audit": completed.as_document(),
        "report_artifact_id": artifact_id,
        "content_hash": written.content_hash,
        "report": outcome.report,
    }


@router.get("/current/report")
async def read_audit_report(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    artifacts: ArtifactsDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """The stored report, read back from the bytes that were sealed.

    Served from the artifact rather than recomposed, because a report the
    operator shows somebody has to be the one whose hash was recorded — a
    recomposition would be equal today and is not the thing that was sealed.
    """
    _require_enabled(settings)
    async with database.reading() as work:
        service = AuditService(work, workspace_id, settings=settings.external_audit)
        found = await service.completed_report_artifact()
        if found is None:
            raise ApiError(
                ApiErrorCode.RESOURCE_NOT_FOUND,
                "This workspace has not completed an audit.",
            )
        artifact_id, _origin = found
        stored = await artifacts.stored_reference(work, workspace_id, artifact_id)
    if stored is None:  # pragma: no cover - completion commits both together
        raise ApiError(ApiErrorCode.HARNESS_ERROR, _CORRUPT_REPORT)

    relative_path, content_hash = stored
    try:
        # Verified, not merely read. A report an operator forwards to somebody
        # else is only worth forwarding if it is still the document whose hash
        # was recorded — and constitution §5 makes an integrity failure an
        # explicit non-pass rather than a document served with a caveat.
        report = artifacts.verified_document(relative_path, content_hash)
    except ArtifactCorrupt as corrupt:
        # Names neither the path nor the hash: together they are what somebody
        # would need to forge a replacement.
        raise ApiError(ApiErrorCode.HARNESS_ERROR, _CORRUPT_REPORT) from corrupt

    return {"report_artifact_id": artifact_id, "content_hash": content_hash, "report": report}


@router.post("/current/cancel")
async def cancel_current_audit(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """Release the workspace's live-audit slot (§22's `cancelled`)."""
    _require_enabled(settings)
    async with locks.hold(workspace_id), database.transaction() as work:
        cancelled = await AuditService(
            work, workspace_id, settings=settings.external_audit
        ).cancel()
    return {"audit": cancelled.as_document()}


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
