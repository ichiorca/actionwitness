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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from buggy_store.catalog import (
    CATALOG_BY_PRODUCT_ID,
    DISCOUNTS,
    MAX_LINE_QUANTITY,
    MAX_SEARCH_RESULTS,
    Product,
    search_catalog,
)
from buggy_store.errors import DiscountNotFound, ProductNotFound, ValidationFailed
from buggy_store.models import CartLine, StoreState, TargetState, build_cart
from buggy_store.repository import StoreRepository

__all__ = ["APPLY_DISCOUNT", "UPDATE_CART", "MutationOutcome", "StoreService"]

#: Tool names, used as the idempotency-record scope (§17.1 keys records on it).
UPDATE_CART: Final = "update_cart"
APPLY_DISCOUNT: Final = "apply_discount"

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

    def __init__(self, repository: StoreRepository) -> None:
        self._repository = repository
        self._locks: dict[str, asyncio.Lock] = {}

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
