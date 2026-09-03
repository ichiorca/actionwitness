"""Shopify development-store pairing and bridge routes (§15.7, FR-110..FR-119).

| Method | Endpoint                                             | Caller       |
|--------|------------------------------------------------------|--------------|
| `POST` | `/shopify/pairings`                                  | harness UI   |
| `POST` | `/shopify/pairings/{pairing_id}/redeem`              | theme bridge |
| `POST` | `/shopify/pairings/{pairing_id}/observations/before` | theme bridge |
| `POST` | `/shopify/pairings/{pairing_id}/verify`              | theme bridge |
| `GET`  | `/shopify/pairings/{pairing_id}`                     | harness UI   |

**Two callers, two authorizations, and they must not be confused.** §15.7 is
explicit: "The harness UI uses its anonymous workspace cookie to create and
inspect a pairing. The Shopify theme bridge does not receive that cookie: every
bridge request must carry the short-lived bearer credential issued for that
pairing, and FastAPI must also validate the exact configured Shopify `Origin`."

So the two UI routes take `WorkspaceDependency` and never look at a credential,
and the three bridge routes take a bearer credential and an exact `Origin` and
**never take a workspace id from anywhere** — not from a body, not from a path,
not from a cookie that a `SameSite=Strict` browser would not have sent anyway.
FR-006 says the same from the other side: "Shopify bridge endpoints ... never
authorize with a caller-supplied workspace ID." The workspace is *derived* from
the row the credential unlocks, which is stronger than checking a name the
caller supplied, because there is no name to check.

**CORS is defence in depth and never authentication.** The project rules say so
outright: "CORS proves it came from our store" is listed as an excuse, and the
reality beside it is "CORS is defence in depth, not bridge authentication". The
headers below exist so a browser will let the theme read the response at all;
the credential is what decides whether there is a response worth reading. One
exact origin, `Vary: Origin`, no `Access-Control-Allow-Credentials`, and a
preflight naming only the method and the two headers the bridge sends (§20.1).

**Refusals carry the CORS headers too**, which is why the bridge handlers catch
`ApiError` rather than letting `create_app`'s handler build the envelope. That
handler builds the identical body and cannot add the headers — it does not know
which routes are cross-origin — and a refusal a browser will not let the bridge
read arrives there as a bare network error. Every fail-closed answer this module
produces is one the bridge has to be able to tell apart from the others.

**The cart body is size-capped before it is parsed** (FR-112: "accept only JSON
responses up to 256 KiB"). A `cart.js` payload is the one part of these requests
whose size a storefront rather than the operator controls, so the bound has to
precede the JSON parser rather than sit behind it as a validator — the same
shape `routes/audits.py` uses for the same reason.

Every `integrations.shopify` import is inside a function. §21.1 requires the
harness to run with an integration absent from the environment entirely, and a
module-scope import would make this router — and therefore `api/app.py` —
unimportable without it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final

from fastapi import APIRouter, Body, Path, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from actionwitness_service.api.dependencies import (
    ArtifactsDependency,
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    SettingsDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry
from actionwitness_service.application.artifacts import ArtifactStore
from actionwitness_service.application.contract_service import ContractService
from actionwitness_service.application.shopify_pairing import (
    CapturedPhase,
    PairingStatus,
    ShopifyPairingService,
    resolve_shopify_adapter,
)
from actionwitness_service.config import ServiceSettings, ShopifySettings
from actionwitness_service.persistence.database import Database
from actionwitness_service.persistence.locks import WorkspaceLocks

__all__ = ["router"]

router = APIRouter(prefix="/shopify", tags=["shopify"])

#: Bounded because it reaches a `WHERE` clause, a log line, and an error message
#: (§20.2). An unbounded path parameter puts a megabyte into all three.
PairingId = Annotated[str, Path(min_length=1, max_length=128)]

#: `secrets.token_urlsafe(32)` is 43 characters. The bound is generous against
#: that and small enough that an `Authorization` header cannot become a payload.
MAX_CREDENTIAL_CHARS: Final = 256

#: The scheme the bridge presents. Compared case-sensitively: RFC 7235 permits
#: any case, and accepting one spelling keeps this parser from being where a
#: later reader looks for flexibility this module does not want.
_BEARER: Final = "Bearer "

#: §20.1: "allow only the required methods/headers". `POST` because every bridge
#: route is one, and the two headers are exactly what the bridge sends.
_ALLOWED_METHODS: Final = "POST, OPTIONS"
_ALLOWED_HEADERS: Final = "authorization, content-type"
#: Ten minutes. A preflight cached longer than a pairing lives would outlast the
#: thing it was granted for.
_PREFLIGHT_MAX_AGE: Final = "600"

#: Where a finished pairing's outcome report is served from. Written out rather
#: than imported from `api/app.py`, which imports this module: `API_PREFIX`
#: cannot come from there without a cycle. A test fetches this path against the
#: real application, so the duplication cannot rot unnoticed.
_REPORT_PATH: Final = "/api/v1/runs/{run_id}/report"

#: The pairing states that mean a verdict exists and its report was sealed
#: (§16.5). A link offered earlier would name a document nothing has written.
_VERDICT_STATUSES: Final[frozenset[PairingStatus]] = frozenset(
    {PairingStatus.PASSED, PairingStatus.PASSED_WITH_WARNINGS, PairingStatus.FAILED}
)


# --- request models -----------------------------------------------------------


class CreatePairingRequest(BaseModel):
    """Which contract this trial is judged against, if the caller has one.

    `contract_id` is optional because the Shopify contract is *server-expanded*.
    §13.5 fixes the test variant and the expected currency to configuration, and
    `integrations.shopify.templates` therefore publishes them as required server
    parameters rather than as form controls — "a form control for the test
    variant would be the caller choosing which variant counted as correct". The
    generic `POST /contracts` cannot supply them and is refused by name, so when
    no contract is named this route expands the shipped template with
    `ShopifyAdapter.contract_parameters()`, whose values came from
    `ServiceSettings.shopify`.

    A named contract is still accepted, and that is not a loophole: it is scoped
    to the caller's workspace, and `_require_safe_scope` refuses one that drives
    checkout, order creation, customer login, or payment (FR-114) before any
    credential is minted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class RedeemRequest(BaseModel):
    """What the bridge tells the harness about itself (FR-117).

    Both are recorded on the pairing and published by the status endpoint, so
    both are bounded, and neither is trusted for anything: they identify a build,
    they do not authorize a request. FR-117 wants them in the Shopify report's
    provenance too; §23.9's `external_target` block is not composed yet.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bridge_version: Annotated[str, Field(min_length=1, max_length=64)]
    #: "theme/build identifier **when available**" (FR-117). Absent is a real
    #: answer, recorded as absent rather than as an empty string.
    theme_build_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class ObservationRequest(BaseModel):
    """One same-session `cart.js` read, and where the bridge read it from.

    `cart` is the raw `cart.js` body, typed as a plain mapping on purpose: the
    shape belongs to Shopify and to the integration that normalizes it, and a
    Pydantic model here would be a second, quietly diverging definition of a
    payload this module is not allowed to understand. Everything about it is
    decided by `ShopifyCartObservationProvider.normalize`, which refuses a
    payload wearing a tool result's clothes.

    `capture_path` is FR-117's "capture URL path without query or fragment", and
    it carries FR-114's checkout refusal: a bridge that followed a checkout link
    reports a checkout path, and the submission is refused before the payload is
    parsed at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    capture_path: Annotated[str, Field(min_length=1, max_length=2048)]
    cart: dict[str, Any]


_DEFAULT_CREATE: Final = CreatePairingRequest()


# --- the harness UI: cookie-authorized ----------------------------------------


@router.post("/pairings", status_code=201)
async def create_pairing(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
    registry: RegistryDependency,
    settings: SettingsDependency,
    body: Annotated[CreatePairingRequest, Body()] = _DEFAULT_CREATE,
) -> Response:
    """FR-111's one-time credential, in a launch URL the operator opens.

    `Cache-Control: no-store` because this body carries the credential inside
    the fragment of the launch URL (§15.7). A fragment never reaches a server
    and never enters an access log, but it does enter a cache, and a shared
    cache holding a live pairing credential is the one place FR-111's "only its
    hash is persisted" would stop being true.
    """
    configured = _require_configured(settings)
    contract_id = body.contract_id or await _expanded_shopify_contract(
        database, workspace_id, registry
    )
    minted = await _service(database, locks, artifacts, configured).create(
        workspace_id, contract_id
    )
    return JSONResponse(
        status_code=201,
        content={
            "pairing_id": minted.pairing_id,
            "status": PairingStatus.CREATED.value,
            "contract_id": contract_id,
            "store_origin": configured.store_origin,
            "expires_at": minted.expires_at,
            # The credential travels only here, inside the fragment. There is
            # deliberately no separate `credential` field: a client that could
            # read one without parsing a URL is a client that would log one.
            "launch_url": minted.launch_url,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/pairings/{pairing_id}")
async def read_pairing(
    pairing_id: PairingId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """§15.7's status endpoint, scoped to the caller's workspace (FR-006).

    Carries no credential and no digest, by construction rather than by
    filtering: `PairingView` holds neither, so no field added to `as_document()`
    later can publish one.
    """
    configured = _require_configured(settings)
    document = await _service(database, locks, artifacts, configured).status_document(
        workspace_id, pairing_id
    )
    status = PairingStatus(str(document["status"]))
    run_id_value = document.get("run_id")
    run_id = None if run_id_value is None else str(run_id_value)
    report_path = _report_path(status, run_id)
    document["report"] = report_path
    return {"pairing": document, "report_path": report_path}


# --- the theme bridge: credential- and origin-authorized ----------------------


@router.post("/pairings/{pairing_id}/redeem")
async def redeem_pairing(
    http_request: Request,
    pairing_id: PairingId,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
    settings: SettingsDependency,
) -> Response:
    """FR-111: redeemed once, then represented by a session-scoped credential.

    `no-store` for the same reason `create` is: this body carries the only copy
    of the bridge-session credential that will exist outside a digest.
    """
    configured = _require_configured(settings)
    headers = {**_cors(http_request, configured), "Cache-Control": "no-store"}
    try:
        body = _parse(RedeemRequest, await _bounded_body(http_request))
        view, session = await _service(database, locks, artifacts, configured).redeem(
            pairing_id,
            _presented_credential(http_request),
            _presented_origin(http_request),
            bridge_version=body.bridge_version,
            theme_build_id=body.theme_build_id,
        )
    except ApiError as refused:
        return _refusal(http_request, refused, headers)

    return JSONResponse(
        status_code=200,
        content={
            "pairing": view.as_document(),
            # §15.7's "bounded bridge-session configuration", bounded literally:
            # the credential, when it dies, and the one origin a cart may be
            # read from. Nothing here tells the bridge what to assert - the
            # contract is the harness's business, and a bridge that knew it
            # could report the answer instead of observing it.
            "bridge_session_credential": session,
            "expires_at": view.expires_at,
            "store_origin": configured.store_origin,
        },
        headers=headers,
    )


@router.post("/pairings/{pairing_id}/observations/before", status_code=201)
async def capture_initial_cart(
    http_request: Request,
    pairing_id: PairingId,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
    settings: SettingsDependency,
) -> Response:
    """§16.5: "Only the `before` observation may create the associated run."

    FR-116's empty-cart precondition is checked here rather than left to the
    contract: a trial that began with a full cart has no clean baseline, and
    arming it would produce a report whose baseline nobody chose.
    """
    configured = _require_configured(settings)
    headers = _cors(http_request, configured)
    try:
        body = _parse(ObservationRequest, await _bounded_body(http_request))
        captured = await _service(database, locks, artifacts, configured).capture_before(
            pairing_id,
            _presented_credential(http_request),
            _presented_origin(http_request),
            payload=body.cart,
            capture_path=body.capture_path,
        )
    except ApiError as refused:
        return _refusal(http_request, refused, headers)

    return JSONResponse(status_code=201, content=_captured(captured), headers=headers)


@router.post("/pairings/{pairing_id}/verify")
async def verify_pairing(
    http_request: Request,
    pairing_id: PairingId,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
    settings: SettingsDependency,
) -> Response:
    """FR-115: "returns only a compact verdict and run ID".

    Compact is the requirement, not a convenience. The bridge runs inside a
    storefront page beside third-party theme scripts, and every field returned
    there is a field one of them can read; the findings, the report, and the
    evidence stay behind the workspace cookie on the harness origin.
    """
    configured = _require_configured(settings)
    headers = _cors(http_request, configured)
    try:
        body = _parse(ObservationRequest, await _bounded_body(http_request))
        verified = await _service(database, locks, artifacts, configured).verify(
            pairing_id,
            _presented_credential(http_request),
            _presented_origin(http_request),
            payload=body.cart,
            capture_path=body.capture_path,
        )
    except ApiError as refused:
        return _refusal(http_request, refused, headers)

    return JSONResponse(
        status_code=200,
        content={
            "pairing_id": verified.pairing.pairing_id,
            "run_id": verified.pairing.run_id,
            # §16.5's terminal pairing state *is* the verdict: it and the run's
            # terminal state are derived from one `LayerResult` inside one
            # transaction, so a bridge reading this and an operator reading the
            # run cannot be told different things.
            "verdict": str(verified.pairing.status.value),
            "content_hash": verified.content_hash,
            "replayed": verified.replayed,
        },
        headers=headers,
    )


@router.options("/pairings/{pairing_id}/redeem", include_in_schema=False)
@router.options("/pairings/{pairing_id}/observations/before", include_in_schema=False)
@router.options("/pairings/{pairing_id}/verify", include_in_schema=False)
async def bridge_preflight(
    http_request: Request,
    pairing_id: PairingId,
    settings: SettingsDependency,
) -> Response:
    """§20.1's preflight, for the three routes a storefront page calls.

    Answers 204 whatever the origin, and grants nothing unless the origin is the
    configured one — `_cors` omits `Access-Control-Allow-Origin` for anybody
    else, which is what a browser reads as a refusal. Answering 403 instead
    would tell a caller that has presented no credential at all whether a
    pairing id exists, since a preflight carries none.
    """
    configured = _require_configured(settings)
    return Response(
        status_code=204,
        headers={
            **_cors(http_request, configured),
            "Access-Control-Allow-Methods": _ALLOWED_METHODS,
            "Access-Control-Allow-Headers": _ALLOWED_HEADERS,
            "Access-Control-Max-Age": _PREFLIGHT_MAX_AGE,
        },
    )


# --- composition --------------------------------------------------------------


def _require_configured(settings: ServiceSettings) -> ShopifySettings:
    """The module gate, restated at every entry point.

    `api/app.py` mounts this router only when the module is on, so this is
    unreachable in a composed application. It is here because "unreachable
    because of how somebody else wired it" is not a property this module can
    check, and the failure it would otherwise produce is an `AttributeError` on
    `None` reaching the generic 500 handler — which tells an operator nothing
    (§21.1 asks for a named unavailable state instead).
    """
    configured = settings.shopify
    if configured is None:
        raise ApiError(
            ApiErrorCode.TARGET_UNAVAILABLE,
            "The Shopify development-store target is not configured in this deployment.",
            details=[{"path": "target", "message": "no shopify store is configured"}],
        )
    return configured


def _service(
    database: Database,
    locks: WorkspaceLocks,
    artifacts: ArtifactStore,
    configured: ShopifySettings,
) -> ShopifyPairingService:
    """One pairing service for one request, with the configured adapter."""
    return ShopifyPairingService(
        database,
        locks,
        artifacts,
        settings=configured,
        adapter=resolve_shopify_adapter(configured),
    )


async def _expanded_shopify_contract(
    database: Database, workspace_id: str, registry: AdapterRegistry
) -> str:
    """Store §13.5's contract, expanded from server configuration, and return its id.

    The variant and the currency come from the adapter the registry built out of
    `ServiceSettings` — never from the request — so "which variant counted as
    correct" is a deployment decision by construction (FR-110, project rules).
    `ContractService.instantiate` then does what it does for every other target:
    parse the document, validate it against the named adapter, hash it, and
    store it once. Nothing here is a second contract-writing path.

    The contract commits in its own transaction, before the pairing's. A `create`
    that then trips FR-008's ceiling leaves a contract nobody paired against,
    which is untidy and deliberately not guarded: contracts are immutable records
    with no ceiling of their own, and the alternative is an advisory pre-check
    that would have to be re-run inside the committing transaction anyway.
    """
    from integrations.shopify.adapter import TARGET_ID
    from integrations.shopify.templates import TEMPLATE_ID, TemplateExpansionError, expand

    slot = registry.resolve(TARGET_ID)
    if slot is None or slot.factory is None:
        raise ApiError(
            ApiErrorCode.TARGET_UNAVAILABLE,
            "The Shopify development-store adapter is not available, so no contract "
            "could be prepared for a pairing.",
            details=[{"path": "target", "message": "no shopify adapter is registered"}],
        )

    try:
        document = expand(TEMPLATE_ID, slot.factory().contract_parameters())
    except TemplateExpansionError as rejected:  # pragma: no cover - configuration is validated
        # Reached only if `ServiceSettings` admitted a variant or currency the
        # template refuses, which config parsing already prevents. Named rather
        # than allowed to escape, because a 500 here would blame the request for
        # a deployment's configuration.
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "The Shopify contract could not be prepared from this deployment's "
            "configured variant and currency.",
            details=[{"path": field, "message": message} for field, message in rejected.details],
        ) from rejected

    async with database.transaction() as work:
        created = await ContractService(work, workspace_id, registry).instantiate(
            document, source_template_id=TEMPLATE_ID
        )
    return str(created["contract_id"])


# --- bridge authorization -----------------------------------------------------


def _presented_credential(request: Request) -> str:
    """The bearer credential, or the same refusal a wrong one gets.

    Deliberately indistinguishable from a credential that simply did not match:
    a caller who could tell "you sent no header" from "that pairing is finished"
    can enumerate pairings one guess at a time, which is the reasoning
    `shopify_pairing._credential_refused` already records.
    """
    presented = request.headers.get("authorization") or ""
    if not presented.startswith(_BEARER):
        raise _not_authorized()
    credential = presented[len(_BEARER) :].strip()
    if not credential or len(credential) > MAX_CREDENTIAL_CHARS:
        raise _not_authorized()
    return credential


def _presented_origin(request: Request) -> str:
    """The `Origin` header, unexamined.

    Compared against the configured store by the service, which owns the one
    equality check FR-110 allows. An absent header becomes an empty string and
    fails that comparison, which is the right direction: a bridge runs in a
    browser, a browser always sends `Origin` on a cross-origin `POST`, and a
    request without one did not come from the storefront page.
    """
    return (request.headers.get("origin") or "").strip()


def _not_authorized() -> ApiError:
    return ApiError(
        ApiErrorCode.AUDIT_NOT_AUTHORIZED,
        "That pairing credential was not accepted. Nothing was captured.",
        details=[{"path": "authorization", "message": "credential is not valid for this pairing"}],
    )


def _cors(request: Request, configured: ShopifySettings) -> dict[str, str]:
    """§20.1's Shopify clause, as headers.

    `Vary: Origin` unconditionally, because the answer *does* depend on the
    request's origin and a cache that did not know would serve the granted
    response to somebody else. `Access-Control-Allow-Origin` only on exact
    equality with the configured store — never a wildcard, never an echo of what
    the caller sent, which is the same string with none of the meaning. And no
    `Access-Control-Allow-Credentials` at all: §20.1 says "omit
    credentialed-cookie CORS", and the bridge authorizes with a bearer
    credential precisely so that it never needs the harness cookie.
    """
    presented = (request.headers.get("origin") or "").strip()
    headers = {"Vary": "Origin"}
    if presented and presented == configured.store_origin:
        headers["Access-Control-Allow-Origin"] = configured.store_origin
    return headers


# --- request and response plumbing --------------------------------------------


async def _bounded_body(request: Request) -> bytes:
    """FR-112's 256 KiB, applied before the JSON parser sees anything."""
    from integrations.shopify.audit import MAX_CART_PAYLOAD_BYTES

    raw = await request.body()
    if len(raw) > MAX_CART_PAYLOAD_BYTES:
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "That submission is larger than this deployment accepts.",
            details=[{"path": "body", "message": f"over {MAX_CART_PAYLOAD_BYTES} bytes"}],
        )
    return raw


def _parse[Body_: BaseModel](model: type[Body_], raw: bytes) -> Body_:
    """Validate a bridge body, forwarding no part of it back.

    Pydantic's own error carries `input`, which on these routes is a
    storefront's payload — untrusted text that would then be echoed into a
    response and from there into whatever logs it. Only the fact of the refusal
    crosses back.
    """
    try:
        return model.model_validate_json(raw)
    except ValidationError as invalid:
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "That request body is not in an acceptable shape.",
            details=[{"path": "body", "message": "does not match the expected schema"}],
        ) from invalid


def _refusal(request: Request, error: ApiError, headers: Mapping[str, str]) -> JSONResponse:
    """§15.8's envelope, plus this route's CORS headers.

    `create_app`'s handler builds the identical body and cannot add the headers,
    so a refusal routed through it reaches the bridge as an opaque network
    failure. `error_code` is recorded here for the same reason the handler
    records it: §21.5 classifies refusals, and a response built outside the
    handler would otherwise be logged as an unclassified 4xx.
    """
    request.state.error_code = error.code.value
    return JSONResponse(
        status_code=error.http_status, content=error.as_envelope(), headers=dict(headers)
    )


def _captured(captured: CapturedPhase) -> dict[str, Any]:
    """§15.7's idempotent capture result.

    `replayed` is what makes idempotency legible: a repeat carrying the same
    content hash gets `true` and the existing result, rather than a second
    capture or a refusal.
    """
    return {
        "pairing_id": captured.pairing.pairing_id,
        "run_id": captured.pairing.run_id,
        "status": str(captured.pairing.status.value),
        "content_hash": captured.content_hash,
        "replayed": captured.replayed,
    }


def _report_path(status: PairingStatus, run_id: str | None) -> str | None:
    """Where this pairing's outcome report is served from, once one exists."""
    if run_id is None or status not in _VERDICT_STATUSES:
        return None
    return _REPORT_PATH.format(run_id=run_id)
