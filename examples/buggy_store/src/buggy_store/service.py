"""Store business logic: catalog reads and absolute, retry-safe cart mutation.

Spec v1.9 §13.2 (canonical state and monotonic `state_version`), §15.5 (the
public surface these operations back), Appendix D.2 (`update_cart` sets an
*absolute* quantity under a retry-safe request ID; an identical repeat returns
the first persisted result; reuse with a different payload is a hard conflict);
ADR-0003 (two-tier locking, and one transaction per unit of work).

Two decisions here are load-bearing.

**Quantities are absolute, never relative.** Appendix D.2: "set one seeded
product to an absolute cart quantity... quantity zero removes the line; positive
values replace its quantity". A relative `add_to_cart` would make a retry
*correct* to apply twice, and there would be no way to tell a duplicated
mutation from an intended second one. Absolute assignment is what makes
idempotency checkable at all.

**A mutation that changes nothing does not bump `state_version`.** Setting a
quantity to the value it already holds succeeds, records its request ID, and
leaves the version alone. The harness reads a version or hash change as evidence
that state moved (FR-032), so a version bump on a no-op would manufacture
exactly the mutation the idempotency policy is looking for.

Run-lock policy is deliberately absent. BUILD_ORDER §7/M2: "keep run-lock policy
outside the store business layer: the harness authorizes whether a human or agent
mutation may be dispatched." The store answers requests; it does not know what a
run is.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Final

from buggy_store.catalog import (
    CATALOG_BY_PRODUCT_ID,
    DISCOUNTS,
    MAX_LINE_QUANTITY,
    MAX_SEARCH_RESULTS,
    Product,
    search_catalog,
)
from buggy_store.confirmations import (
    AUTHORIZING_STATUS,
    Confirmation,
    ConfirmationStatus,
)
from buggy_store.errors import (
    ConfirmationRequired,
    DiscountNotFound,
    ProductNotFound,
    StoreError,
    ValidationFailed,
)
from buggy_store.models import CartLine, Order, StoreState, TargetState, build_cart
from buggy_store.repository import StoreRepository

__all__ = [
    "APPLY_DISCOUNT",
    "DEFAULT_EXPIRY_SECONDS",
    "PROCEED_TO_CHECKOUT",
    "UPDATE_CART",
    "MutationOutcome",
    "StoreService",
]

#: Tool names, used as the idempotency-record scope (§17.1 keys records on it).
UPDATE_CART: Final = "update_cart"
APPLY_DISCOUNT: Final = "apply_discount"
PROCEED_TO_CHECKOUT: Final = "proceed_to_checkout"

#: FR-062 fixes the range at 10..300 seconds and the default at 60.
DEFAULT_EXPIRY_SECONDS: Final = 60
MIN_EXPIRY_SECONDS: Final = 10
MAX_EXPIRY_SECONDS: Final = 300

#: Appendix D.2 bounds `request_id` at 8..80 characters.
MIN_REQUEST_ID: Final = 8
MAX_REQUEST_ID: Final = 80


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    """What a mutation did, and whether this call is the one that did it.

    `replayed` is service-level metadata rather than part of `response`, because
    Appendix D.2 requires a retry to return *the first persisted result* - if the
    replayed body differed from the original, it would not be that result.
    """

    response: Mapping[str, Any]
    state: StoreState
    replayed: bool


class StoreService:
    """The store's operations over one repository.

    The keyed lock is admission control, exactly as ADR-0003 frames it: the
    database transaction remains the serialization boundary and correctness never
    depends on this lock. It exists so concurrent requests for one workspace
    queue in the event loop instead of piling onto SQLite's writer and burning
    the busy timeout.
    """

    def __init__(
        self,
        repository: StoreRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        id_source: Callable[[str], str] | None = None,
    ) -> None:
        self._repository = repository
        self._locks: dict[str, asyncio.Lock] = {}
        #: One clock for the whole store, so a confirmation's expiry and the
        #: instant a decision is dated by cannot disagree.
        self._clock = clock or repository.clock
        #: Injected so a recorded journey replays to the same identifiers
        #: (constitution §1). `uuid4` only when nobody supplied one.
        self._id_source = id_source or (lambda prefix: f"{prefix}_{uuid.uuid4().hex}")

    def _lock_for(self, workspace_id: str) -> asyncio.Lock:
        lock = self._locks.get(workspace_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[workspace_id] = lock
        return lock

    def release_idle_locks(self) -> int:
        """Drop unheld locks so the map cannot grow across the deployment's life.

        ADR-0003 requires bounded cleanup. Called by the store's own maintenance
        path rather than on every request, because sweeping under contention
        would just churn.
        """
        idle = [key for key, lock in self._locks.items() if not lock.locked()]
        for key in idle:
            del self._locks[key]
        return len(idle)

    # -- reads ---------------------------------------------------------------

    async def read_state(self, workspace_id: str) -> StoreState:
        """This workspace's canonical state, seeding it on first contact."""
        async with self._repository.connect() as connection:
            return await self._repository.ensure_workspace(connection, workspace_id)

    def search(self, query: str, max_results: int = 3) -> Sequence[Product]:
        """Appendix D.2's `search_catalog`. Reads nothing and changes nothing."""
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            raise ValidationFailed("max_results must be an integer")
        if not 1 <= max_results <= MAX_SEARCH_RESULTS:
            raise ValidationFailed(
                f"max_results must be between 1 and {MAX_SEARCH_RESULTS}",
                details={"max_results": max_results},
            )
        return search_catalog(query, max_results)

    # -- cart mutation -------------------------------------------------------

    async def update_cart(
        self, workspace_id: str, product_id: str, quantity: int, request_id: str
    ) -> MutationOutcome:
        """Set `product_id` to an absolute `quantity` (Appendix D.2).

        Zero removes the line. The whole operation - replay check, mutation, and
        the idempotency record - is one `BEGIN IMMEDIATE` transaction, so a
        concurrent caller cannot slip between the check and the record and
        produce two first-results for one key.
        """
        self._validate_request_id(request_id)
        product = self._require_product(product_id)
        self._validate_quantity(quantity)
        payload = {"product_id": product_id, "quantity": quantity}

        async with self._lock_for(workspace_id), self._repository.connect() as connection:
            await self._repository.ensure_workspace(connection, workspace_id)
            async with self._repository.transaction(connection):
                replayed = await self._repository.replay_or_claim(
                    connection, workspace_id, UPDATE_CART, request_id, payload
                )
                current = await self._repository.read_state(connection, workspace_id)
                assert current is not None  # ensured above, inside this transaction
                if replayed is not None:
                    # App. D.2: return the first persisted result and do not
                    # mutate again.
                    return MutationOutcome(replayed.response, current, replayed=True)

                next_state = self._apply_quantity(current, product, quantity)
                if next_state is not current:
                    await self._repository.write_state(connection, workspace_id, next_state)

                response = _cart_response(next_state)
                await self._repository.record_result(
                    connection,
                    workspace_id,
                    UPDATE_CART,
                    request_id,
                    payload,
                    response,
                    next_state.state_version,
                )
        return MutationOutcome(response, next_state, replayed=False)

    def _apply_quantity(self, current: StoreState, product: Product, quantity: int) -> StoreState:
        """Absolute assignment, returning `current` unchanged when nothing moved."""
        items = dict(current.target_state.cart.items)
        if quantity == 0:
            items.pop(product.line_key, None)
        else:
            items[product.line_key] = CartLine(
                product_id=product.product_id, quantity=quantity, unit_price=product.price
            )

        existing_discount = current.target_state.cart.discount
        rebuilt = TargetState(
            cart=build_cart(items, existing_discount.code if existing_discount else None),
            order=current.target_state.order,
            preferences=current.target_state.preferences,
        )
        if rebuilt.canonical_document() == current.target_state.canonical_document():
            # A no-op must not manufacture the state change the harness reads as
            # evidence that a mutation happened.
            return current
        return current.with_target_state(rebuilt)

    # -- discount ------------------------------------------------------------

    async def apply_discount(self, workspace_id: str, code: str) -> MutationOutcome:
        """Apply one allowlisted discount to the canonical cart (Appendix D.2).

        Carries no `request_id`, and needs none: Appendix D.2's schema omits one
        because the operation is naturally idempotent. "Reapplying the active
        code returns `already_applied` and does not change state", so repetition
        is safe without a stored record, and inventing a key here would add a
        failure mode (reuse conflicts) the specification does not define.

        The discount is stored as a *code*, and its amount is recomputed whenever
        the lines move. Freezing the amount at application time would leave a
        cart claiming 20% off a subtotal it no longer has.
        """
        self._require_discount(code)

        async with self._lock_for(workspace_id), self._repository.connect() as connection:
            await self._repository.ensure_workspace(connection, workspace_id)
            async with self._repository.transaction(connection):
                current = await self._repository.read_state(connection, workspace_id)
                assert current is not None  # ensured above, inside this transaction

                active = current.target_state.cart.discount
                if active is not None and active.code == code:
                    # A successful no-op, not a failure and not a fresh mutation.
                    return MutationOutcome(
                        _cart_response(current, status="already_applied"),
                        current,
                        replayed=False,
                    )

                rebuilt = TargetState(
                    cart=build_cart(dict(current.target_state.cart.items), code),
                    order=current.target_state.order,
                    preferences=current.target_state.preferences,
                )
                next_state = current.with_target_state(rebuilt)
                await self._repository.write_state(connection, workspace_id, next_state)
        return MutationOutcome(_cart_response(next_state), next_state, replayed=False)

    def _require_discount(self, code: object) -> str:
        if not isinstance(code, str):
            raise ValidationFailed("code must be a string")
        if code not in DISCOUNTS:
            raise DiscountNotFound(
                f"{code!r} is not an allowlisted discount code",
                details={"code": code},
            )
        return code

    # -- confirmation lifecycle (§14, §15.5, FR-066) --------------------------

    async def request_confirmation(
        self, workspace_id: str, *, expires_in_seconds: int = DEFAULT_EXPIRY_SECONDS
    ) -> Confirmation:
        """Create a pending confirmation bound to the cart as it stands now (§14).

        The binding hash is the point. Nothing is held across the human's
        decision (ADR-0003), so the cart can move while the modal is open;
        recording what was shown lets checkout refuse to act on an approval of a
        different cart.
        """
        self._validate_expiry(expires_in_seconds)
        now = self._now()

        async with self._lock_for(workspace_id), self._repository.connect() as connection:
            state = await self._repository.ensure_workspace(connection, workspace_id)
            confirmation = Confirmation(
                confirmation_id=self._id_source("confirmation"),
                workspace_id=workspace_id,
                status=ConfirmationStatus.PENDING,
                state_binding_hash=_state_binding_hash(state),
                # §14.1: "the Buggy Store summary includes cart version and exact
                # total". Bounded to what a human needs in order to decide.
                consequence={
                    "action": PROCEED_TO_CHECKOUT,
                    "state_version": state.state_version,
                    "cart_total": state.target_state.cart.canonical_document()["total"],
                    "item_count": state.target_state.cart.item_count,
                },
                expires_at=now + timedelta(seconds=expires_in_seconds),
                created_at=now,
            )
            async with self._repository.transaction(connection):
                await self._repository.insert_confirmation(connection, confirmation)
        return confirmation

    async def decide_confirmation(
        self, workspace_id: str, confirmation_id: str, *, approved: bool
    ) -> Confirmation:
        """Record a human's decision (§14 step 6: before any order mutation).

        A decision on a lapsed request is refused rather than honoured. §14 lists
        approve, deny, expire and cancel as four outcomes, and letting a late
        approval land would collapse expiry into approval.
        """
        target = ConfirmationStatus.APPROVED if approved else ConfirmationStatus.DENIED
        return await self._transition(
            workspace_id,
            confirmation_id,
            target,
            allowed_from=ConfirmationStatus.PENDING,
        )

    async def cancel_confirmation(self, workspace_id: str, confirmation_id: str) -> Confirmation:
        """Cancel a pending request (§14 step 9), creating no order."""
        return await self._transition(
            workspace_id,
            confirmation_id,
            ConfirmationStatus.CANCELLED,
            allowed_from=ConfirmationStatus.PENDING,
        )

    async def read_confirmation(self, workspace_id: str, confirmation_id: str) -> Confirmation:
        """One confirmation, scoped to its workspace."""
        async with self._repository.connect() as connection:
            return await self._require_confirmation(connection, workspace_id, confirmation_id)

    async def _transition(
        self,
        workspace_id: str,
        confirmation_id: str,
        target: ConfirmationStatus,
        *,
        allowed_from: ConfirmationStatus,
    ) -> Confirmation:
        async with (
            self._lock_for(workspace_id),
            self._repository.connect() as connection,
            self._repository.transaction(connection),
        ):
            confirmation = await self._require_confirmation(
                connection, workspace_id, confirmation_id
            )
            effective = confirmation.effective_status(self._now())
            if effective is not allowed_from:
                raise StoreError(
                    f"confirmation {confirmation_id!r} is {effective}, not {allowed_from}",
                    details={"status": str(effective)},
                )
            moved = await self._repository.set_confirmation_status(
                connection, workspace_id, confirmation_id, target, expected=allowed_from
            )
            if not moved:
                # Another decision won the race between the read and the update.
                # Refusing is correct: two humans cannot both decide one request.
                raise StoreError(
                    f"confirmation {confirmation_id!r} was already decided",
                    details={"confirmation_id": confirmation_id},
                )
            return await self._require_confirmation(connection, workspace_id, confirmation_id)

    # -- protected checkout (§14 step 7, FR-066, App. D.2) --------------------

    async def checkout(
        self, workspace_id: str, *, confirmation_id: str, request_id: str
    ) -> MutationOutcome:
        """Create one simulated order, consuming a valid approval exactly once.

        Revalidation is mandatory rather than defensive: ADR-0003 holds nothing
        between the request and the decision, so the approval is checked against
        *current* state at the moment it is spent. The consume and the order
        creation share one transaction, so an order cannot exist without the
        approval having been spent, nor the approval be spent without an order.

        App. D.2: "repeating a completed checkout request returns its original
        simulated order result and never creates another confirmation or order."
        """
        self._validate_request_id(request_id)
        payload = {"confirmation_id": confirmation_id}

        async with self._lock_for(workspace_id), self._repository.connect() as connection:
            await self._repository.ensure_workspace(connection, workspace_id)
            async with self._repository.transaction(connection):
                replayed = await self._repository.replay_or_claim(
                    connection, workspace_id, PROCEED_TO_CHECKOUT, request_id, payload
                )
                current = await self._repository.read_state(connection, workspace_id)
                assert current is not None  # ensured above, inside this transaction
                if replayed is not None:
                    return MutationOutcome(replayed.response, current, replayed=True)

                confirmation = await self._require_confirmation(
                    connection, workspace_id, confirmation_id
                )
                self._require_authorization(confirmation, current)

                consumed = await self._repository.set_confirmation_status(
                    connection,
                    workspace_id,
                    confirmation_id,
                    ConfirmationStatus.CONSUMED,
                    expected=ConfirmationStatus.APPROVED,
                    consumed=True,
                )
                if not consumed:
                    # Single use: a concurrent checkout already spent it.
                    raise ConfirmationRequired(
                        f"confirmation {confirmation_id!r} has already been used",
                        details={"confirmation_id": confirmation_id},
                    )

                rebuilt = TargetState(
                    cart=current.target_state.cart,
                    order=Order(created=True, order_id=self._id_source("order")),
                    preferences=current.target_state.preferences,
                )
                next_state = current.with_target_state(rebuilt)
                await self._repository.write_state(connection, workspace_id, next_state)

                response = {
                    "status": "success",
                    "state_version": next_state.state_version,
                    "order_id": next_state.target_state.order.order_id,
                    "cart": next_state.target_state.cart.canonical_document(),
                }
                await self._repository.record_result(
                    connection,
                    workspace_id,
                    PROCEED_TO_CHECKOUT,
                    request_id,
                    payload,
                    response,
                    next_state.state_version,
                )
        return MutationOutcome(response, next_state, replayed=False)

    def _require_authorization(self, confirmation: Confirmation, state: StoreState) -> None:
        """FR-066's refusals, each with its own reason.

        "Stale, expired, reused, mismatched, denied, or cancelled confirmations
        shall never authorize a mutation." They stay separate rather than merged
        into one boolean so a human reading the failure learns which happened.
        """
        effective = confirmation.effective_status(self._now())
        if effective not in AUTHORIZING_STATUS:
            raise ConfirmationRequired(
                f"confirmation {confirmation.confirmation_id!r} is {effective} and cannot "
                "authorize a checkout",
                details={"status": str(effective)},
            )
        if confirmation.state_binding_hash != _state_binding_hash(state):
            raise ConfirmationRequired(
                "the cart changed after this confirmation was approved, so the approval "
                "no longer describes what would be ordered",
                details={"confirmation_id": confirmation.confirmation_id},
            )

    async def _require_confirmation(
        self, connection, workspace_id: str, confirmation_id: str
    ) -> Confirmation:
        if not isinstance(confirmation_id, str) or not confirmation_id:
            raise ValidationFailed("confirmation_id must be a non-empty string")
        confirmation = await self._repository.find_confirmation(
            connection, workspace_id, confirmation_id
        )
        if confirmation is None:
            raise StoreError(
                f"confirmation {confirmation_id!r} does not exist in this workspace",
                details={"confirmation_id": confirmation_id},
            )
        return confirmation

    def _validate_expiry(self, seconds: object) -> None:
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            raise ValidationFailed("expires_in_seconds must be an integer")
        if not MIN_EXPIRY_SECONDS <= seconds <= MAX_EXPIRY_SECONDS:
            raise ValidationFailed(
                f"expires_in_seconds must be between {MIN_EXPIRY_SECONDS} and {MAX_EXPIRY_SECONDS}",
                details={"expires_in_seconds": seconds},
            )

    def _now(self) -> datetime:
        instant = self._clock()
        if instant.tzinfo is None:
            raise ValueError("a persisted instant must be timezone-aware")
        return instant.astimezone(UTC)

    # -- validation ----------------------------------------------------------

    def _require_product(self, product_id: object) -> Product:
        if not isinstance(product_id, str):
            raise ValidationFailed("product_id must be a string")
        product = CATALOG_BY_PRODUCT_ID.get(product_id)
        if product is None:
            raise ProductNotFound(
                f"{product_id!r} is not a seeded product",
                details={"product_id": product_id},
            )
        return product

    def _validate_quantity(self, quantity: object) -> None:
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            raise ValidationFailed("quantity must be an integer")
        if not 0 <= quantity <= MAX_LINE_QUANTITY:
            raise ValidationFailed(
                f"quantity must be between 0 and {MAX_LINE_QUANTITY}",
                details={"quantity": quantity},
            )

    def _validate_request_id(self, request_id: object) -> None:
        if not isinstance(request_id, str):
            raise ValidationFailed("request_id must be a string")
        if not MIN_REQUEST_ID <= len(request_id) <= MAX_REQUEST_ID:
            raise ValidationFailed(
                f"request_id must be between {MIN_REQUEST_ID} and {MAX_REQUEST_ID} characters",
                details={"length": len(request_id)},
            )


def _state_binding_hash(state: StoreState) -> str:
    """What a confirmation is bound to: the cart a human was shown (§14).

    The cart alone, not the whole target state. Binding to `preferences` would
    invalidate an approval because someone toggled gift wrap in another tab,
    which is a refusal with no safety value behind it.
    """
    document = json.dumps(
        state.target_state.cart.canonical_document(), sort_keys=True, separators=(",", ":")
    )
    return sha256(document.encode("utf-8")).hexdigest()


def _cart_response(state: StoreState, *, status: str = "success") -> dict[str, Any]:
    """The bounded body a cart mutation returns.

    Canonical cart plus the version, and nothing else. §23.3 keeps evidence
    server-side; a response carrying the whole target state would put order and
    preference data into every tool result that only asked about a cart.
    """
    return {
        "status": status,
        "state_version": state.state_version,
        "cart": state.target_state.cart.canonical_document(),
    }
