"""Discount gates (spec v1.9 §13.1, §13.2, App. D.2; 003-T4).

The arithmetic here is the arithmetic the product's headline failure is measured
against: §13.2's worked example is a 25.00 subtotal, a 5.00 SAVE20 discount and a
20.00 total, and §10.1's prebuilt contract asserts `target.cart.total` equals
`"20.00"`. Appendix B shows the same contract failing with actual `"25.00"` when
the `pre_fix` profile reports success without applying anything.

So these tests pin the *correct* behaviour precisely. If the honest path were
wrong, the injected fault in 003-T8 would prove nothing — a discount that never
worked is not a discount that lied.

The recomputation test is the one that is easy to miss. A discount is stored as a
code, not a frozen amount, so changing the cart afterwards must move the
discount with it; a frozen amount would leave a cart claiming 20% off a subtotal
it no longer has.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from buggy_store.errors import DiscountNotFound, ValidationFailed
from buggy_store.repository import StoreRepository
from buggy_store.service import StoreService

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
MUG = "mug-ceramic-001"
NOTEBOOK = "notebook-001"


class _Clock:
    def __init__(self) -> None:
        self._tick = 0

    def __call__(self) -> datetime:
        self._tick += 1
        return EPOCH + timedelta(seconds=self._tick)


@pytest.fixture
async def service(tmp_path: Path) -> StoreService:
    repository = StoreRepository(tmp_path / "store.sqlite3", clock=_Clock())
    await repository.initialize()
    return StoreService(repository)


def _request(suffix: str) -> str:
    return f"req-{suffix:>012}"


# --- the specified arithmetic (§13.2) ---------------------------------------


@pytest.mark.integration
async def test_the_specs_worked_example_reproduces_exactly(service: StoreService) -> None:
    """§13.2: one mug at 25.00 with SAVE20 gives 5.00 off and a 20.00 total."""
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    outcome = await service.apply_discount("ws-1", "SAVE20")

    cart = outcome.response["cart"]
    assert cart["subtotal"] == "25.00"
    assert cart["discount"] == {"code": "SAVE20", "amount": "5.00"}
    assert cart["total"] == "20.00"


@pytest.mark.integration
async def test_the_prebuilt_contracts_asserted_total_is_reachable(
    service: StoreService,
) -> None:
    """§10.1 asserts `target.cart.total` equals "20.00"; the honest path must produce it."""
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    await service.apply_discount("ws-1", "SAVE20")
    state = await service.read_state("ws-1")
    assert state.target_state.cart.canonical_document()["total"] == "20.00"


@pytest.mark.integration
async def test_a_discount_reduces_the_total_and_leaves_the_subtotal_alone(
    service: StoreService,
) -> None:
    await service.update_cart("ws-1", MUG, 2, _request("1"))
    cart = (await service.apply_discount("ws-1", "SAVE20")).response["cart"]
    assert cart["subtotal"] == "50.00"
    assert cart["discount"]["amount"] == "10.00"
    assert cart["total"] == "40.00"


@pytest.mark.integration
async def test_a_discount_applies_across_mixed_lines(service: StoreService) -> None:
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    await service.update_cart("ws-1", NOTEBOOK, 2, _request("2"))
    cart = (await service.apply_discount("ws-1", "SAVE20")).response["cart"]
    assert cart["subtotal"] == "49.00"
    assert cart["discount"]["amount"] == "9.80"
    assert cart["total"] == "39.20"


@pytest.mark.integration
async def test_every_money_field_stays_a_two_place_decimal_string(
    service: StoreService,
) -> None:
    """§13.2: decimal strings, never binary floating point."""
    await service.update_cart("ws-1", NOTEBOOK, 1, _request("1"))
    cart = (await service.apply_discount("ws-1", "SAVE20")).response["cart"]
    for value in (cart["subtotal"], cart["total"], cart["discount"]["amount"]):
        assert isinstance(value, str)
        assert value.split(".")[1] == value.split(".")[1][:2]
        assert len(value.split(".")[1]) == 2


# --- recomputation ----------------------------------------------------------


@pytest.mark.integration
async def test_the_discount_follows_the_cart_when_lines_change(
    service: StoreService,
) -> None:
    """A frozen amount would claim 20% off a subtotal the cart no longer has."""
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    await service.apply_discount("ws-1", "SAVE20")

    outcome = await service.update_cart("ws-1", MUG, 2, _request("2"))
    cart = outcome.response["cart"]
    assert cart["subtotal"] == "50.00"
    assert cart["discount"]["amount"] == "10.00"
    assert cart["total"] == "40.00"


@pytest.mark.integration
async def test_emptying_a_discounted_cart_leaves_consistent_zeroes(
    service: StoreService,
) -> None:
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    await service.apply_discount("ws-1", "SAVE20")
    cart = (await service.update_cart("ws-1", MUG, 0, _request("2"))).response["cart"]
    assert cart["subtotal"] == "0.00"
    assert cart["discount"] == {"code": "SAVE20", "amount": "0.00"}
    assert cart["total"] == "0.00"


@pytest.mark.integration
async def test_a_discount_on_an_empty_cart_is_allowed_and_takes_nothing_off(
    service: StoreService,
) -> None:
    cart = (await service.apply_discount("ws-1", "SAVE20")).response["cart"]
    assert cart["subtotal"] == "0.00"
    assert cart["total"] == "0.00"


# --- repeated application (Appendix D.2) ------------------------------------


@pytest.mark.integration
async def test_reapplying_the_active_code_reports_already_applied(
    service: StoreService,
) -> None:
    """App. D.2: "reapplying the active code returns already_applied"."""
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    first = await service.apply_discount("ws-1", "SAVE20")
    repeat = await service.apply_discount("ws-1", "SAVE20")

    assert first.response["status"] == "success"
    assert repeat.response["status"] == "already_applied"


@pytest.mark.integration
async def test_reapplying_the_active_code_changes_no_state(service: StoreService) -> None:
    """ "...and does not change state" — including the version the harness reads."""
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    applied = await service.apply_discount("ws-1", "SAVE20")
    before = (await service.read_state("ws-1")).canonical_document()

    repeat = await service.apply_discount("ws-1", "SAVE20")

    assert repeat.state.state_version == applied.state.state_version
    assert (await service.read_state("ws-1")).canonical_document() == before


@pytest.mark.integration
async def test_a_no_op_reapplication_is_successful_rather_than_an_error(
    service: StoreService,
) -> None:
    """`already_applied` is a success status; failing here would punish a safe retry."""
    await service.apply_discount("ws-1", "SAVE20")
    repeat = await service.apply_discount("ws-1", "SAVE20")
    assert repeat.response["status"] == "already_applied"
    assert "error" not in repeat.response


# --- validation -------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("code", ["SAVE10", "save20", "", "FREESHIP"])
async def test_a_code_outside_the_allowlist_is_refused(service: StoreService, code: str) -> None:
    """Appendix D.2 enumerates the code, so an unknown one is refused, not ignored."""
    with pytest.raises(DiscountNotFound) as excinfo:
        await service.apply_discount("ws-1", code)
    assert excinfo.value.code == "DISCOUNT_NOT_FOUND"


@pytest.mark.integration
async def test_an_unknown_code_changes_nothing(service: StoreService) -> None:
    """A zero-percent no-op would look like a discount that had been applied."""
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    before = (await service.read_state("ws-1")).canonical_document()

    with pytest.raises(DiscountNotFound):
        await service.apply_discount("ws-1", "SAVE10")

    assert (await service.read_state("ws-1")).canonical_document() == before


@pytest.mark.integration
@pytest.mark.parametrize("code", [None, 20, True])
async def test_a_non_string_code_is_refused(service: StoreService, code: object) -> None:
    with pytest.raises(ValidationFailed):
        await service.apply_discount("ws-1", code)  # type: ignore[arg-type]


# --- isolation --------------------------------------------------------------


@pytest.mark.integration
async def test_a_discount_applies_to_one_workspace_only(service: StoreService) -> None:
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    await service.update_cart("ws-2", MUG, 1, _request("2"))
    await service.apply_discount("ws-1", "SAVE20")

    first = await service.read_state("ws-1")
    second = await service.read_state("ws-2")
    assert first.target_state.cart.discount is not None
    assert second.target_state.cart.discount is None
    assert second.target_state.cart.canonical_document()["total"] == "25.00"
