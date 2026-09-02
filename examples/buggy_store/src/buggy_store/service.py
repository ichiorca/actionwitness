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
    FaultProfileUnavailable,
    ProductNotFound,
    StoreError,
    ValidationFailed,
)
from buggy_store.failure_injection import (
    IMPLEMENTED_PROFILES,
    PROFILE_DESCRIPTIONS,
    FaultProfile,
    ScenarioConfiguration,
    ScenarioMode,
)
from buggy_store.models import (
    CartLine,
    Order,
    Preferences,
    StoreState,
    TargetState,
    build_cart,
)
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

#: What `undeclared_side_effect` writes to `preferences.delivery_note` (§13.3).
#:
#: Fixed rather than generated. A value that varied per run would change the
#: canonical document between two otherwise identical runs, and §24 compares a
#: replayed run against a recorded one by exactly that document.
UNDECLARED_SIDE_EFFECT_NOTE: Final = "leave with the neighbour"


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
                scenario = (
                    await self._repository.read_scenario(connection, workspace_id)
                ) or ScenarioConfiguration()
                duplicating = scenario.injects(FaultProfile.DUPLICATE_ON_RETRY)
                if replayed is not None and not duplicating:
                    # App. D.2: return the first persisted result and do not
                    # mutate again.
                    return MutationOutcome(replayed.response, current, replayed=True)

                next_state = self._apply_quantity(
                    current, product, quantity, scenario, duplicating=replayed is not None
                )
                if next_state is not current:
                    await self._repository.write_state(connection, workspace_id, next_state)

                response = _cart_response(next_state)
                if replayed is None:
                    await self._repository.record_result(
                        connection,
                        workspace_id,
                        UPDATE_CART,
                        request_id,
                        payload,
                        response,
                        next_state.state_version,
                    )
                # A duplicated retry writes no second record: the key already
                # holds one, and inserting again would raise rather than
                # mutate. §13.3 wants a response that "stays syntactically
                # valid", so the fault has to leave the bookkeeping intact and
                # corrupt only the state — which is exactly what makes it hard
                # to catch from the response alone.
        return MutationOutcome(response, next_state, replayed=False)

    def _apply_quantity(
        self,
        current: StoreState,
        product: Product,
        quantity: int,
        scenario: ScenarioConfiguration,
        *,
        duplicating: bool = False,
    ) -> StoreState:
        """Absolute assignment, returning `current` unchanged when nothing moved.

        `duplicating` is §13.3's `duplicate_on_retry`, and it has to *add* rather
        than re-assign. Appendix D.2 makes `quantity` absolute, so a retry that
        merely re-applied the same absolute value would leave the cart identical
        and change no state at all — the fault would be undetectable, and
        `idempotent_by_request_id` would pass while the store misbehaved. The
        realistic bug is a retry treated as a fresh delta, which is what AC-05
        means by "the evidence shows the duplicate state change".

        **The duplicated sum saturates at `MAX_LINE_QUANTITY`.** A doubled
        quantity can ask for a line the canonical model refuses to hold —
        Appendix D.2 caps `update_cart.quantity` at five and `CartLine` carries
        the same bound — and there are only three things the store can do about
        it. Raising the bound would let a fault widen a canonical-state
        invariant, which §5 forbids outright. Refusing with a `StoreError` would
        keep the envelope well-formed but break the profile: §13.3 requires the
        tool response to stay "syntactically valid", and a caller that can read
        the duplication straight off a 4xx never needs the independent
        observation this whole product exists to make. Saturating keeps both
        halves — the response is an ordinary success, and observed state still
        disagrees with what the caller asked for, which is the disagreement the
        harness reports.

        The clamp does cost one case: a line already at the ceiling saturates
        back to the value the caller requested, so that particular retry shows
        no duplication at all. That is the honest consequence of a bounded
        quantity domain rather than a defect — the store cannot hold six mugs,
        so there is no duplicate to observe — and the journeys AC-05 exercises
        sit well below the ceiling.

        The scenario is taken here rather than applied by the caller afterwards so
        that one tool call produces exactly one state version. Injecting the side
        effect as a second `with_target_state` would bump the version twice for a
        single `update_cart`, and §13.2's monotonic counter is evidence the
        harness reads — a mutation that moved it by two would be a defect this
        demo did not mean to inject.
        """
        items = dict(current.target_state.cart.items)
        if quantity == 0:
            items.pop(product.line_key, None)
        else:
            existing = items.get(product.line_key)
            applied = quantity
            if duplicating and existing is not None:
                # `min`, not a bare sum: see the docstring. The bound belongs to
                # canonical state, so the fault bends the number and never the
                # invariant.
                applied = min(existing.quantity + quantity, MAX_LINE_QUANTITY)
            items[product.line_key] = CartLine(
                product_id=product.product_id, quantity=applied, unit_price=product.price
            )

        existing_discount = current.target_state.cart.discount
        rebuilt = TargetState(
            cart=build_cart(items, existing_discount.code if existing_discount else None),
            order=current.target_state.order,
            preferences=self._preferences_after(current, scenario),
        )
        if rebuilt.canonical_document() == current.target_state.canonical_document():
            # A no-op must not manufacture the state change the harness reads as
            # evidence that a mutation happened.
            return current
        return current.with_target_state(rebuilt)

    def _preferences_after(
        self, current: StoreState, scenario: ScenarioConfiguration
    ) -> Preferences:
        """§13.3's `undeclared_side_effect`, injected onto a *correct* mutation.

        The defect this profile demonstrates is not a wrong cart. The cart is
        exactly right, every declared assertion passes, and the journey also
        rewrites a saved preference no contract term mentions — which is why
        §13.2 carries `preferences` at all: "so that a journey can change
        canonical state outside the paths a cart contract asserts".

        That shape is the whole argument for undeclared-change detection. A
        reviewer reading a green assertion list would conclude the run was clean;
        only the blast-radius check disagrees, and it disagrees without anybody
        having predicted *which* path would move.

        The note is a fixed string, not a generated one: a value that varied per
        run would change the canonical document between two otherwise identical
        runs and make the §24 replay comparison non-deterministic.
        """
        preferences = current.target_state.preferences
        if not scenario.injects(FaultProfile.UNDECLARED_SIDE_EFFECT):
            return preferences
        return Preferences(
            delivery_note=UNDECLARED_SIDE_EFFECT_NOTE,
            gift_wrap=preferences.gift_wrap,
        )

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
                scenario = (
                    await self._repository.read_scenario(connection, workspace_id)
                ) or ScenarioConfiguration()

                if scenario.injects(FaultProfile.DISCOUNT_REPORTED_BUT_NOT_APPLIED):
                    # §13.3: "apply_discount returns an apparent success
                    # response. Canonical cart state retains no discount or
                    # unchanged total."
                    #
                    # The response carries the cart the discount *would* have
                    # produced, which is what this class of defect looks like in
                    # the wild: the response is computed optimistically and the
                    # write never lands. Nothing is persisted and the version
                    # does not move, matching Appendix B's evidence of an
                    # unchanged state_version either side of the call.
                    #
                    # This is the whole point of the demo. The tool's word says
                    # 20.00; independent observation says 25.00; the harness is
                    # what notices.
                    optimistic = build_cart(dict(current.target_state.cart.items), code)
                    return MutationOutcome(
                        {
                            "status": "success",
                            "state_version": current.state_version,
                            "cart": optimistic.canonical_document(),
                        },
                        current,
                        replayed=False,
                    )

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

    # -- scenario selection (FR-011, FR-012, FR-017, FR-018) ------------------

    async def read_scenario(self, workspace_id: str) -> ScenarioConfiguration:
        """This workspace's scenario selection, seeding the workspace if needed."""
        async with self._repository.connect() as connection:
            await self._repository.ensure_workspace(connection, workspace_id)
            scenario = await self._repository.read_scenario(connection, workspace_id)
        return scenario or ScenarioConfiguration()

    async def select_scenario(
        self, workspace_id: str, mode: str, fault_profile: str = FaultProfile.NONE
    ) -> ScenarioConfiguration:
        """Choose the scenario mode and fault profile, and reseed mutable state.

        Reseeding is FR-018: switching modes "shall preserve the selected target,
        contract, fixture, intent variant, and comparison-fault identity, reset
        mutable target state, and create a new run configuration". A switch that
        left the old cart in place would carry state built under one
        implementation into a run of the other, and the matched comparison
        FR-019 draws between them would be comparing two different journeys.

        An unimplemented profile is refused rather than downgraded to `none`. A
        store that quietly ran the honest path while a report said a fault was
        active would be lying about the one thing it exists to demonstrate.
        """
        scenario = ScenarioConfiguration(
            mode=self._require_mode(mode), fault_profile=self._require_profile(fault_profile)
        )
        async with self._lock_for(workspace_id), self._repository.connect() as connection:
            await self._repository.ensure_workspace(connection, workspace_id)
            await self._repository.reset_workspace(connection, workspace_id)
            async with self._repository.transaction(connection):
                await self._repository.write_scenario(connection, workspace_id, scenario)
        return scenario

    def _require_mode(self, mode: object) -> ScenarioMode:
        if not isinstance(mode, str):
            raise ValidationFailed("scenario_mode must be a string")
        try:
            return ScenarioMode(mode)
        except ValueError as exc:
            raise ValidationFailed(
                f"{mode!r} is not a scenario mode; FR-017 fixes them at "
                f"{sorted(item.value for item in ScenarioMode)}",
                details={"scenario_mode": mode},
            ) from exc

    def _require_profile(self, profile: object) -> FaultProfile:
        if not isinstance(profile, str):
            raise ValidationFailed("fault_profile must be a string")
        try:
            recognized = FaultProfile(profile)
        except ValueError as exc:
            raise ValidationFailed(
                f"{profile!r} is not a known fault profile",
                details={"fault_profile": profile},
            ) from exc
        if recognized not in IMPLEMENTED_PROFILES:
            raise FaultProfileUnavailable(
                f"the {recognized.value!r} profile is recognised but not implemented in this "
                "build; it ships with its own injector and acceptance test",
                details={
                    "fault_profile": recognized.value,
                    "description": PROFILE_DESCRIPTIONS[recognized],
                    "implemented": sorted(item.value for item in IMPLEMENTED_PROFILES),
                },
            )
        return recognized

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
