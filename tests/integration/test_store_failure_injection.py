"""Scenario and fault-injection gates (spec v1.9 §12.2, §13.3; 003-T8).

This is the milestone's exit-gate item: "a direct target API test proves the
discount fault reports success without changing canonical state only in
`pre_fix`". The word doing the work is *only* — a store that misbehaved in both
modes would make the matched pre/post comparison of FR-019 meaningless, and one
that misbehaved in neither would make the whole product a demo of nothing.

So the fault is pinned from three directions: it fires in `pre_fix`, it does not
fire in `post_fix`, and when it fires the *tool's own response says success*
while independent observation of canonical state disagrees. That last pair is
the contradiction the harness exists to detect, and Appendix B's worked example
is transcribed here as the expectation: expected `"20.00"`, actual `"25.00"`, and
an unchanged `state_version` either side of the call.

013-T5 adds the second injector, `undeclared_side_effect`, whose whole point is
the opposite shape: the cart comes out *exactly right* and a path no contract
names moves alongside it. Its section is at the end of this file.

The remaining three profiles are recognised and refused. A store that quietly ran
the honest path while a report claimed a fault was active would be lying about
the one thing it exists to demonstrate, so `FAULT_PROFILE_UNAVAILABLE` is a
distinct code from an unknown value.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from buggy_store.api import API_PREFIX, WORKSPACE_HEADER, create_app
from buggy_store.errors import FaultProfileUnavailable, ValidationFailed
from buggy_store.failure_injection import (
    IMPLEMENTED_PROFILES,
    PROFILE_DESCRIPTIONS,
    UNSAFE_PROFILE_LABEL,
    FaultProfile,
    ScenarioConfiguration,
    ScenarioMode,
)
from buggy_store.repository import StoreRepository
from buggy_store.service import StoreService

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
MUG = "mug-ceramic-001"
DISCOUNT_FAULT = FaultProfile.DISCOUNT_REPORTED_BUT_NOT_APPLIED


@pytest.fixture
async def service(tmp_path: Path) -> StoreService:
    repository = StoreRepository(tmp_path / "store.sqlite3", clock=lambda: EPOCH)
    await repository.initialize()
    return StoreService(repository, clock=lambda: EPOCH)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    database = tmp_path / "api.sqlite3"
    app = create_app(database_path=database)
    async with (
        app.router.lifespan_context(app),
        httpx.ASGITransport(app=app) as transport,
        httpx.AsyncClient(
            transport=transport,
            base_url="http://buggy-store.test",
            headers={WORKSPACE_HEADER: "ws-1"},
        ) as http,
    ):
        yield http


def _request(suffix: str) -> str:
    return f"req-{suffix:>012}"


# --- the vocabulary (FR-011) ------------------------------------------------


@pytest.mark.integration
def test_the_six_profiles_are_the_specs_list_in_its_published_order() -> None:
    assert [profile.value for profile in FaultProfile] == [
        "none",
        "duplicate_on_retry",
        "discount_reported_but_not_applied",
        "checkout_without_confirmation",
        "undeclared_side_effect",
        "tool_surface_poisoned",
    ]


@pytest.mark.integration
def test_there_are_exactly_two_scenario_modes() -> None:
    """FR-017: "a required scenario_mode selector with exactly pre_fix and post_fix"."""
    assert [mode.value for mode in ScenarioMode] == ["pre_fix", "post_fix"]


@pytest.mark.integration
def test_every_profile_is_described_so_a_report_can_label_it() -> None:
    assert set(PROFILE_DESCRIPTIONS) == set(FaultProfile)
    assert all(text.strip() for text in PROFILE_DESCRIPTIONS.values())


@pytest.mark.integration
def test_this_build_implements_the_honest_path_and_three_faults() -> None:
    """Extended, never widened: each injector arrives with its own tests.

    003 shipped the honest path and the discount fault; 013-T5 added
    `undeclared_side_effect`; 012-T1 adds `duplicate_on_retry`. Pinned as set
    equality rather than a count, so a profile that gained an entry here
    without gaining an injector fails.
    """
    assert (
        frozenset(
            {
                FaultProfile.NONE,
                DISCOUNT_FAULT,
                FaultProfile.UNDECLARED_SIDE_EFFECT,
                FaultProfile.DUPLICATE_ON_RETRY,
            }
        )
        == IMPLEMENTED_PROFILES
    )


@pytest.mark.integration
def test_a_fault_is_active_only_in_pre_fix() -> None:
    """FR-011: recorded in both modes, active in one."""
    pre = ScenarioConfiguration(ScenarioMode.PRE_FIX, DISCOUNT_FAULT)
    post = ScenarioConfiguration(ScenarioMode.POST_FIX, DISCOUNT_FAULT)

    assert pre.fault_active is True
    assert post.fault_active is False
    # ...and the profile survives the switch, which is what FR-019's matched
    # comparison pairs the two runs on.
    assert post.fault_profile is DISCOUNT_FAULT


@pytest.mark.integration
def test_the_none_profile_is_never_active_even_in_pre_fix() -> None:
    assert ScenarioConfiguration(ScenarioMode.PRE_FIX, FaultProfile.NONE).fault_active is False


@pytest.mark.integration
def test_an_injected_profile_is_labelled_as_unsafe_wherever_it_is_shown() -> None:
    """FR-011: every non-`none` profile "is labelled as such in the UI and reports"."""
    unsafe = ScenarioConfiguration(ScenarioMode.PRE_FIX, DISCOUNT_FAULT).as_document()
    honest = ScenarioConfiguration(ScenarioMode.POST_FIX, FaultProfile.NONE).as_document()

    assert unsafe["label"] == UNSAFE_PROFILE_LABEL
    assert "label" not in honest


# --- selection (FR-012, FR-017, FR-018) -------------------------------------


@pytest.mark.integration
async def test_a_fresh_workspace_runs_the_honest_path(service: StoreService) -> None:
    """A store nobody configured behaves correctly."""
    scenario = await service.read_scenario("ws-1")
    assert scenario.fault_profile is FaultProfile.NONE
    assert scenario.fault_active is False


@pytest.mark.integration
async def test_a_selection_is_recorded_and_read_back(service: StoreService) -> None:
    await service.select_scenario("ws-1", "pre_fix", DISCOUNT_FAULT)
    scenario = await service.read_scenario("ws-1")
    assert scenario.mode is ScenarioMode.PRE_FIX
    assert scenario.fault_active is True


@pytest.mark.integration
async def test_selecting_a_scenario_reseeds_mutable_state(service: StoreService) -> None:
    """FR-018: a mode switch resets mutable target state.

    Carrying a cart built under one implementation into a run of the other would
    make FR-019's comparison compare two different journeys.
    """
    await service.update_cart("ws-1", MUG, 3, _request("1"))
    await service.select_scenario("ws-1", "pre_fix", DISCOUNT_FAULT)

    state = await service.read_state("ws-1")
    assert state.target_state.cart.items == {}


@pytest.mark.integration
async def test_a_selection_is_scoped_to_its_workspace(service: StoreService) -> None:
    await service.select_scenario("ws-1", "pre_fix", DISCOUNT_FAULT)
    assert (await service.read_scenario("ws-2")).fault_active is False


@pytest.mark.integration
@pytest.mark.parametrize(
    "profile",
    [
        FaultProfile.CHECKOUT_WITHOUT_CONFIRMATION,
        FaultProfile.TOOL_SURFACE_POISONED,
    ],
)
async def test_an_unimplemented_profile_is_refused_rather_than_downgraded(
    service: StoreService, profile: FaultProfile
) -> None:
    """Recognised, described, and refused — never silently treated as `none`."""
    with pytest.raises(FaultProfileUnavailable) as excinfo:
        await service.select_scenario("ws-1", "pre_fix", profile)
    assert excinfo.value.code == "FAULT_PROFILE_UNAVAILABLE"
    assert excinfo.value.details["description"] == PROFILE_DESCRIPTIONS[profile]

    # And nothing was recorded, so the store did not half-apply the request.
    assert (await service.read_scenario("ws-1")).fault_profile is FaultProfile.NONE


@pytest.mark.integration
@pytest.mark.parametrize("value", ["sideways", "PRE_FIX", "", None, 1])
async def test_an_unknown_scenario_mode_is_refused(service: StoreService, value: object) -> None:
    with pytest.raises(ValidationFailed):
        await service.select_scenario("ws-1", value)  # type: ignore[arg-type]


@pytest.mark.integration
async def test_an_unknown_fault_profile_is_refused_as_unknown(
    service: StoreService,
) -> None:
    """Distinct from unavailable: this name is not in the specification at all."""
    with pytest.raises(ValidationFailed):
        await service.select_scenario("ws-1", "pre_fix", "explode_on_tuesdays")


# --- the fault itself (§13.3, App. B) ---------------------------------------


@pytest.mark.integration
async def test_in_pre_fix_the_discount_reports_success_and_changes_nothing(
    service: StoreService,
) -> None:
    """The exit-gate item, and Appendix B's worked example.

    The tool says the total is 20.00; canonical state says 25.00; the version
    does not move. That contradiction is the product's whole subject.
    """
    await service.select_scenario("ws-1", "pre_fix", DISCOUNT_FAULT)
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    before = await service.read_state("ws-1")

    outcome = await service.apply_discount("ws-1", "SAVE20")
    after = await service.read_state("ws-1")

    # What the tool reported.
    assert outcome.response["status"] == "success"
    assert outcome.response["cart"]["total"] == "20.00"

    # What independent observation of canonical state says.
    assert after.target_state.cart.canonical_document()["total"] == "25.00"
    assert after.target_state.cart.discount is None

    # App. B: before_state_version 2, after_state_version 2.
    assert after.state_version == before.state_version


@pytest.mark.integration
async def test_in_post_fix_the_same_call_actually_applies_the_discount(
    service: StoreService,
) -> None:
    """ "...only in `pre_fix`". The corrected implementation is the same build."""
    await service.select_scenario("ws-1", "post_fix", DISCOUNT_FAULT)
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    before = await service.read_state("ws-1")

    outcome = await service.apply_discount("ws-1", "SAVE20")
    after = await service.read_state("ws-1")

    assert outcome.response["status"] == "success"
    assert after.target_state.cart.canonical_document()["total"] == "20.00"
    assert after.target_state.cart.discount is not None
    assert after.state_version == before.state_version + 1


@pytest.mark.integration
async def test_the_comparison_fault_stays_recorded_in_post_fix(
    service: StoreService,
) -> None:
    """FR-019 pairs the two runs on every controlled input but the mode."""
    await service.select_scenario("ws-1", "post_fix", DISCOUNT_FAULT)
    scenario = await service.read_scenario("ws-1")
    assert scenario.fault_profile is DISCOUNT_FAULT
    assert scenario.fault_active is False


@pytest.mark.integration
async def test_the_fault_touches_only_the_discount_tool(service: StoreService) -> None:
    """§13.3 scopes it to `apply_discount`; a broader defect would confuse the finding."""
    await service.select_scenario("ws-1", "pre_fix", DISCOUNT_FAULT)

    cart = await service.update_cart("ws-1", MUG, 2, _request("1"))
    assert cart.response["cart"]["items"]["mug"]["quantity"] == 2
    assert (await service.read_state("ws-1")).target_state.cart.item_count == 1

    confirmation = await service.request_confirmation("ws-1")
    await service.decide_confirmation("ws-1", confirmation.confirmation_id, approved=True)
    ordered = await service.checkout(
        "ws-1", confirmation_id=confirmation.confirmation_id, request_id=_request("9")
    )
    assert ordered.state.target_state.order.created is True


@pytest.mark.integration
async def test_the_faulty_response_stays_syntactically_valid(
    service: StoreService,
) -> None:
    """§13.3: the response is *apparently* successful, not malformed.

    A malformed response would be caught by the execution layer, and the point
    is that nothing short of independent observation catches this one.
    """
    await service.select_scenario("ws-1", "pre_fix", DISCOUNT_FAULT)
    await service.update_cart("ws-1", MUG, 1, _request("1"))
    response = (await service.apply_discount("ws-1", "SAVE20")).response

    assert set(response) == {"status", "state_version", "cart"}
    assert set(response["cart"]) == {"items", "discount", "subtotal", "total"}
    assert response["cart"]["discount"] == {"code": "SAVE20", "amount": "5.00"}


@pytest.mark.integration
async def test_the_fault_is_deterministic_across_repeats(service: StoreService) -> None:
    """A flaky injector would make a regression case irreproducible."""
    await service.select_scenario("ws-1", "pre_fix", DISCOUNT_FAULT)
    await service.update_cart("ws-1", MUG, 1, _request("1"))

    first = (await service.apply_discount("ws-1", "SAVE20")).response
    second = (await service.apply_discount("ws-1", "SAVE20")).response
    assert first == second
    assert (await service.read_state("ws-1")).target_state.cart.discount is None


# --- through the versioned API (the exit gate's "direct target API test") ---


@pytest.mark.integration
async def test_the_fault_is_reachable_and_visible_over_http(
    client: httpx.AsyncClient,
) -> None:
    """Exit gate: "a direct target API test proves the discount fault..."."""
    selected = await client.post(
        f"{API_PREFIX}/store/scenario",
        json={"scenario_mode": "pre_fix", "fault_profile": DISCOUNT_FAULT.value},
    )
    assert selected.status_code == 200
    assert selected.json()["fault_active"] is True
    assert selected.json()["label"] == UNSAFE_PROFILE_LABEL

    await client.post(
        f"{API_PREFIX}/store/cart/mutations",
        json={"product_id": MUG, "quantity": 1, "request_id": _request("1")},
    )
    reported = await client.post(f"{API_PREFIX}/store/discount", json={"code": "SAVE20"})
    observed = await client.get(f"{API_PREFIX}/store/cart")

    assert reported.status_code == 200
    assert reported.json()["cart"]["total"] == "20.00"
    assert observed.json()["cart"]["total"] == "25.00"


@pytest.mark.integration
async def test_the_same_http_journey_is_correct_in_post_fix(
    client: httpx.AsyncClient,
) -> None:
    await client.post(
        f"{API_PREFIX}/store/scenario",
        json={"scenario_mode": "post_fix", "fault_profile": DISCOUNT_FAULT.value},
    )
    await client.post(
        f"{API_PREFIX}/store/cart/mutations",
        json={"product_id": MUG, "quantity": 1, "request_id": _request("1")},
    )
    reported = await client.post(f"{API_PREFIX}/store/discount", json={"code": "SAVE20"})
    observed = await client.get(f"{API_PREFIX}/store/cart")

    assert reported.json()["cart"]["total"] == "20.00"
    assert observed.json()["cart"]["total"] == "20.00"


@pytest.mark.integration
async def test_the_scenario_endpoint_publishes_what_the_panel_needs(
    client: httpx.AsyncClient,
) -> None:
    """FR-017: explanation, active fault behaviour, and supported modes."""
    body = (await client.get(f"{API_PREFIX}/store/scenario")).json()
    assert body["supported_scenario_modes"] == ["pre_fix", "post_fix"]
    assert body["recognized_fault_profiles"] == [profile.value for profile in FaultProfile]
    assert body["implemented_fault_profiles"] == [
        "discount_reported_but_not_applied",
        "duplicate_on_retry",
        "none",
        "undeclared_side_effect",
    ]
    assert body["description"] == PROFILE_DESCRIPTIONS[FaultProfile.NONE]


@pytest.mark.integration
async def test_an_unimplemented_profile_is_refused_over_http(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        f"{API_PREFIX}/store/scenario",
        json={"scenario_mode": "pre_fix", "fault_profile": "checkout_without_confirmation"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "FAULT_PROFILE_UNAVAILABLE"
    assert response.json()["error"]["details"]["description"]


# --- undeclared_side_effect (§13.3; 013-T5) ---------------------------------
#
# §13.3 fixes the shape of this profile precisely: "a cart mutation additionally
# rewrites a state path no contract term mentions... Every declared assertion
# still passes; only `no_undeclared_changes` fails."
#
# Both halves have to hold, and the first is the one that is easy to lose. An
# injector that also got the cart wrong would fail an ordinary assertion, the run
# would go red for the usual reason, and the demonstration — that a contract can
# be green everywhere it looks and still be wrong — would be gone.


@pytest.mark.integration
async def test_the_side_effect_leaves_the_cart_exactly_correct(service: StoreService) -> None:
    """The half that makes the demonstration mean anything."""
    await service.select_scenario("ws-1", "pre_fix", FaultProfile.UNDECLARED_SIDE_EFFECT)

    outcome = await service.update_cart("ws-1", MUG, 1, "req_addonemug")

    cart = outcome.state.target_state.cart
    assert outcome.response["status"] == "success"
    assert cart.items["mug"].quantity == 1
    assert str(cart.total) == "25.00"
    assert len(cart.items) == 1


@pytest.mark.integration
async def test_the_side_effect_rewrites_a_path_no_cart_contract_names(
    service: StoreService,
) -> None:
    """§13.2 carries `preferences` precisely so this is observable."""
    await service.select_scenario("ws-1", "pre_fix", FaultProfile.UNDECLARED_SIDE_EFFECT)
    before = await service.read_state("ws-1")
    assert before.target_state.preferences.delivery_note == ""

    outcome = await service.update_cart("ws-1", MUG, 1, "req_addonemug")

    assert outcome.state.target_state.preferences.delivery_note == "leave with the neighbour"


@pytest.mark.integration
async def test_the_side_effect_does_not_fire_in_post_fix(service: StoreService) -> None:
    """FR-011: recorded in both modes, active in one.

    Without this the matched pre/post comparison of FR-019 would be meaningless —
    a store that misbehaved in both modes proves nothing about either.
    """
    await service.select_scenario("ws-1", "post_fix", FaultProfile.UNDECLARED_SIDE_EFFECT)

    outcome = await service.update_cart("ws-1", MUG, 1, "req_addonemug")

    assert outcome.state.target_state.cart.items["mug"].quantity == 1
    assert outcome.state.target_state.preferences.delivery_note == ""


@pytest.mark.integration
async def test_one_mutation_moves_the_state_version_exactly_once(
    service: StoreService,
) -> None:
    """The side effect rides along; it is not a second write.

    §13.2's counter is evidence the harness reads, and FR-032 treats a version
    change as proof that state moved. A profile that bumped it twice for one call
    would inject a defect this demo did not mean to inject, and the idempotency
    policy would be looking at the wrong number.
    """
    await service.select_scenario("ws-1", "pre_fix", FaultProfile.UNDECLARED_SIDE_EFFECT)
    before = await service.read_state("ws-1")

    outcome = await service.update_cart("ws-1", MUG, 1, "req_addonemug")

    assert outcome.state.state_version == before.state_version + 1


@pytest.mark.integration
async def test_the_side_effect_is_deterministic_across_runs(tmp_path: Path) -> None:
    """§24 compares a replayed run against a recorded one by canonical document.

    A generated note — a timestamp, a request ID — would differ between two
    otherwise identical runs and make that comparison fail for a reason that has
    nothing to do with the defect.
    """
    notes = []
    for index in range(2):
        repository = StoreRepository(tmp_path / f"store-{index}.sqlite3", clock=lambda: EPOCH)
        await repository.initialize()
        store = StoreService(repository, clock=lambda: EPOCH)
        await store.select_scenario("ws-1", "pre_fix", FaultProfile.UNDECLARED_SIDE_EFFECT)
        outcome = await store.update_cart("ws-1", MUG, 1, "req_addonemug")
        notes.append(outcome.state.target_state.canonical_document())

    assert notes[0] == notes[1]


@pytest.mark.integration
async def test_a_retried_mutation_does_not_reapply_the_side_effect(
    service: StoreService,
) -> None:
    """Appendix D.2's replay path returns the first persisted result.

    The injector lives inside that path, so a retry must not write again — the
    idempotency guarantee covers the whole mutation, side effect included.
    """
    await service.select_scenario("ws-1", "pre_fix", FaultProfile.UNDECLARED_SIDE_EFFECT)
    first = await service.update_cart("ws-1", MUG, 1, "req_addonemug")
    repeat = await service.update_cart("ws-1", MUG, 1, "req_addonemug")

    assert repeat.replayed is True
    assert repeat.state.state_version == first.state.state_version
