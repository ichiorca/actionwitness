"""Cart-mutation gates (spec v1.9 §13.2, App. D.2; 003-T3).

Appendix D.2 defines `update_cart` as an *absolute* assignment under a retry-safe
request ID, and the exit gate asks for two properties: "normal retries return the
first persisted result; conflicting request-ID reuse returns a non-retryable
conflict".

Absoluteness is what makes the rest checkable. A relative `add_to_cart` would
make a retry *correct* to apply twice, and nothing downstream could distinguish a
duplicated mutation from an intended second one — which is the whole subject of
§13.3's `duplicate_on_retry` profile and the harness's idempotency policy.

The no-op test is the subtle one. Setting a quantity to the value it already
holds must succeed without bumping `state_version`, because the harness reads a
version or hash change as evidence that state moved (FR-032). A bump there would
manufacture exactly the mutation the idempotency policy is hunting for.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from buggy_store.errors import IdempotencyConflict, ProductNotFound, ValidationFailed
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
    """A request ID inside Appendix D.2's 8..80 character bound."""
    return f"req-{suffix:>012}"


# --- absolute assignment (Appendix D.2) -------------------------------------


@pytest.mark.integration
async def test_a_mutation_sets_an_absolute_quantity(service: StoreService) -> None:
    outcome = await service.update_cart("ws-1", MUG, 2, _request("1"))
    assert outcome.response["cart"]["items"]["mug"]["quantity"] == 2
    assert outcome.response["cart"]["subtotal"] == "50.00"
    assert outcome.replayed is False


@pytest.mark.integration
async def test_a_second_mutation_replaces_rather_than_adds(service: StoreService) -> None:
    """Absolute, not relative: this is what makes a retry checkable at all."""
    await service.update_cart("ws-1", MUG, 2, _request("1"))
    outcome = await service.update_cart("ws-1", MUG, 3, _request("2"))
    assert outcome.response["cart"]["items"]["mug"]["quantity"] == 3
    assert outcome.response["cart"]["subtotal"] == "75.00"


@pytest.mark.integration
async def test_quantity_zero_removes_the_line(service: StoreService) -> None:
    await service.update_cart("ws-1", MUG, 2, _request("1"))
    outcome = await service.update_cart("ws-1", MUG, 0, _request("2"))
    assert outcome.response["cart"]["items"] == {}
    assert outcome.response["cart"]["subtotal"] == "0.00"


@pytest.mark.integration
async def test_removing_a_line_that_was_never_there_is_not_an_error(
    service: StoreService,
) -> None:
    """Setting zero is an assignment; asserting the absent line exists is not."""
    outcome = await service.update_cart("ws-1", MUG, 0, _request("1"))
    assert outcome.response["cart"]["items"] == {}


@pytest.mark.integration
async def test_lines_are_keyed_by_line_key_not_product_id(service: StoreService) -> None:
    """§13.1: contract authors address `target.cart.items.mug.quantity`."""
    outcome = await service.update_cart("ws-1", MUG, 1, _request("1"))
    assert set(outcome.response["cart"]["items"]) == {"mug"}
    assert outcome.response["cart"]["items"]["mug"]["product_id"] == MUG


@pytest.mark.integration
async def test_several_products_accumulate_as_separate_lines(service: StoreService) -> None:
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    outcome = await service.update_cart("ws-1", NOTEBOOK, 2, _request("2"))
    assert set(outcome.response["cart"]["items"]) == {"mug", "notebook"}
    assert outcome.response["cart"]["subtotal"] == "49.00"


# --- state versioning (§13.2, FR-032) ---------------------------------------


@pytest.mark.integration
async def test_a_real_mutation_advances_the_state_version(service: StoreService) -> None:
    before = (await service.read_state("ws-1")).state_version
    outcome = await service.update_cart("ws-1", MUG, 1, _request("1"))
    assert outcome.state.state_version == before + 1


@pytest.mark.integration
async def test_a_mutation_that_changes_nothing_does_not_advance_the_version(
    service: StoreService,
) -> None:
    """A bump here would manufacture the state change the harness reads as a mutation."""
    await service.update_cart("ws-1", MUG, 2, _request("1"))
    settled = (await service.read_state("ws-1")).state_version

    outcome = await service.update_cart("ws-1", MUG, 2, _request("2"))
    assert outcome.replayed is False
    assert outcome.state.state_version == settled
    assert outcome.response["cart"]["items"]["mug"]["quantity"] == 2


@pytest.mark.integration
async def test_versions_never_move_backwards_across_a_sequence(
    service: StoreService,
) -> None:
    versions = []
    for index, quantity in enumerate((1, 2, 2, 3, 0), start=1):
        outcome = await service.update_cart("ws-1", MUG, quantity, _request(str(index)))
        versions.append(outcome.state.state_version)
    assert versions == sorted(versions)


# --- retry semantics (Appendix D.2) -----------------------------------------


@pytest.mark.integration
async def test_an_identical_repeat_returns_the_first_persisted_result(
    service: StoreService,
) -> None:
    """App. D.2: "return the first persisted result and do not mutate again"."""
    first = await service.update_cart("ws-1", MUG, 2, _request("1"))
    repeat = await service.update_cart("ws-1", MUG, 2, _request("1"))

    assert repeat.replayed is True
    assert repeat.response == first.response
    assert repeat.state.state_version == first.state.state_version


@pytest.mark.integration
async def test_a_repeat_does_not_mutate_again_even_after_other_changes(
    service: StoreService,
) -> None:
    """The recorded result is replayed verbatim, not recomputed from current state."""
    first = await service.update_cart("ws-1", MUG, 2, _request("1"))
    await service.update_cart("ws-1", NOTEBOOK, 1, _request("2"))

    repeat = await service.update_cart("ws-1", MUG, 2, _request("1"))
    assert repeat.response == first.response
    assert "notebook" not in repeat.response["cart"]["items"]

    # The cart itself still carries both lines; only the replayed body is frozen.
    current = await service.read_state("ws-1")
    assert set(current.target_state.cart.items) == {"mug", "notebook"}


@pytest.mark.integration
async def test_reusing_a_request_id_with_a_different_payload_is_refused(
    service: StoreService,
) -> None:
    await service.update_cart("ws-1", MUG, 2, _request("1"))
    with pytest.raises(IdempotencyConflict) as excinfo:
        await service.update_cart("ws-1", MUG, 5, _request("1"))
    assert excinfo.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert excinfo.value.retryable is False
    assert excinfo.value.http_status == 409


@pytest.mark.integration
async def test_a_refused_reuse_mutates_nothing(service: StoreService) -> None:
    """The conflict must leave the cart exactly as the first call left it."""
    await service.update_cart("ws-1", MUG, 2, _request("1"))
    before = (await service.read_state("ws-1")).canonical_document()

    with pytest.raises(IdempotencyConflict):
        await service.update_cart("ws-1", MUG, 5, _request("1"))

    assert (await service.read_state("ws-1")).canonical_document() == before


@pytest.mark.integration
async def test_changing_the_product_under_one_request_id_is_also_a_conflict(
    service: StoreService,
) -> None:
    """The whole payload is the intent, not just the quantity."""
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    with pytest.raises(IdempotencyConflict):
        await service.update_cart("ws-1", NOTEBOOK, 1, _request("1"))


@pytest.mark.integration
async def test_a_request_id_is_scoped_to_its_workspace(service: StoreService) -> None:
    """One shopper's key must not replay into another shopper's cart."""
    await service.update_cart("ws-1", MUG, 2, _request("1"))
    other = await service.update_cart("ws-2", NOTEBOOK, 1, _request("1"))

    assert other.replayed is False
    assert set(other.response["cart"]["items"]) == {"notebook"}


# --- workspace isolation ----------------------------------------------------


@pytest.mark.integration
async def test_two_workspaces_do_not_see_each_others_carts(service: StoreService) -> None:
    await service.update_cart("ws-1", MUG, 3, _request("1"))
    await service.update_cart("ws-2", NOTEBOOK, 1, _request("2"))

    first = await service.read_state("ws-1")
    second = await service.read_state("ws-2")
    assert set(first.target_state.cart.items) == {"mug"}
    assert set(second.target_state.cart.items) == {"notebook"}


# --- validation -------------------------------------------------------------


@pytest.mark.integration
async def test_an_unseeded_product_is_refused(service: StoreService) -> None:
    """Appendix D.2's schema enumerates the three seeded IDs."""
    with pytest.raises(ProductNotFound) as excinfo:
        await service.update_cart("ws-1", "bicycle-001", 1, _request("1"))
    assert excinfo.value.code == "PRODUCT_NOT_FOUND"


@pytest.mark.integration
@pytest.mark.parametrize("quantity", [-1, 6, 99])
async def test_a_quantity_outside_the_schema_bound_is_refused(
    service: StoreService, quantity: int
) -> None:
    with pytest.raises(ValidationFailed):
        await service.update_cart("ws-1", MUG, quantity, _request("1"))


@pytest.mark.integration
@pytest.mark.parametrize("quantity", [True, "2", 2.0, None])
async def test_a_non_integer_quantity_is_refused(service: StoreService, quantity: object) -> None:
    """`True` is an `int` in Python and would silently become one mug."""
    with pytest.raises(ValidationFailed):
        await service.update_cart("ws-1", MUG, quantity, _request("1"))  # type: ignore[arg-type]


@pytest.mark.integration
@pytest.mark.parametrize("request_id", ["", "short", "x" * 81, None, 12345678])
async def test_a_request_id_outside_the_schema_bound_is_refused(
    service: StoreService, request_id: object
) -> None:
    with pytest.raises(ValidationFailed):
        await service.update_cart("ws-1", MUG, 1, request_id)  # type: ignore[arg-type]


@pytest.mark.integration
async def test_a_refused_request_records_no_idempotency_key(service: StoreService) -> None:
    """A rejected request must not burn a key the caller will legitimately reuse."""
    with pytest.raises(ProductNotFound):
        await service.update_cart("ws-1", "bicycle-001", 1, _request("1"))

    outcome = await service.update_cart("ws-1", MUG, 1, _request("1"))
    assert outcome.replayed is False
    assert outcome.response["cart"]["items"]["mug"]["quantity"] == 1


# --- search (Appendix D.2) --------------------------------------------------


@pytest.mark.integration
async def test_search_is_bounded_by_its_schema(service: StoreService) -> None:
    assert [product.line_key for product in service.search("mug")] == ["mug"]
    with pytest.raises(ValidationFailed):
        service.search("mug", max_results=0)
    with pytest.raises(ValidationFailed):
        service.search("mug", max_results=6)
    with pytest.raises(ValidationFailed):
        service.search("mug", max_results=True)  # type: ignore[arg-type]


# --- concurrency (ADR-0003) -------------------------------------------------


@pytest.mark.integration
async def test_concurrent_mutations_on_one_workspace_all_apply(
    service: StoreService,
) -> None:
    """Admission control queues them; the transaction is what makes it correct."""
    await asyncio.gather(
        *(service.update_cart("ws-1", MUG, 1, _request(str(index))) for index in range(5))
    )
    state = await service.read_state("ws-1")
    assert state.target_state.cart.items["mug"].quantity == 1
    # Only the first call changed anything; the rest were no-ops under new keys.
    assert state.state_version == 2


@pytest.mark.integration
async def test_concurrent_identical_retries_produce_one_mutation(
    service: StoreService,
) -> None:
    """The replay check and the record commit together, so there is one first-result."""
    outcomes = await asyncio.gather(
        *(service.update_cart("ws-1", MUG, 3, _request("1")) for _ in range(4))
    )
    assert sum(1 for outcome in outcomes if not outcome.replayed) == 1
    state = await service.read_state("ws-1")
    assert state.target_state.cart.items["mug"].quantity == 3
    assert state.state_version == 2


@pytest.mark.integration
async def test_idle_locks_are_swept(service: StoreService) -> None:
    """ADR-0003 requires bounded cleanup of the keyed lock map."""
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    await service.update_cart("ws-2", MUG, 1, _request("2"))
    assert service.release_idle_locks() == 2
    assert service.release_idle_locks() == 0
