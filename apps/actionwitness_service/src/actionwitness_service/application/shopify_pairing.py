"""Shopify development-store pairing, capture, and verification (§16.5, FR-111).

FR-111, in full, because nearly every clause below is one sentence of it:

> The harness shall create a cryptographically random pairing credential bound to
> workspace, contract, Shopify origin, and a 15-minute expiry. Only its hash is
> persisted. The credential is delivered in a URL fragment... removed from the
> visible URL immediately, redeemed once, and thereafter represented by a bounded
> session-scoped credential. Expired, reused, cross-workspace, or cross-origin
> pairings fail closed without capturing an observation.

**Where a raw credential exists.** In exactly two places, both of them stack
frames: the value `_mint()` returns, and the value a bridge presents in an
`Authorization` header. It is compared against a stored digest and dropped. There
is no column that could hold one (migration 9 stores only `*_token_hash`), no log
field that could carry one (`telemetry.RequestLog` has a closed field set of
identifiers), and no response body that returns one except the two that mint it
— the create response's launch URL and the redeem response's bridge credential,
both `Cache-Control: no-store`. The status endpoint returns neither, and no
artifact is written from anything on this path.

**The hash binds, it does not merely index.** The digest covers the workspace,
the contract, the store origin, *and* the secret, so a credential minted for one
pairing cannot validate against another row even if an attacker could choose
which row it was checked against. FR-111's "cross-workspace or cross-origin
pairings fail closed" is therefore a property of the comparison rather than of a
`WHERE` clause somebody has to remember to write. Plain SHA-256 and not a KDF:
the input carries 256 bits of `secrets` entropy, so there is no guessing space to
slow down, and the constitution's ban on custom cryptography argues for the
boring primitive rather than an invented one.

**Nothing here reads the wall clock.** Expiry is decided against the injected
clock the `UnitOfWork` carries, which is what lets a test age a pairing by
fifteen minutes without sleeping and what makes replay reproducible
(constitution §1).

**ADR-0003 is the shape of every public method.** A submitted observation has to
be normalized (pure), the run has to move (transaction), the contract has to be
evaluated (pure), the report has to be written to disk (no transaction), and the
two terminal states have to commit together (transaction). §16.5 requires that
last step to be atomic — "an atomic repository transaction commits both terminal
states and the report reference, or neither" — which is why `verify` hands
`VerificationService` a sealing callback rather than updating the pairing
afterwards in a transaction of its own.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Protocol, runtime_checkable

from actionwitness_core.journeys.enums import (
    EventActor,
    OutcomeEventType,
    RunState,
    SnapshotPhase,
)
from actionwitness_core.ports.models import Observation, TargetDescriptor
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.reports.models import ExternalCaptureReference, ExternalTargetReference
from actionwitness_core.security.redaction import RedactionPolicy

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.artifacts import ArtifactStore
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.application.limits import WorkspaceCeilings
from actionwitness_service.application.run_service import IMPLEMENTATION_VERSION
from actionwitness_service.application.verification_service import VerificationService
from actionwitness_service.config import ShopifySettings
from actionwitness_service.persistence.database import Database, UnitOfWork
from actionwitness_service.persistence.locks import WorkspaceLocks
from actionwitness_service.persistence.repositories import (
    EventRepository,
    SnapshotIntegrityError,
    SnapshotRepository,
    new_id,
)

__all__ = [
    "CREDENTIAL_LIFETIME",
    "FORBIDDEN_OPERATIONS",
    "TERMINAL_PAIRING_STATUSES",
    "CapturedPhase",
    "MintedPairing",
    "PairingStatus",
    "PairingView",
    "ShopifyObservationAdapter",
    "ShopifyPairingService",
    "resolve_shopify_adapter",
]


class PairingStatus(StrEnum):
    """§16.5's ten states, verbatim.

    `expired` is a terminal state of its own and not a flavour of anything, for
    the reason §16.5 states outright: "expiry never converts an incomplete
    Shopify trial into a pass". A pairing that ran out of time is a trial that
    did not finish, and a reader has to be able to see that rather than infer it
    from an absent verdict.
    """

    CREATED = "created"
    PAIRED = "paired"
    ARMED = "armed"
    VERIFYING = "verifying"
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed_with_warnings"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ERROR = "error"


#: §16.5's terminal set. The same list is written literally into migration 9's
#: partial unique index — SQLite cannot import an enum — and a test compares the
#: two so the schema and the service cannot drift apart on which pairings still
#: hold the workspace's one live slot.
TERMINAL_PAIRING_STATUSES: Final[frozenset[PairingStatus]] = frozenset(
    {
        PairingStatus.PASSED,
        PairingStatus.PASSED_WITH_WARNINGS,
        PairingStatus.FAILED,
        PairingStatus.EXPIRED,
        PairingStatus.CANCELLED,
        PairingStatus.ERROR,
    }
)

#: §16.5's permitted moves, transcribed. Held here rather than in the core
#: because §16.5 is target-specific Tier 3 vocabulary and `journeys/enums.py`
#: says so explicitly: "this module is target-neutral, so it holds no Shopify
#: pairing states". The core's `validate_run_transition` still owns every move
#: the *run* makes; this table owns only the pairing's.
_PAIRING_TRANSITIONS: Final[Mapping[PairingStatus, frozenset[PairingStatus]]] = {
    PairingStatus.CREATED: frozenset(
        {
            PairingStatus.PAIRED,
            PairingStatus.EXPIRED,
            PairingStatus.CANCELLED,
            PairingStatus.ERROR,
        }
    ),
    PairingStatus.PAIRED: frozenset(
        {
            PairingStatus.ARMED,
            PairingStatus.EXPIRED,
            PairingStatus.CANCELLED,
            PairingStatus.ERROR,
        }
    ),
    PairingStatus.ARMED: frozenset(
        {
            PairingStatus.VERIFYING,
            PairingStatus.EXPIRED,
            PairingStatus.CANCELLED,
            PairingStatus.ERROR,
        }
    ),
    PairingStatus.VERIFYING: frozenset(
        {
            PairingStatus.PASSED,
            PairingStatus.PASSED_WITH_WARNINGS,
            PairingStatus.FAILED,
            PairingStatus.ERROR,
        }
    ),
    PairingStatus.PASSED: frozenset(),
    PairingStatus.PASSED_WITH_WARNINGS: frozenset(),
    PairingStatus.FAILED: frozenset(),
    PairingStatus.EXPIRED: frozenset(),
    PairingStatus.CANCELLED: frozenset(),
    PairingStatus.ERROR: frozenset(),
}

#: FR-111's "15-minute expiry", as a number rather than a habit.
CREDENTIAL_LIFETIME: Final = timedelta(minutes=15)

#: 32 bytes from `secrets`, URL-safe because the credential travels in a URL
#: fragment (FR-111) and a percent-encoded one would be a second representation
#: to keep in agreement.
_CREDENTIAL_BYTES: Final = 32

#: FR-114: "`proceed_to_checkout`, order creation, customer login, and payment
#: are forbidden for this contract." Matched against the contract document at
#: pairing time and against the path the bridge says it read, because both are
#: places the forbidden thing could enter — one by an author, one by a browser
#: that navigated somewhere else mid-trial.
FORBIDDEN_OPERATIONS: Final[tuple[str, ...]] = (
    "checkout",
    "order",
    "payment",
    "customer_login",
    "login",
)

#: The one resource FR-112 allows the bridge to read: the locale-aware
#: `cart.js`. Compared as a suffix of the *path* — a locale prefix such as
#: `/en-gb/cart.js` is legitimate and is exactly what `window.Shopify.routes.root`
#: produces — and never against a query or fragment, which FR-117 excludes from
#: the recorded capture URL in the first place.
_CART_RESOURCE: Final = "/cart.js"

#: §9.3's Tier 3 provenance. Checked rather than accepted: a bridge that could
#: label its own submission could label a tool result `platform_session_api`, and
#: the independence claim would rest on a string the browser chose.
PLATFORM_SESSION_API: Final = "platform_session_api"


@runtime_checkable
class ShopifyObservationAdapter(Protocol):
    """What this service needs from `integrations.shopify` and nothing more.

    Two methods, both of them refusals: one decides whether an origin is the
    configured store, and one decides whether a submitted payload is a `cart.js`
    read rather than a tool result wearing an observation's clothes. Expressed as
    a `Protocol` so this module names no commerce type and imports the
    integration only inside a function — §21.1 requires the harness to run with
    the integration absent from the environment entirely.
    """

    descriptor: TargetDescriptor

    def validate_origin(self, origin: str) -> None: ...

    def normalize(self, payload: dict, provenance: str) -> Observation: ...


def resolve_shopify_adapter(settings: ShopifySettings) -> ShopifyObservationAdapter:
    """The configured store's adapter, or FR-119's bounded unavailability.

    Imported here rather than at module scope for the reason `routes/audits.py`
    imports its own the same way: §21.1 requires the harness to run with an
    integration absent, and a top-level import would make this whole module —
    and therefore the router that imports it — unimportable without it.

    FR-119: "If the Shopify store, theme bridge, or Cart API is unavailable or
    incompatible, the Shopify module shall report a bounded external-target error
    and remain retryable. It shall not change Buggy Store runs, benchmark
    artifacts, or the deterministic Buggy Store demonstration." A build whose
    adapter has not landed is exactly that case, and it is a named refusal rather
    than an `ImportError` reaching the generic 500 handler.
    """
    try:
        from integrations.shopify import ShopifyAdapter
    except ImportError as absent:  # pragma: no cover - exercised by the stub path
        raise _target_unavailable() from absent

    return ShopifyAdapter(
        store_origin=settings.store_origin,
        test_variant_id=settings.test_variant_id,
        expected_currency=settings.expected_currency,
    )


def _target_unavailable() -> ApiError:
    return ApiError(
        ApiErrorCode.TARGET_UNAVAILABLE,
        "The Shopify development-store target is not available in this deployment, "
        "so no pairing can be created. Nothing else is affected.",
        details=[{"path": "target", "message": "no shopify adapter is installed"}],
    )


@dataclass(frozen=True)
class MintedPairing:
    """A newly created pairing and the one-time credential it just minted.

    The raw credential lives on this object and nowhere else, and the object
    lives for the length of one request. It is deliberately not part of
    `PairingView`: the status endpoint returns a view, and a view that could
    carry a credential is one refactor away from returning one.
    """

    pairing_id: str
    launch_url: str
    expires_at: str
    #: FR-111's one-time credential, in the clear, for the URL fragment only.
    credential: str


@dataclass(frozen=True)
class CapturedPhase:
    """One accepted observation, and whether this call is the one that took it.

    `replayed` is what makes §15.7's idempotency legible to a caller: a repeat
    carrying the same content hash gets the existing result and a `true` here,
    rather than a second capture or a refusal.
    """

    pairing: PairingView
    content_hash: str
    replayed: bool


class PairingView:
    """One pairing as the API reports it. Carries no credential, by construction."""

    __slots__ = (
        "after_capture_path",
        "before_capture_path",
        "bridge_version",
        "completed_at",
        "contract_content_hash",
        "contract_id",
        "created_at",
        "expires_at",
        "pairing_id",
        "redeemed_at",
        "run_id",
        "status",
        "store_origin",
        "theme_build_id",
        "workspace_id",
    )

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.after_capture_path = row["after_capture_path"]
        self.before_capture_path = row["before_capture_path"]
        self.pairing_id = str(row["id"])
        self.workspace_id = str(row["workspace_id"])
        self.contract_id = str(row["contract_id"])
        self.contract_content_hash = str(row["contract_content_hash"])
        self.run_id = row["run_id"]
        self.store_origin = str(row["store_origin"])
        self.status = PairingStatus(str(row["status"]))
        self.expires_at = str(row["expires_at"])
        self.redeemed_at = row["redeemed_at"]
        self.completed_at = row["completed_at"]
        self.created_at = str(row["created_at"])
        self.bridge_version = row["bridge_version"]
        self.theme_build_id = row["theme_build_id"]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_PAIRING_STATUSES

    def as_document(self) -> dict[str, Any]:
        """§15.7's status payload.

        Every field here is an identifier, a state, or a timestamp. The two hash
        columns are absent and so is anything derived from them: a digest is not
        a credential, but publishing one turns a stolen response into an offline
        target, and there is no reason a client needs it.
        """
        return {
            "pairing_id": self.pairing_id,
            "status": str(self.status.value),
            "store_origin": self.store_origin,
            "contract_id": self.contract_id,
            "contract_content_hash": self.contract_content_hash,
            "run_id": self.run_id,
            "bridge_version": self.bridge_version,
            "theme_build_id": self.theme_build_id,
            "expires_at": self.expires_at,
            "redeemed_at": self.redeemed_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
        }


class ShopifyPairingService:
    """§16.5's lifecycle: create, redeem, capture, verify, read.

    Constructed per request from the long-lived `Database`, `WorkspaceLocks`, and
    `ArtifactStore`, plus the module's settings and the target adapter. It takes
    the adapter as a constructor argument rather than resolving one itself so a
    test can supply a conforming double and so the composition root stays the one
    place that decides which integration is installed.
    """

    def __init__(
        self,
        database: Database,
        locks: WorkspaceLocks,
        artifacts: ArtifactStore,
        *,
        settings: ShopifySettings,
        adapter: ShopifyObservationAdapter,
    ) -> None:
        self._database = database
        self._locks = locks
        self._artifacts = artifacts
        self._settings = settings
        self._adapter = adapter

    # -- create ---------------------------------------------------------------

    async def create(self, workspace_id: str, contract_id: str) -> MintedPairing:
        """FR-111's one-time credential, bound to four things and expiring.

        One transaction, and every check is inside it. The workspace ceiling, the
        one-live-pairing rule, and the insert have to see the same database state
        or two tabs both pass a count of four and the workspace ends with six
        pairings (FR-008, §17.1).
        """
        credential = _mint()
        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            contract = await WorkspaceScope(work, workspace_id).contract(contract_id)
            document = json.loads(contract["document_json"])
            _require_safe_scope(document)

            await WorkspaceCeilings(work, workspace_id).guard_new_pairing()
            await self._require_no_live_pairing(work, workspace_id)

            pairing_id = new_id("pair")
            created_at = work.instant()
            expires_at = created_at + CREDENTIAL_LIFETIME
            await work.execute(
                """
                INSERT INTO shopify_pairings (
                    id, workspace_id, contract_id, contract_content_hash, store_origin,
                    pairing_token_hash, status, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pairing_id,
                    workspace_id,
                    contract_id,
                    str(contract["content_hash"]),
                    self._settings.store_origin,
                    _fingerprint(
                        credential,
                        workspace_id=workspace_id,
                        contract_id=contract_id,
                        store_origin=self._settings.store_origin,
                    ),
                    str(PairingStatus.CREATED.value),
                    _iso(expires_at),
                    _iso(created_at),
                ),
            )
            # §17.1's `workspaces.active_shopify_pairing_id`, set in the same
            # transaction as the row it points at. A pointer written afterwards
            # is a workspace that briefly believes it has no pairing while a
            # pairing believes it is live.
            await work.execute(
                "UPDATE workspaces SET active_shopify_pairing_id = ? WHERE id = ?",
                (pairing_id, workspace_id),
            )

        return MintedPairing(
            pairing_id=pairing_id,
            # FR-111: "delivered in a URL fragment". A fragment is never sent to
            # a server, never written to an access log, and never survives a
            # redirect — which is the whole reason the specification names it
            # rather than a query parameter.
            launch_url=f"{self._settings.store_origin}/#actionwitness={pairing_id}.{credential}",
            expires_at=_iso(expires_at),
            credential=credential,
        )

    # -- redeem ---------------------------------------------------------------

    async def redeem(
        self,
        pairing_id: str,
        credential: str,
        origin: str,
        *,
        bridge_version: str,
        theme_build_id: str | None,
    ) -> tuple[PairingView, str]:
        """FR-111: redeemed once, then represented by a session-scoped credential.

        Returns the view and the raw bridge credential, which the route puts in a
        `no-store` response and nothing else ever sees again — only its digest is
        written.

        "Redeemed once" is enforced by the `WHERE status = 'created'` on the
        update rather than by the read above it. Two bridges racing on the same
        fragment would both read `created`; only one can write it away, and the
        loser is told the credential was already spent rather than being handed a
        second session.
        """
        session = _mint()
        # The workspace is read before the lock is taken, because the bridge
        # never names one (§15.6) and the lock is keyed by it. This first read is
        # authorization-free on purpose: it learns nothing but which key to
        # queue on, and every check runs again inside the transaction below.
        async with self._database.reading() as work:
            workspace_id = (await self._read(work, pairing_id)).workspace_id

        async with self._locks.hold(workspace_id), self._database.transaction() as work:
            pairing = await self._authorized(
                work, pairing_id, credential=credential, origin=origin, kind=_Credential.PAIRING
            )
            _require_transition(pairing, PairingStatus.PAIRED)

            redeemed_at = work.now()
            updated = await work.execute(
                """
                UPDATE shopify_pairings
                   SET status = ?, bridge_session_token_hash = ?, bridge_version = ?,
                       theme_build_id = ?, redeemed_at = ?
                 WHERE id = ? AND status = ?
                """,
                (
                    str(PairingStatus.PAIRED.value),
                    _fingerprint(
                        session,
                        workspace_id=pairing.workspace_id,
                        contract_id=pairing.contract_id,
                        store_origin=pairing.store_origin,
                    ),
                    bridge_version,
                    theme_build_id,
                    redeemed_at,
                    pairing_id,
                    str(PairingStatus.CREATED.value),
                ),
            )
            if updated.rowcount == 0:
                raise _credential_refused()
            refreshed = await self._read(work, pairing_id)

        return refreshed, session

    # -- the `before` observation --------------------------------------------

    async def capture_before(
        self,
        pairing_id: str,
        credential: str,
        origin: str,
        *,
        payload: Mapping[str, Any],
        capture_path: str,
    ) -> CapturedPhase:
        """§16.5: "Only the `before` observation may create the associated run."

        Three phases, and the boundaries are ADR-0003's. Reading and authorizing
        is a short transaction; normalizing the payload is pure and holds
        nothing; the run, the snapshot, the events and the pairing's move to
        `armed` are one transaction that commits together or not at all.

        The precondition is FR-116's: the initial cart must be empty. A trial
        that began with a full cart is not a trial of adding one variant, and
        arming it anyway would produce a report whose baseline nobody chose.
        """
        async with self._database.reading() as work:
            pairing = await self._authorized(
                work, pairing_id, credential=credential, origin=origin, kind=_Credential.BRIDGE
            )
            document = await self._contract_document(work, pairing)

        _require_cart_resource(capture_path)
        observation = self._observe(payload, document)

        async with (
            self._locks.hold(pairing.workspace_id),
            self._database.transaction() as work,
        ):
            # Re-authorized inside the writing transaction. Nothing was held
            # across the normalization above, so the pairing may have expired, or
            # been cancelled by a reset, in the meantime — and an expired pairing
            # must capture no observation at all (FR-111).
            pairing = await self._authorized(
                work, pairing_id, credential=credential, origin=origin, kind=_Credential.BRIDGE
            )
            if pairing.run_id is not None:
                return await self._replayed(work, pairing, SnapshotPhase.BEFORE, observation)

            _require_transition(pairing, PairingStatus.ARMED)
            _require_empty_cart(observation)
            run_id = await self._create_run(work, pairing, document, observation)
            await work.execute(
                "UPDATE shopify_pairings "
                "SET status = ?, run_id = ?, before_capture_path = ? "
                "WHERE id = ? AND status = ?",
                (
                    str(PairingStatus.ARMED.value),
                    run_id,
                    capture_path,
                    pairing_id,
                    str(PairingStatus.PAIRED.value),
                ),
            )
            captured = await self._read(work, pairing_id)

        return CapturedPhase(
            pairing=captured, content_hash=observation.content_hash(), replayed=False
        )

    # -- the final observation and the verdict --------------------------------

    async def verify(
        self,
        pairing_id: str,
        credential: str,
        origin: str,
        *,
        payload: Mapping[str, Any],
        capture_path: str,
    ) -> CapturedPhase:
        """§16.5: the final observation moves the run through `running` to a verdict.

        §16 names the exception this implements: "For an `external_webmcp`
        Shopify target, the accepted final external observation is evidence that
        the shopper-session state changed outside the harness; it appends
        `external_observation_received` and transitions the run from `armed` to
        `running` immediately before verification. This exception does not
        manufacture tool events, and the observed-trajectory layer remains
        `not_evaluated`."

        Both halves are honoured literally. The transaction below appends exactly
        one event and writes no `tool_invocation_*` row, and the contract carries
        no `expected_tools` (FR-114), so the core's trajectory check returns
        `not_evaluated` on its own rather than being told to.
        """
        async with self._database.reading() as work:
            pairing = await self._authorized(
                work, pairing_id, credential=credential, origin=origin, kind=_Credential.BRIDGE
            )
            document = await self._contract_document(work, pairing)

        _require_cart_resource(capture_path)
        observation = self._observe(payload, document)

        async with (
            self._locks.hold(pairing.workspace_id),
            self._database.transaction() as work,
        ):
            pairing = await self._authorized(
                work, pairing_id, credential=credential, origin=origin, kind=_Credential.BRIDGE
            )
            if pairing.is_terminal or pairing.status is PairingStatus.VERIFYING:
                return await self._replayed(work, pairing, SnapshotPhase.AFTER, observation)

            _require_transition(pairing, PairingStatus.VERIFYING)
            run_id = pairing.run_id
            if run_id is None:  # pragma: no cover - `armed` implies a run exists
                raise ApiError(
                    ApiErrorCode.PRECONDITION_FAILED,
                    "This pairing has captured no initial cart, so there is nothing to verify.",
                )
            initial = await SnapshotRepository(work).get(str(run_id), SnapshotPhase.BEFORE)
            if initial is None:  # pragma: no cover - `armed` implies a baseline exists
                raise ApiError(
                    ApiErrorCode.PRECONDITION_FAILED,
                    "This pairing has no initial cart evidence, so it cannot be verified.",
                )
            external_target = self._external_target_reference(
                pairing, initial, observation, after_capture_path=capture_path
            )
            await self._accept_external_observation(work, str(run_id), observation)
            await work.execute(
                "UPDATE shopify_pairings SET status = ?, after_capture_path = ? "
                "WHERE id = ? AND status = ?",
                (
                    str(PairingStatus.VERIFYING.value),
                    capture_path,
                    pairing_id,
                    str(PairingStatus.ARMED.value),
                ),
            )

        await VerificationService(
            self._database,
            # The registry is passed as `None` on purpose and the external path
            # never asks it for anything: an `external_webmcp` target is driven
            # by its own tools, so there is no adapter to capture through and no
            # effect map to read (§9.1). See `VerificationService.verify`.
            None,
            self._locks,
            self._artifacts,
        ).verify(
            pairing.workspace_id,
            str(run_id),
            external_observation=observation,
            external_target=external_target,
            on_seal=self._finalizer(pairing_id),
        )

        async with self._database.reading() as work:
            final = await self._read(work, pairing_id)
        return CapturedPhase(pairing=final, content_hash=observation.content_hash(), replayed=False)

    def _external_target_reference(
        self,
        pairing: PairingView,
        initial: Observation,
        final: Observation,
        *,
        after_capture_path: str,
    ) -> ExternalTargetReference:
        """Build FR-117's source block only from recorded, validated evidence."""
        if pairing.before_capture_path is None or pairing.bridge_version is None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This pairing predates complete Shopify capture provenance. Reset the "
                "workspace and create a new pairing before verifying.",
                details=[
                    {
                        "path": "pairing",
                        "message": "capture path or bridge version is unavailable",
                    }
                ],
            )
        return ExternalTargetReference(
            target_type=self._adapter.descriptor.target_type,
            origin=pairing.store_origin,
            pairing_id=pairing.pairing_id,
            bridge_version=str(pairing.bridge_version),
            theme_build_id=(
                str(pairing.theme_build_id) if pairing.theme_build_id is not None else None
            ),
            observation_provider=final.provider_id,
            provenance=final.provenance,
            before=ExternalCaptureReference(
                path=str(pairing.before_capture_path),
                captured_at=initial.captured_at,
                content_hash=initial.content_hash(),
            ),
            after=ExternalCaptureReference(
                path=after_capture_path,
                captured_at=final.captured_at,
                content_hash=final.content_hash(),
            ),
            safe_scope_result=LayerResult.PASSED,
        )

    def _finalizer(self, pairing_id: str) -> Callable[[UnitOfWork, LayerResult], Awaitable[None]]:
        """§16.5's atomic finalization, as a callback the seal runs inside itself.

        "Pairing and run terminal results must agree; an atomic repository
        transaction commits both terminal states and the report reference, or
        neither." A second transaction after `verify` returned would satisfy the
        sentence's words and not its purpose: a crash between the two leaves a
        `failed` run under a `verifying` pairing, which is precisely the
        disagreement the clause forbids. So the pairing's terminal write happens
        in the transaction that writes the run's, beside the artifact row that is
        the report reference.
        """

        async def _seal(work: UnitOfWork, result: LayerResult) -> None:
            updated = await work.execute(
                "UPDATE shopify_pairings SET status = ?, completed_at = ? "
                "WHERE id = ? AND status = ?",
                (
                    _PAIRING_RESULT[result].value,
                    work.now(),
                    pairing_id,
                    str(PairingStatus.VERIFYING.value),
                ),
            )
            if updated.rowcount == 0:  # pragma: no cover - the seal holds the lock
                raise ApiError(
                    ApiErrorCode.RUN_IN_PROGRESS,
                    "This pairing left `verifying` while its verdict was being computed, "
                    "so the verdict was discarded rather than applied.",
                )

        return _seal

    # -- reads and cancellation ----------------------------------------------

    async def read(self, workspace_id: str, pairing_id: str) -> PairingView:
        """§15.7's status endpoint, scoped to the caller's workspace (FR-006)."""
        async with self._database.reading() as work:
            pairing = await self._read(work, pairing_id)
            if pairing.workspace_id != workspace_id:
                # 404 rather than 403, exactly as `authorization.not_found`
                # argues: a 403 confirms the identifier names something real.
                raise ApiError(
                    ApiErrorCode.RESOURCE_NOT_FOUND, "No such pairing in this workspace."
                )
            return self._expire_if_elapsed(work, pairing)

    async def status_document(self, workspace_id: str, pairing_id: str) -> dict[str, Any]:
        """Build the operator-facing status from verified persisted evidence."""
        async with self._database.reading() as work:
            pairing = await self._read(work, pairing_id)
            if pairing.workspace_id != workspace_id:
                raise ApiError(
                    ApiErrorCode.RESOURCE_NOT_FOUND, "No such pairing in this workspace."
                )
            pairing = self._expire_if_elapsed(work, pairing)
            document = pairing.as_document()
            observations: list[dict[str, Any]] = []
            document["overall_result"] = None
            document["observations"] = observations
            if pairing.run_id is None:
                return document

            run_id = str(pairing.run_id)
            run = await work.fetch_one(
                "SELECT overall_result FROM runs WHERE id = ? AND workspace_id = ?",
                (run_id, workspace_id),
            )
            if run is None:
                raise ApiError(
                    ApiErrorCode.HARNESS_ERROR,
                    "The Shopify pairing refers to a run that is unavailable.",
                )
            document["overall_result"] = run["overall_result"]

            snapshots = SnapshotRepository(work)
            try:
                for phase, capture_path in (
                    (SnapshotPhase.BEFORE, pairing.before_capture_path),
                    (SnapshotPhase.AFTER, pairing.after_capture_path),
                ):
                    observation = await snapshots.get(run_id, phase)
                    if observation is not None:
                        observations.append(
                            _shopify_observation_document(phase, observation, capture_path)
                        )
            except SnapshotIntegrityError as corrupt:
                raise ApiError(
                    ApiErrorCode.HARNESS_ERROR,
                    "Stored Shopify cart evidence failed its integrity check and was not served.",
                ) from corrupt
            return document

    # -- internals ------------------------------------------------------------

    async def _require_no_live_pairing(self, work: UnitOfWork, workspace_id: str) -> None:
        """§17.1: "At most one nonterminal pairing may exist per interactive workspace."

        Checked here as well as by migration 9's partial unique index, and the
        duplication is deliberate: the index is the guarantee and this is the
        message. An `IntegrityError` reaching the boundary would be a 500 with no
        text, and an operator would learn nothing about the pairing they already
        have open.
        """
        live = await self._live(work, workspace_id)
        if live is None:
            return
        raise ApiError(
            ApiErrorCode.RUN_IN_PROGRESS,
            "This workspace already has a Shopify pairing in progress. Finish it or "
            "reset the workspace before starting another.",
            details=[{"path": "pairing", "message": f"pairing {live.pairing_id} is live"}],
        )

    async def _live(self, work: UnitOfWork, workspace_id: str) -> PairingView | None:
        placeholders = ", ".join("?" for _ in TERMINAL_PAIRING_STATUSES)
        row = await work.fetch_one(
            f"""
            SELECT * FROM shopify_pairings
             WHERE workspace_id = ? AND status NOT IN ({placeholders})
             ORDER BY created_at DESC LIMIT 1
            """,
            (workspace_id, *sorted(status.value for status in TERMINAL_PAIRING_STATUSES)),
        )
        return None if row is None else PairingView(row)

    async def _read(self, work: UnitOfWork, pairing_id: str) -> PairingView:
        row = await work.fetch_one("SELECT * FROM shopify_pairings WHERE id = ?", (pairing_id,))
        if row is None:
            raise ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, "No such pairing.")
        return PairingView(row)

    async def _authorized(
        self,
        work: UnitOfWork,
        pairing_id: str,
        *,
        credential: str,
        origin: str,
        kind: _Credential,
    ) -> PairingView:
        """Every gate FR-111 names, in the order that fails closed soonest.

        **No `workspace_id` is accepted from the caller and none is compared
        against one.** §15.6 and FR-111 both put the bridge outside the cookie's
        reach; the workspace is *derived* from the row the credential unlocks, so
        a bridge cannot name a workspace at all — which is stronger than checking
        the name it gave. The digest covers the workspace, so a credential minted
        elsewhere cannot unlock this row even by presenting its id.

        Expiry is checked before the digest is compared and before any payload is
        looked at, because FR-111 requires an expired pairing to "fail closed
        *without capturing an observation*".
        """
        pairing = await self._read(work, pairing_id)
        self._require_configured_origin(origin)
        self._adapter.validate_origin(origin)

        if pairing.store_origin != self._settings.store_origin:
            # The row was created against a different configured origin than the
            # one this deployment now serves. Refused rather than re-pointed: a
            # pairing is bound to the store it was minted for (§17.1 makes
            # `store_origin` immutable), and honouring it here would let a
            # configuration change move a live trial to another storefront.
            raise _credential_refused()

        if _expired(pairing, work.instant()):
            await self._mark_expired(work, pairing)
            raise ApiError(
                ApiErrorCode.CONFIRMATION_EXPIRED,
                "This Shopify pairing has expired. Nothing was captured; create a new "
                "pairing and reload the storefront.",
                details=[{"path": "pairing", "message": "the 15-minute lifetime elapsed"}],
            )
        if pairing.is_terminal and kind is _Credential.PAIRING:
            raise _credential_refused()

        stored = await self._digest(work, pairing_id, kind)
        if stored is None:
            raise _credential_refused()
        presented = _fingerprint(
            credential,
            workspace_id=pairing.workspace_id,
            contract_id=pairing.contract_id,
            store_origin=pairing.store_origin,
        )
        # Constant-time, because a credential comparison that short-circuits on
        # the first differing byte leaks the digest one byte at a time.
        if not secrets.compare_digest(presented, stored):
            raise _credential_refused()
        return pairing

    async def _digest(self, work: UnitOfWork, pairing_id: str, kind: _Credential) -> str | None:
        """The stored hash for one credential kind, read as late as possible.

        Kept off `PairingView` deliberately. The view is what the status endpoint
        serializes, and a digest that never reaches the view cannot be
        accidentally published by a future field added to `as_document`.
        """
        column = (
            "pairing_token_hash" if kind is _Credential.PAIRING else "bridge_session_token_hash"
        )
        row = await work.fetch_one(
            f"SELECT {column} AS digest FROM shopify_pairings WHERE id = ?", (pairing_id,)
        )
        if row is None or row["digest"] is None:
            return None
        return str(row["digest"])

    def _require_configured_origin(self, origin: str) -> None:
        """FR-110's exact origin, compared for equality and nothing looser."""
        if origin != self._settings.store_origin:
            raise ApiError(
                ApiErrorCode.EXTERNAL_TARGET_FORBIDDEN_OPERATION,
                "That request did not come from the configured development store.",
                details=[{"path": "origin", "message": "origin is not the configured store"}],
            )

    async def _mark_expired(self, work: UnitOfWork, pairing: PairingView) -> None:
        """§16.5's `expired`, written before the refusal is raised.

        Only from a nonterminal state: "expiry never converts an incomplete
        Shopify trial into a pass", and it must not rewrite one that already
        reached a verdict either. The write is conditional on the current status
        so a pairing that terminated in the meantime keeps what it earned.
        """
        if pairing.is_terminal:
            return
        await work.execute(
            "UPDATE shopify_pairings SET status = ?, completed_at = ? WHERE id = ? AND status = ?",
            (
                str(PairingStatus.EXPIRED.value),
                work.now(),
                pairing.pairing_id,
                str(pairing.status.value),
            ),
        )

    async def _contract_document(self, work: UnitOfWork, pairing: PairingView) -> Mapping[str, Any]:
        """The contract this pairing was minted against, by its own identifier.

        Read by id and not by the workspace's current selection: FR-025 fixes the
        contract at pairing time, and a workspace that has since selected another
        must not change what a trial in flight is judged against.
        """
        row = await work.fetch_one(
            "SELECT document_json FROM contracts WHERE id = ?", (pairing.contract_id,)
        )
        if row is None:  # pragma: no cover - contracts are insert-only
            raise ApiError(ApiErrorCode.HARNESS_ERROR, "The paired contract is missing.")
        return json.loads(row["document_json"])

    def _observe(self, payload: Mapping[str, Any], document: Mapping[str, Any]) -> Observation:
        """Normalize a submitted `cart.js` read, then redact it (§20.3).

        Pure, and outside every transaction: it is the one step that can take a
        measurable amount of CPU on a payload a storefront controls, and
        ADR-0003 keeps that off the write lock.

        Redacted *before* the content hash is taken, because §20.3 requires
        redaction "before persistence, hashing, or export" — and because the hash
        is the idempotency key, so hashing the unredacted form would make a
        repeat submission compare against a document nobody stored.
        """
        from actionwitness_core.evidence.effects import redacted_observation

        try:
            observation = self._adapter.normalize(dict(payload), PLATFORM_SESSION_API)
        except ValueError as invalid:
            # A `cart.js` read that will not normalize is a *broken channel*, not
            # an empty cart and not a verdict. §12.17 draws the same line for the
            # audit path, and the message names no field value: the payload came
            # from a storefront, and echoing it back would put untrusted text in
            # a response.
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                "That submission is not a usable cart.js observation, so nothing was "
                "captured from it.",
                details=[{"path": "cart", "message": "payload is not a cart.js document"}],
            ) from invalid

        paths = ((document.get("redaction") or {}).get("paths")) or []
        return redacted_observation(
            observation, RedactionPolicy.from_paths([str(path) for path in paths])
        )

    async def _replayed(
        self,
        work: UnitOfWork,
        pairing: PairingView,
        phase: SnapshotPhase,
        observation: Observation,
    ) -> CapturedPhase:
        """§15.7's idempotency, decided by the snapshot that is already stored.

        "Initial capture and verification are idempotent by `(pairing_id, phase,
        content_hash)`; a repeat with the same hash returns the existing result,
        while a different second payload for the same phase returns `409
        OBSERVATION_ALREADY_CAPTURED`."

        The key is read off `snapshots` rather than kept in a table of its own: a
        pairing has exactly one run (§17.1 makes `run_id` unique), and `snapshots`
        already carries a unique `(run_id, phase)` with the stored content hash
        beside it. So the evidence *is* the idempotency record, which means the
        two cannot disagree — a second table could say "captured" about a
        snapshot that was never written.
        """
        run_id = pairing.run_id
        stored = None if run_id is None else await SnapshotRepository(work).get(str(run_id), phase)
        if stored is None:
            # The phase is claimed by the pairing's state but has no snapshot.
            # That is a harness inconsistency rather than a client error, and it
            # must not be answered by capturing over the top of it.
            raise ApiError(
                ApiErrorCode.OBSERVATION_ALREADY_CAPTURED,
                f"The {phase.value} observation for this pairing is already recorded.",
            )
        if stored.content_hash() != observation.content_hash():
            raise ApiError(
                ApiErrorCode.OBSERVATION_ALREADY_CAPTURED,
                f"A different {phase.value} cart was already captured for this pairing. "
                "A trial observes one moment; a second, differing read describes another.",
                details=[{"path": "cart", "message": "content hash differs from the capture"}],
            )
        return CapturedPhase(pairing=pairing, content_hash=stored.content_hash(), replayed=True)

    async def _create_run(
        self,
        work: UnitOfWork,
        pairing: PairingView,
        document: Mapping[str, Any],
        observation: Observation,
    ) -> str:
        """§16.5: the `before` observation, and only it, creates the run.

        Written here rather than through `RunService.arm` because arming captures
        the initial observation *from* the target over HTTP, and this one arrived
        in the request body from a session the server cannot reach. Everything
        else follows arming's shape: the run copies its controlled inputs in and
        never updates them (FR-012), `run_armed` is the timeline's first event,
        and the snapshot follows it.
        """
        from actionwitness_core.security.canonical import content_hash

        run_id = new_id("run")
        await WorkspaceCeilings(work, pairing.workspace_id).guard_new_run()
        await work.execute(
            """
            INSERT INTO runs (
                id, workspace_id, contract_id, contract_content_hash, target_id,
                target_adapter_id, scenario_mode, fault_active, intent_content_hash,
                implementation_version, status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                pairing.workspace_id,
                pairing.contract_id,
                pairing.contract_content_hash,
                str(document.get("target_id", "")),
                _SHOPIFY_ADAPTER_ID,
                # §9.1: an external target has no pre/post fixture to switch
                # between, so the mode names the only thing it can observe.
                _EXTERNAL_SCENARIO_MODE,
                # FR-162 forbids injected faults against an external target, and
                # `fault_active` is a claim about the target rather than about the
                # request — recording `true` here would name a defect nothing
                # produced.
                0,
                content_hash({"intent": str(document.get("intent", ""))}),
                IMPLEMENTATION_VERSION,
                str(RunState.ARMED.value),
                work.now(),
            ),
        )
        await work.execute(
            "UPDATE workspaces SET active_run_id = ? WHERE id = ?",
            (run_id, pairing.workspace_id),
        )

        events = EventRepository(work)
        await events.append(
            run_id,
            {
                "event_type": str(OutcomeEventType.RUN_ARMED.value),
                "actor": str(EventActor.HUMAN.value),
                "redacted_payload": {
                    "contract_id": pairing.contract_id,
                    "contract_content_hash": pairing.contract_content_hash,
                    "store_origin": pairing.store_origin,
                    "pairing_id": pairing.pairing_id,
                },
            },
        )
        await SnapshotRepository(work).add(run_id, SnapshotPhase.BEFORE, observation)
        await events.append(
            run_id,
            {
                "event_type": str(OutcomeEventType.SNAPSHOT_CAPTURED.value),
                # `external`, which §17.1 reserves for exactly this: "used only
                # for accepted external observations". `harness` would say the
                # server took this reading, and it did not — it received one.
                "actor": str(EventActor.EXTERNAL.value),
                "state_hash_after": observation.content_hash(),
                "redacted_payload": {
                    "phase": str(SnapshotPhase.BEFORE.value),
                    "provider": observation.provider_id,
                    "provenance": observation.provenance,
                },
            },
        )
        return run_id

    async def _accept_external_observation(
        self, work: UnitOfWork, run_id: str, observation: Observation
    ) -> None:
        """§16's `external_observation_received`, and the `armed` → `running` move.

        One event and one status change, and deliberately nothing else. §16 is
        explicit that "this exception does not manufacture tool events": writing
        a `tool_invocation_completed` here so that the verification gate would
        find a completed action is exactly the manufactured evidence the clause
        forbids, and the gate was taught about this event instead.
        """
        await EventRepository(work).append(
            run_id,
            {
                "event_type": str(OutcomeEventType.EXTERNAL_OBSERVATION_RECEIVED.value),
                "actor": str(EventActor.EXTERNAL.value),
                "state_hash_after": observation.content_hash(),
                "redacted_payload": {
                    "provider": observation.provider_id,
                    "provenance": observation.provenance,
                },
            },
        )
        updated = await work.execute(
            "UPDATE runs SET status = ? WHERE id = ? AND status = ?",
            (str(RunState.RUNNING.value), run_id, str(RunState.ARMED.value)),
        )
        if updated.rowcount == 0:
            raise ApiError(
                ApiErrorCode.RUN_IN_PROGRESS,
                "This run is no longer armed, so the submitted cart has no run to belong to.",
            )

    def _expire_if_elapsed(self, work: UnitOfWork, pairing: PairingView) -> PairingView:
        """A read that reports `expired` without needing a writer to have run.

        The status endpoint runs on a read-only unit of work (ADR-0003 keeps
        reads off the write lock), so it cannot persist the transition. Reporting
        the elapsed lifetime anyway is the honest answer: the row says `paired`
        because nobody has written since, and a UI that showed `paired` for a
        credential no request would accept would be describing a pairing that no
        longer exists. The durable write happens on the next bridge request,
        which is the moment it matters.
        """
        if pairing.is_terminal or not _expired(pairing, work.instant()):
            return pairing
        pairing.status = PairingStatus.EXPIRED
        return pairing


class _Credential(StrEnum):
    """Which of the two credentials a request is presenting."""

    PAIRING = "pairing"
    BRIDGE = "bridge"


#: §16.5's terminal pairing state for each layer result the core can produce.
#: Keyed by `LayerResult` so the pairing and the run cannot disagree: both are
#: derived from the one verdict rather than from two readings of the findings.
_PAIRING_RESULT: Final[Mapping[LayerResult, PairingStatus]] = {
    LayerResult.PASSED: PairingStatus.PASSED,
    LayerResult.PASSED_WITH_WARNINGS: PairingStatus.PASSED_WITH_WARNINGS,
    LayerResult.FAILED: PairingStatus.FAILED,
}

#: The adapter id copied into a Shopify run. A string rather than an import,
#: because `runs.target_adapter_id` is recorded evidence and must stay readable
#: in a database whose integration has since been uninstalled.
_SHOPIFY_ADAPTER_ID: Final = "integrations.shopify"

#: §9.1: an external target has no pre/post fixture, so this is the only mode it
#: can honestly report observing.
_EXTERNAL_SCENARIO_MODE: Final = "external_current"

#: The provider id written by the Shopify observation adapter. Kept here as
#: recorded protocol data so the generic service never imports the integration.
_SHOPIFY_CART_PROVIDER: Final = "shopify_cart_state"


def _shopify_observation_document(
    phase: SnapshotPhase, observation: Observation, capture_path: object
) -> dict[str, Any]:
    """A bounded, validated summary for the status UI; never the raw cart."""
    cart = observation.payload.get("cart")
    if not isinstance(cart, Mapping):
        raise _invalid_shopify_snapshot("cart")

    item_count = cart.get("item_count")
    currency = cart.get("currency")
    subtotal = cart.get("subtotal")
    total = cart.get("total")
    if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0:
        raise _invalid_shopify_snapshot("cart.item_count")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise _invalid_shopify_snapshot("cart.currency")
    if not isinstance(subtotal, str) or not subtotal:
        raise _invalid_shopify_snapshot("cart.subtotal")
    if not isinstance(total, str) or not total:
        raise _invalid_shopify_snapshot("cart.total")
    if observation.provider_id != _SHOPIFY_CART_PROVIDER:
        raise _invalid_shopify_snapshot("provider")
    if observation.provenance != PLATFORM_SESSION_API:
        raise _invalid_shopify_snapshot("provenance")
    if capture_path is not None and not isinstance(capture_path, str):
        raise _invalid_shopify_snapshot("capture_url_path")

    return {
        "phase": phase.value,
        "captured_at": _iso(observation.captured_at),
        "content_hash": observation.content_hash(),
        "capture_url_path": capture_path,
        "provider": observation.provider_id,
        "provenance": observation.provenance,
        "item_count": item_count,
        "currency": currency,
        "subtotal": subtotal,
        "total": total,
    }


def _invalid_shopify_snapshot(field: str) -> ApiError:
    return ApiError(
        ApiErrorCode.HARNESS_ERROR,
        "Stored Shopify cart evidence does not match the recorded observation schema.",
        details=[{"path": field, "message": "stored observation is invalid"}],
    )


def _mint() -> str:
    """A credential with 256 bits of entropy from the OS CSPRNG."""
    return secrets.token_urlsafe(_CREDENTIAL_BYTES)


def _fingerprint(credential: str, *, workspace_id: str, contract_id: str, store_origin: str) -> str:
    """The stored digest: the secret **and** everything it is bound to.

    FR-111 binds a credential to "workspace, contract, Shopify origin, and a
    15-minute expiry". Three of those are inputs here, and the fourth is the
    `expires_at` column, which is a comparison rather than a binding. Folding the
    three into the digest makes a cross-workspace or cross-origin presentation
    fail on the hash rather than on a `WHERE` clause — the difference matters
    because a `WHERE` clause is one refactor away from being dropped, and a
    changed hash input can only ever fail closed.

    The separator cannot appear in any of the fields, so no two different tuples
    can produce the same input string.
    """
    material = "\x00".join((workspace_id, contract_id, store_origin, credential))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _credential_refused() -> ApiError:
    """One refusal for every way of presenting an unacceptable credential.

    Deliberately identical whether the credential was never valid, belongs to
    another workspace, was already redeemed, or names a pairing that has
    finished. Distinguishing them would let a caller enumerate which pairings
    exist and which have been used, one guess at a time.
    """
    return ApiError(
        ApiErrorCode.AUDIT_NOT_AUTHORIZED,
        "That pairing credential was not accepted. Nothing was captured.",
        details=[{"path": "authorization", "message": "credential is not valid for this pairing"}],
    )


def _expired(pairing: PairingView, now: datetime) -> bool:
    """Whether the 15-minute lifetime has elapsed, by the injected clock."""
    return now >= _parse(pairing.expires_at)


def _parse(stored: str) -> datetime:
    return datetime.fromisoformat(stored.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_transition(pairing: PairingView, target: PairingStatus) -> None:
    """Refuse a pairing move §16.5 does not permit.

    409 with a stable code, matching §16's rule for the run machine: "invalid
    non-reset state transitions shall return HTTP 409".
    """
    if target in _PAIRING_TRANSITIONS[pairing.status]:
        return
    permitted = sorted(str(state.value) for state in _PAIRING_TRANSITIONS[pairing.status])
    raise ApiError(
        ApiErrorCode.RUN_IN_PROGRESS,
        f"A Shopify pairing may not move from {pairing.status.value!r} to "
        f"{target.value!r}; permitted transitions are {permitted or 'none (terminal)'}.",
        details=[
            {
                "path": "pairing.status",
                "message": f"{pairing.status.value} -> {target.value} is not permitted",
            }
        ],
    )


def _require_safe_scope(document: Mapping[str, Any]) -> None:
    """FR-114's forbidden operations, refused before a credential is minted.

    "`proceed_to_checkout`, order creation, customer login, and payment are
    forbidden for this contract." §15.9 gives the code: "any attempt to arm an
    external contract naming a forbidden operation [is] refused with
    `EXTERNAL_TARGET_FORBIDDEN_OPERATION`".

    Checked against the tool and policy names the contract *drives*, not against
    every string in the document. `target.order.created == false` is an assertion
    that no order was created — the safest thing a Shopify contract can say — and
    a scan that failed on the word "order" anywhere would refuse exactly the
    contract FR-114 asks for.
    """
    named: list[str] = [str(entry) for entry in _expected_calls(document)]
    named += [
        str(policy.get("tool", ""))
        for policy in document.get("policies") or []
        if isinstance(policy, Mapping)
    ]

    offending = sorted(
        {
            name
            for name in named
            if name and any(forbidden in name.lower() for forbidden in FORBIDDEN_OPERATIONS)
        }
    )
    if offending:
        raise ApiError(
            ApiErrorCode.EXTERNAL_TARGET_FORBIDDEN_OPERATION,
            "This contract drives an operation the Shopify cart-only scope forbids, so "
            "no pairing was created.",
            details=[
                {"path": "contract", "message": f"forbidden operation {name!r}"}
                for name in offending
            ],
        )


def _expected_calls(document: Mapping[str, Any]) -> tuple[str, ...]:
    expected = document.get("expected_tools")
    if not isinstance(expected, Mapping):
        return ()
    calls = expected.get("calls")
    return tuple(str(call) for call in calls) if isinstance(calls, list) else ()


def _require_cart_resource(capture_path: str) -> None:
    """FR-112: the bridge reads `cart.js` and reports which path it read.

    FR-114 makes "cross-origin checkout navigation" a failed or incomplete trial
    rather than a pass, and this is where the harness finds out: a bridge that
    followed a checkout link reports a checkout path, and the submission is
    refused before the payload is even parsed into an observation. Refusing is
    the conservative direction — the trial captures nothing and the pairing keeps
    its state, so nothing is recorded as having passed.

    A suffix comparison because `window.Shopify.routes.root` is locale-aware
    (FR-112): `/en-gb/cart.js` is the correct path for a localized storefront and
    an equality check against `/cart.js` would refuse every store outside the
    primary locale.
    """
    if not capture_path.startswith("/") or "?" in capture_path or "#" in capture_path:
        raise ApiError(
            ApiErrorCode.EXTERNAL_TARGET_FORBIDDEN_OPERATION,
            "A Shopify capture path must be absolute and contain no query or fragment.",
            details=[
                {
                    "path": "capture_path",
                    "message": "expected an absolute path without query or fragment",
                }
            ],
        )
    path = capture_path
    if any(forbidden in path.lower() for forbidden in FORBIDDEN_OPERATIONS):
        raise ApiError(
            ApiErrorCode.EXTERNAL_TARGET_FORBIDDEN_OPERATION,
            "That observation was read from a checkout, order, or account path. The "
            "Shopify proof is cart-only, so nothing was captured.",
            details=[{"path": "capture_path", "message": "path is outside the cart-only scope"}],
        )
    if not path.endswith(_CART_RESOURCE):
        raise ApiError(
            ApiErrorCode.EXTERNAL_TARGET_FORBIDDEN_OPERATION,
            "A Shopify observation must be read from the storefront's cart.js resource.",
            details=[
                {"path": "capture_path", "message": f"expected a path ending {_CART_RESOURCE}"}
            ],
        )


def _require_empty_cart(observation: Observation) -> None:
    """FR-116: "The initial observation must satisfy the empty-cart precondition."

    Read off the normalized observation rather than the raw payload, so the check
    and the contract's own preconditions are looking at the same document. A
    trial that began with items in the cart is not a trial of adding one variant,
    and arming it would produce a report whose baseline nobody chose.
    """
    cart = observation.payload.get("cart")
    items = cart.get("items") if isinstance(cart, Mapping) else None
    if isinstance(items, Mapping) and not items:
        return
    raise ApiError(
        ApiErrorCode.PRECONDITION_FAILED,
        "The shopper session's cart is not empty, so this trial has no clean baseline. "
        "Start a fresh session or clear the cart, then pair again.",
        details=[{"path": "target.cart.items", "message": "expected an empty cart"}],
    )
