"""Confirmation and protected-checkout gates (spec v1.9 §14, §15.5, FR-066; 003-T5).

FR-066 is a list of things that must never authorize a mutation: "stale, expired,
reused, mismatched, denied, or cancelled confirmations". Each gets its own test,
because they fail for different reasons and a reader of the refusal needs to know
which one happened — and because merging them into one boolean is how five checks
quietly become one.

The two properties the exit gate depends on are here as well: an approval is
spent exactly once, and denial, expiry and cancellation create no order. The
single-use test drives two concurrent checkouts against one approval, since the
interesting failure is a race rather than a sequence.

Expiry is tested by moving an injected clock, never by sleeping. BUILD_ORDER §9
forbids fixed sleeps, and a test that waited 60 seconds for the default window
would simply be skipped by whoever ran it next.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from buggy_store.confirmations import ConfirmationStatus
from buggy_store.errors import ConfirmationRequired, StoreError, ValidationFailed
from buggy_store.repository import StoreRepository
from buggy_store.service import DEFAULT_EXPIRY_SECONDS, StoreService

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
MUG = "mug-ceramic-001"


class _MovableClock:
    """A clock that only moves when a test moves it."""

    def __init__(self) -> None:
        self.now = EPOCH

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class _Ids:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        self._counters[prefix] = self._counters.get(prefix, 0) + 1
        return f"{prefix}-{self._counters[prefix]:04d}"


@pytest.fixture
def clock() -> _MovableClock:
    return _MovableClock()


@pytest.fixture
async def service(tmp_path: Path, clock: _MovableClock) -> StoreService:
    repository = StoreRepository(tmp_path / "store.sqlite3", clock=clock)
    await repository.initialize()
    return StoreService(repository, clock=clock, id_source=_Ids())


def _request(suffix: str) -> str:
    return f"req-{suffix:>012}"


async def _cart_with_a_mug(service: StoreService, workspace: str = "ws-1") -> None:
    await service.update_cart(workspace, MUG, 1, _request("1"))


# --- requesting a confirmation (§14) ----------------------------------------


@pytest.mark.integration
async def test_a_request_is_pending_and_bound_to_the_cart_it_showed(
    service: StoreService,
) -> None:
    """§14.1: the summary carries cart version and exact total."""
    await _cart_with_a_mug(service)
    confirmation = await service.request_confirmation("ws-1")

    assert confirmation.status is ConfirmationStatus.PENDING
    assert confirmation.consequence["action"] == "proceed_to_checkout"
    assert confirmation.consequence["cart_total"] == "25.00"
    assert confirmation.consequence["state_version"] == 2
    assert confirmation.state_binding_hash


@pytest.mark.integration
async def test_the_expiry_window_defaults_to_sixty_seconds(
    service: StoreService, clock: _MovableClock
) -> None:
    """FR-062 fixes the default; §14.1 calls it the contract-configured expiry."""
    confirmation = await service.request_confirmation("ws-1")
    assert confirmation.expires_at == clock.now + timedelta(seconds=DEFAULT_EXPIRY_SECONDS)


@pytest.mark.integration
@pytest.mark.parametrize("seconds", [9, 301, 0, -1, True])
async def test_an_expiry_outside_the_specified_range_is_refused(
    service: StoreService, seconds: object
) -> None:
    """FR-062 bounds it at 10..300 seconds."""
    with pytest.raises(ValidationFailed):
        await service.request_confirmation("ws-1", expires_in_seconds=seconds)  # type: ignore[arg-type]


@pytest.mark.integration
async def test_the_returned_document_carries_no_credential_or_raw_payload(
    service: StoreService,
) -> None:
    """§20.3: nothing an agent can read may carry a token or unbounded payload."""
    await _cart_with_a_mug(service)
    document = (await service.request_confirmation("ws-1")).as_document()
    assert set(document) == {"confirmation_id", "status", "consequence", "expires_at"}
    assert set(document["consequence"]) == {
        "action",
        "state_version",
        "cart_total",
        "item_count",
    }


# --- decisions (§14 steps 4-6) ----------------------------------------------


@pytest.mark.integration
async def test_a_human_can_approve_a_pending_request(service: StoreService) -> None:
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    decided = await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)
    assert decided.status is ConfirmationStatus.APPROVED
    assert decided.decided_at is not None


@pytest.mark.integration
async def test_a_human_can_deny_a_pending_request(service: StoreService) -> None:
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    decided = await service.decide_confirmation("ws-1", pending.confirmation_id, approved=False)
    assert decided.status is ConfirmationStatus.DENIED


@pytest.mark.integration
async def test_a_pending_request_can_be_cancelled(service: StoreService) -> None:
    """§14 step 9: the invocation was aborted while the dialog was open."""
    pending = await service.request_confirmation("ws-1")
    cancelled = await service.cancel_confirmation("ws-1", pending.confirmation_id)
    assert cancelled.status is ConfirmationStatus.CANCELLED


@pytest.mark.integration
async def test_a_second_decision_on_one_request_is_refused(service: StoreService) -> None:
    """Two humans cannot both decide; the first decision is the decision."""
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)
    with pytest.raises(StoreError, match="approved, not pending"):
        await service.decide_confirmation("ws-1", pending.confirmation_id, approved=False)


@pytest.mark.integration
async def test_a_decision_after_expiry_is_refused(
    service: StoreService, clock: _MovableClock
) -> None:
    """Letting a late approval land would collapse expiry into approval."""
    pending = await service.request_confirmation("ws-1")
    clock.advance(DEFAULT_EXPIRY_SECONDS + 1)
    with pytest.raises(StoreError, match="expired, not pending"):
        await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)


@pytest.mark.integration
async def test_a_lapsed_pending_request_reads_as_expired_without_a_sweeper(
    service: StoreService, clock: _MovableClock
) -> None:
    """Reading the stored status alone would let a lapsed approval still authorize."""
    pending = await service.request_confirmation("ws-1")
    clock.advance(DEFAULT_EXPIRY_SECONDS)
    current = await service.read_confirmation("ws-1", pending.confirmation_id)
    assert current.status is ConfirmationStatus.PENDING
    assert current.effective_status(clock.now) is ConfirmationStatus.EXPIRED


@pytest.mark.integration
async def test_a_confirmation_from_another_workspace_is_not_found(
    service: StoreService,
) -> None:
    """§20.1: an identifier is never an authorization mechanism."""
    pending = await service.request_confirmation("ws-1")
    with pytest.raises(StoreError, match="does not exist in this workspace"):
        await service.decide_confirmation("ws-2", pending.confirmation_id, approved=True)


@pytest.mark.integration
@pytest.mark.parametrize("confirmation_id", ["", None, 123])
async def test_a_malformed_confirmation_id_is_refused(
    service: StoreService, confirmation_id: object
) -> None:
    with pytest.raises(ValidationFailed):
        await service.read_confirmation("ws-1", confirmation_id)  # type: ignore[arg-type]


# --- protected checkout (§14 step 7) ----------------------------------------


@pytest.mark.integration
async def test_an_approved_confirmation_creates_exactly_one_order(
    service: StoreService,
) -> None:
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)

    outcome = await service.checkout(
        "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
    )
    assert outcome.response["status"] == "success"
    assert outcome.state.target_state.order.created is True
    assert outcome.state.target_state.order.order_id == "order-0001"


@pytest.mark.integration
async def test_checkout_consumes_the_approval(service: StoreService) -> None:
    """FR-066: approval is single-use, and `consumed` is a status of its own."""
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)
    await service.checkout(
        "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
    )

    spent = await service.read_confirmation("ws-1", pending.confirmation_id)
    assert spent.status is ConfirmationStatus.CONSUMED
    assert spent.consumed_at is not None


@pytest.mark.integration
async def test_a_consumed_approval_cannot_authorize_a_second_order(
    service: StoreService,
) -> None:
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)
    await service.checkout(
        "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
    )

    with pytest.raises(ConfirmationRequired, match="consumed"):
        await service.checkout(
            "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("10")
        )


@pytest.mark.integration
async def test_concurrent_checkouts_on_one_approval_produce_one_order(
    service: StoreService,
) -> None:
    """The interesting failure is a race, not a sequence."""
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)

    results = await asyncio.gather(
        *(
            service.checkout(
                "ws-1", confirmation_id=pending.confirmation_id, request_id=_request(str(index))
            )
            for index in range(20, 24)
        ),
        return_exceptions=True,
    )
    succeeded = [result for result in results if not isinstance(result, BaseException)]
    refused = [result for result in results if isinstance(result, ConfirmationRequired)]

    assert len(succeeded) == 1
    assert len(refused) == 3
    state = await service.read_state("ws-1")
    assert state.target_state.order.order_id == "order-0001"


# --- FR-066's refusals ------------------------------------------------------


@pytest.mark.integration
async def test_checkout_without_any_confirmation_is_refused(service: StoreService) -> None:
    await _cart_with_a_mug(service)
    with pytest.raises(StoreError, match="does not exist"):
        await service.checkout("ws-1", confirmation_id="never-issued", request_id=_request("9"))
    assert (await service.read_state("ws-1")).target_state.order.created is False


@pytest.mark.integration
async def test_an_undecided_confirmation_cannot_authorize_a_checkout(
    service: StoreService,
) -> None:
    """Requesting consent is not receiving it."""
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    with pytest.raises(ConfirmationRequired, match="pending"):
        await service.checkout(
            "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
        )
    assert (await service.read_state("ws-1")).target_state.order.created is False


@pytest.mark.integration
async def test_a_denied_confirmation_creates_no_order(service: StoreService) -> None:
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=False)

    with pytest.raises(ConfirmationRequired, match="denied"):
        await service.checkout(
            "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
        )
    assert (await service.read_state("ws-1")).target_state.order.created is False


@pytest.mark.integration
async def test_a_cancelled_confirmation_creates_no_order(service: StoreService) -> None:
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.cancel_confirmation("ws-1", pending.confirmation_id)

    with pytest.raises(ConfirmationRequired, match="cancelled"):
        await service.checkout(
            "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
        )
    assert (await service.read_state("ws-1")).target_state.order.created is False


@pytest.mark.integration
async def test_an_expired_approval_creates_no_order(
    service: StoreService, clock: _MovableClock
) -> None:
    """Approved in time, spent too late. Expiry outranks the stored approval."""
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)
    clock.advance(DEFAULT_EXPIRY_SECONDS + 1)

    with pytest.raises(ConfirmationRequired):
        await service.checkout(
            "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
        )
    assert (await service.read_state("ws-1")).target_state.order.created is False


@pytest.mark.integration
async def test_an_approval_for_a_cart_that_has_since_changed_is_refused(
    service: StoreService,
) -> None:
    """The stale case §14 exists to prevent: approve 25.00, check out 75.00."""
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)

    await service.update_cart("ws-1", MUG, 3, _request("2"))

    with pytest.raises(ConfirmationRequired, match="cart changed"):
        await service.checkout(
            "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
        )
    assert (await service.read_state("ws-1")).target_state.order.created is False


@pytest.mark.integration
async def test_an_approval_survives_a_change_that_puts_the_cart_back(
    service: StoreService,
) -> None:
    """The binding is to the cart's *value*, not to a version counter.

    Returning the cart to what the human approved is not a stale approval; it is
    the same order they agreed to. Binding to `state_version` instead would
    refuse this for no safety reason.
    """
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)

    await service.update_cart("ws-1", MUG, 3, _request("2"))
    await service.update_cart("ws-1", MUG, 1, _request("3"))

    outcome = await service.checkout(
        "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
    )
    assert outcome.state.target_state.order.created is True


@pytest.mark.integration
async def test_a_confirmation_cannot_authorize_a_checkout_in_another_workspace(
    service: StoreService,
) -> None:
    await _cart_with_a_mug(service, "ws-1")
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)

    with pytest.raises(StoreError, match="does not exist in this workspace"):
        await service.checkout(
            "ws-2", confirmation_id=pending.confirmation_id, request_id=_request("9")
        )
    assert (await service.read_state("ws-2")).target_state.order.created is False


# --- checkout retry semantics (Appendix D.2) --------------------------------


@pytest.mark.integration
async def test_repeating_a_completed_checkout_returns_its_original_result(
    service: StoreService,
) -> None:
    """App. D.2: "...and never creates another confirmation or order"."""
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)

    first = await service.checkout(
        "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
    )
    repeat = await service.checkout(
        "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
    )

    assert repeat.replayed is True
    assert repeat.response == first.response
    assert repeat.response["order_id"] == "order-0001"


@pytest.mark.integration
async def test_a_replayed_checkout_does_not_advance_the_state_version(
    service: StoreService,
) -> None:
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", pending.confirmation_id, approved=True)
    first = await service.checkout(
        "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
    )
    repeat = await service.checkout(
        "ws-1", confirmation_id=pending.confirmation_id, request_id=_request("9")
    )
    assert repeat.state.state_version == first.state.state_version


# --- reset ------------------------------------------------------------------


@pytest.mark.integration
async def test_resetting_a_workspace_clears_its_confirmations(
    service: StoreService, tmp_path: Path
) -> None:
    await _cart_with_a_mug(service)
    pending = await service.request_confirmation("ws-1")

    repository = StoreRepository(tmp_path / "store.sqlite3", clock=_MovableClock())
    async with repository.connect() as connection:
        await repository.reset_workspace(connection, "ws-1")

    with pytest.raises(StoreError, match="does not exist"):
        await service.read_confirmation("ws-1", pending.confirmation_id)
