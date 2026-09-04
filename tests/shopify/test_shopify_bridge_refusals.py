"""Every way a Shopify trial is refused, one test each (011-T10, FR-114, FR-116).

FR-114's sentence is the organising idea: "A missing final observation,
cross-origin checkout navigation, or unexpected variant is a failed or
incomplete trial, **never a pass**." Two different outcomes live under that
sentence and the difference matters, so the tests keep them apart.

* A **refusal** captures nothing and leaves the pairing where it was. That is
  the answer whenever the harness can tell, before looking at any payload, that
  this submission is outside the cart-only scope — a credential that expired, an
  origin nobody configured, a path pointing at checkout. Nothing is recorded as
  having passed *or* failed, because nothing was observed.
* A **failed verdict** is the answer when the observation was real and the cart
  was wrong. That is evidence, and deleting it by refusing would delete the
  finding the product exists to produce.

Each test asserts which of the two happened, and the ones that refuse assert
that the trial's state did not move.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX

#: The `trial` fixture's driver (`tests/shopify/conftest.py`). Annotated loosely
#: because `tests/` is not a package and importing a sibling conftest by name
#: would depend on whichever directory pytest happened to put on the path.
Trial = Any

pytestmark = [pytest.mark.shopify, pytest.mark.integration]

#: FR-111's lifetime, in seconds, plus one. Advanced on the injected clock, never
#: slept: a test that waited fifteen minutes is a test nobody runs.
PAST_EXPIRY_SECONDS = 15 * 60 + 1


# --- FR-114's forbidden scope -------------------------------------------------


async def test_a_contract_that_drives_checkout_is_refused_before_a_credential_exists(
    demo_ui: httpx.AsyncClient,
) -> None:
    """FR-114: "`proceed_to_checkout`, order creation, customer login, and
    payment are forbidden for this contract."

    Refused at *pairing* time, which is the earliest moment the answer is
    knowable and the last moment before a credential exists. §15.9 names the
    code: an attempt to arm an external contract naming a forbidden operation is
    refused with `EXTERNAL_TARGET_FORBIDDEN_OPERATION`.

    The offending contract is a real seeded template rather than a handcrafted
    document — the demo pack's `one_mug_save20_no_checkout` carries a
    `requires_confirmation` policy on `proceed_to_checkout` — so the check is
    exercised against a contract the product actually ships.
    """
    # Arrange
    templates = (await demo_ui.get(f"{API_PREFIX}/contracts/templates")).json()["templates"]
    checkout_contract = next(
        template
        for template in templates
        if template["source_template_id"] == "one_mug_save20_no_checkout"
    )

    # Act
    refused = await demo_ui.post(
        f"{API_PREFIX}/shopify/pairings", json={"contract_id": checkout_contract["contract_id"]}
    )

    # Assert
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "EXTERNAL_TARGET_FORBIDDEN_OPERATION"
    assert "proceed_to_checkout" in refused.text


@pytest.mark.parametrize(
    ("capture_path", "why"),
    [
        ("/checkout/cart.js", "checkout navigation"),
        ("/orders/12345/cart.js", "order scope"),
        ("/account/payment/cart.js", "payment scope"),
        ("/account/login/cart.js", "customer login"),
        ("/cart.js?token=secret", "query string"),
        ("/cart.js#secret", "fragment"),
        ("cart.js", "relative path"),
    ],
)
async def test_a_cart_read_from_outside_the_cart_scope_captures_nothing(
    trial: Trial, capture_path: str, why: str
) -> None:
    """FR-112 and FR-114, at the observation boundary.

    The bridge reports which path it read, and a bridge that followed a checkout
    link reports a checkout path. Refusing before the payload is parsed is the
    conservative direction: the trial captures nothing and the pairing keeps its
    state, so nothing is recorded as having passed. The payload below is a
    perfectly valid empty cart, which is the point — the refusal is about where
    it was read, not about what it said.
    """
    # Arrange
    pairing_id, session = await trial.paired()

    # Act
    refused = await trial.before(pairing_id, session, trial.cart(), capture_path=capture_path)

    # Assert
    assert refused.status_code == 400, f"{why}: {refused.text}"
    assert refused.json()["error"]["code"] == "EXTERNAL_TARGET_FORBIDDEN_OPERATION"
    read = (await trial.status(pairing_id)).json()["pairing"]
    assert read["status"] == "paired", f"{why} moved the pairing"
    assert read["run_id"] is None, f"{why} armed a run"


# --- a real observation of a wrong cart is a verdict, not a refusal ------------


async def test_an_unexpected_variant_fails_the_trial_rather_than_being_refused(
    trial: Trial,
) -> None:
    """FR-114: an "unexpected variant is a failed or incomplete trial, never a pass".

    A *failure*, deliberately, and not a refusal. The observation was real and
    the cart genuinely held the wrong thing, which is precisely the finding this
    product exists to produce; refusing it would delete the evidence. The
    projection keys the configured variant as `test_variant` and every other
    variant by its own id, which is what makes a wrong item visible to the
    contract rather than silently counted as the right one.
    """
    # Arrange
    pairing_id, session = await trial.armed()

    # Act
    verified = await trial.verify(pairing_id, session, trial.cart(variant="999"))

    # Assert
    assert verified.status_code == 200, verified.text
    assert verified.json()["verdict"] == "failed"
    assert (await trial.status(pairing_id)).json()["pairing"]["status"] == "failed"


async def test_an_empty_final_cart_fails_the_trial_rather_than_crashing(
    trial: Trial,
) -> None:
    """A missing expected line item is a witnessed failure, not a server error."""
    # Arrange
    pairing_id, session = await trial.armed()

    # Act
    verified = await trial.verify(pairing_id, session, trial.cart())

    # Assert
    assert verified.status_code == 200, verified.text
    assert verified.headers["access-control-allow-origin"] == trial.STORE
    assert verified.json()["verdict"] == "failed"
    assert (await trial.status(pairing_id)).json()["pairing"]["status"] == "failed"


async def test_observed_checkout_navigation_fails_the_trial(trial: Trial) -> None:
    """FR-114's other half, carried by the one fact the bridge can witness.

    The cart itself is exactly right here: one configured variant, quantity one,
    consistent totals. The only thing wrong is that the shopper went to
    checkout, and the contract asserts on
    `target.page.checkout_navigation_observed` because that is the term the
    bridge can actually observe. A pass here would be the harness certifying a
    journey that did the forbidden thing.
    """
    # Arrange
    pairing_id, session = await trial.armed()

    # Act
    verified = await trial.verify(
        pairing_id, session, trial.cart(variant=trial.VARIANT, checkout_navigated=True)
    )

    # Assert
    assert verified.status_code == 200, verified.text
    assert verified.json()["verdict"] == "failed"


async def test_a_trial_that_does_not_start_empty_is_refused(trial: Trial) -> None:
    """FR-116: "The initial observation must satisfy the empty-cart precondition."

    A trial that began with the variant already in the cart is not a trial of
    adding it, and arming it anyway would produce a report whose baseline nobody
    chose — one in which an agent that did nothing at all passes.
    """
    # Arrange
    pairing_id, session = await trial.paired()

    # Act
    refused = await trial.before(pairing_id, session, trial.cart(variant=trial.VARIANT))

    # Assert
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "PRECONDITION_FAILED"
    assert (await trial.status(pairing_id)).json()["pairing"]["run_id"] is None


# --- §15.7's idempotency boundary ---------------------------------------------


async def test_a_second_differing_cart_for_one_phase_is_refused(trial: Trial) -> None:
    """§15.7: "a different second payload for the same phase returns
    `409 OBSERVATION_ALREADY_CAPTURED`".

    The contrast with idempotency is the whole rule: an identical resubmission
    is a lost response and gets the first result back, while a *differing* one
    describes another moment entirely. Capturing over the top of the first would
    silently move a trial's baseline after the fact.
    """
    # Arrange
    pairing_id, session = await trial.paired()
    first = await trial.before(pairing_id, session, trial.cart())
    assert first.status_code == 201, first.text

    # Act - an empty cart is still required here, so the difference is the
    # currency: a real second read of a differently-configured session.
    refused = await trial.before(pairing_id, session, trial.cart(currency="GBP"))

    # Assert
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "OBSERVATION_ALREADY_CAPTURED"
    assert (await trial.status(pairing_id)).json()["pairing"]["run_id"] == first.json()["run_id"]


async def test_a_second_differing_final_cart_is_refused(trial: Trial) -> None:
    """The same rule on the `after` phase, where the verdict is already sealed.

    A bridge that retried `verify` with a cart that had moved on since would
    otherwise ask the harness to re-judge a finished trial — and §16.5 makes
    every verdict state terminal precisely so nothing can.
    """
    # Arrange
    pairing_id, session = await trial.armed()
    first = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))
    assert first.status_code == 200, first.text

    # Act
    refused = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT, quantity=2))

    # Assert
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "OBSERVATION_ALREADY_CAPTURED"
    assert (await trial.status(pairing_id)).json()["pairing"]["status"] == "passed"


# --- FR-111's credentials -----------------------------------------------------


async def test_an_expired_credential_captures_nothing(trial: Trial, frozen_clock: Any) -> None:
    """FR-111: "Expired ... pairings fail closed without capturing an observation."

    Aged on the injected clock rather than by waiting, which is also what proves
    the expiry decision reads that clock: a check written against
    `datetime.now()` would not notice this test at all and the pairing would
    still redeem.

    The pairing moves to `expired`, which §16.5 makes a terminal state of its own
    rather than a flavour of anything — "expiry never converts an incomplete
    Shopify trial into a pass", and a reader has to be able to see that a trial
    ran out of time rather than infer it from an absent verdict.
    """
    # Arrange
    created = await trial.create()
    pairing_id = created.json()["pairing_id"]
    credential = trial.credential_in(created.json()["launch_url"])

    # Act
    frozen_clock.advance(PAST_EXPIRY_SECONDS)
    refused = await trial.redeem(pairing_id, credential)

    # Assert
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "CONFIRMATION_EXPIRED"
    read = (await trial.status(pairing_id)).json()["pairing"]
    assert read["status"] == "expired"
    assert read["run_id"] is None
    assert read["redeemed_at"] is None


async def test_a_credential_is_redeemed_once_and_never_again(trial: Trial) -> None:
    """FR-111: "redeemed once, and thereafter represented by a bounded
    session-scoped credential."

    The second redemption must not hand out a second session. Two live bridge
    sessions on one pairing would mean two storefront tabs able to submit carts
    into the same trial, and §15.7's idempotency is keyed by phase and content
    hash — it has no way to say which of two differing tabs was the shopper.
    """
    # Arrange
    created = await trial.create()
    pairing_id = created.json()["pairing_id"]
    credential = trial.credential_in(created.json()["launch_url"])
    first = await trial.redeem(pairing_id, credential)
    assert first.status_code == 200, first.text

    # Act
    reused = await trial.redeem(pairing_id, credential)

    # Assert - §16's rule for the run machine, applied to the pairing machine:
    # "invalid non-reset state transitions shall return HTTP 409". A pairing
    # already in `paired` has no move to `paired`, and that gate answers before
    # the digest comparison does. What matters either way is the last two lines:
    # no second session was minted and the first one still stands.
    assert reused.status_code == 409, reused.text
    assert reused.json()["error"]["code"] == "RUN_IN_PROGRESS"
    assert "bridge_session_credential" not in reused.text
    assert (await trial.status(pairing_id)).json()["pairing"]["status"] == "paired"

    # Assert - and the spent one-time credential is not a bridge session either.
    # The two digests are stored in different columns over different secrets, so
    # a caller holding the launch URL can never skip redemption and submit a cart.
    borrowed = await trial.before(pairing_id, credential, trial.cart())
    assert borrowed.status_code == 403, borrowed.text
    assert borrowed.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"


async def test_a_credential_from_another_pairing_unlocks_nothing(trial: Trial) -> None:
    """FR-111: "cross-workspace ... pairings fail closed".

    The stored digest covers the workspace, the contract, and the store origin as
    well as the secret, so a credential minted for one pairing cannot validate
    against another even when the caller chooses which row it is checked against.
    That is a property of the comparison rather than of a `WHERE` clause somebody
    has to remember to write, and this is the test that says so.
    """
    # Arrange - two workspaces, because §17.1 allows only one live pairing each.
    mine = await trial.create()
    assert mine.status_code == 201, mine.text
    stranger_credential = trial.credential_in(mine.json()["launch_url"])
    await trial.ui.post(f"{API_PREFIX}/workspace/reset")
    theirs = await trial.create()
    assert theirs.status_code == 201, theirs.text

    # Act - the first pairing's credential, presented against the second.
    refused = await trial.redeem(theirs.json()["pairing_id"], stranger_credential)

    # Assert
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"


# --- FR-110's exact origin ----------------------------------------------------


async def test_a_bridge_request_from_an_unconfigured_origin_is_refused(trial: Trial) -> None:
    """FR-110 and §20.1: exact equality, and "reject ... unconfigured origins".

    Refused twice over, and both are asserted because the redundancy is the
    design. The origin middleware refuses a mutation whose `Origin` is neither
    the harness nor the one scoped store, and the pairing service compares the
    header against the configured store again before it will look at a
    credential. Either lock alone would be enough; the point is that removing
    one does not open the door.
    """
    # Arrange
    created = await trial.create()
    pairing_id = created.json()["pairing_id"]
    credential = trial.credential_in(created.json()["launch_url"])

    # Act
    refused = await trial.redeem(pairing_id, credential, origin=trial.STRANGER)

    # Assert
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "ORIGIN_NOT_ALLOWED"
    assert (await trial.status(pairing_id)).json()["pairing"]["status"] == "created"


async def test_a_bridge_request_with_no_origin_at_all_is_refused(trial: Trial) -> None:
    """The header's absence is not a pass.

    A missing `Origin` is *allowed* by the harness-wide policy, deliberately: a
    CLI or an agent has no ambient cookie authority and refusing it would break
    the documented client without closing anything. The bridge routes are the
    exception, and they have to be: a bridge runs in a browser, a browser always
    sends `Origin` on a cross-origin `POST`, so a bridge request without one did
    not come from the storefront page and FR-110 gives it nothing.
    """
    # Arrange
    created = await trial.create()
    pairing_id = created.json()["pairing_id"]
    credential = trial.credential_in(created.json()["launch_url"])

    # Act
    refused = await trial.redeem(pairing_id, credential, origin=None)

    # Assert
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "EXTERNAL_TARGET_FORBIDDEN_OPERATION"
    assert (await trial.status(pairing_id)).json()["pairing"]["status"] == "created"


async def test_a_bridge_request_with_no_credential_is_refused(trial: Trial) -> None:
    """§15.7: "every bridge request must carry the short-lived bearer credential".

    The refusal is the same one a wrong credential gets, on purpose. A caller who
    could tell "you sent no header" from "that pairing is finished" could
    enumerate pairings one guess at a time.
    """
    # Arrange
    pairing_id, _session = await trial.paired()

    # Act
    refused = await trial.before(pairing_id, None, trial.cart())

    # Assert
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"
    assert (await trial.status(pairing_id)).json()["pairing"]["run_id"] is None


# --- untrusted input at the boundary ------------------------------------------


async def test_an_oversized_submission_is_refused_before_it_is_parsed(trial: Trial) -> None:
    """FR-112: "accept only JSON responses up to 256 KiB".

    A `cart.js` payload is the one part of a bridge request whose size a
    storefront rather than the operator controls, so the bound sits in front of
    the JSON parser rather than behind it as a validator. The body below is
    deliberately *valid* JSON of a valid shape — if the cap were enforced after
    parsing, this would be accepted.
    """
    # Arrange
    pairing_id, session = await trial.paired()
    enormous = trial.cart()
    enormous["note"] = "a" * (256 * 1024)

    # Act
    refused = await trial.before(pairing_id, session, enormous)

    # Assert
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"
    assert (await trial.status(pairing_id)).json()["pairing"]["run_id"] is None


async def test_a_tool_result_wearing_an_observation_costume_is_refused(trial: Trial) -> None:
    """Constitution §5: "A tool's self-report is evidence, never proof."

    This is the substitution the whole product exists to refuse, attempted at the
    one door that accepts JSON from a browser. A bridge that submitted what
    Shopify's own `update_cart` tool *said* — rather than what `/cart.js`
    returned — would make the verdict agree with the channel under test.
    """
    # Arrange
    pairing_id, session = await trial.paired()
    self_report = {**trial.cart(), "status": "success", "content": "Added to cart"}

    # Act
    refused = await trial.before(pairing_id, session, self_report)

    # Assert
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"
    assert (await trial.status(pairing_id)).json()["pairing"]["run_id"] is None
    # The refusal names no value from the payload: it came from a storefront,
    # and echoing it back would put untrusted text into a response.
    assert "Added to cart" not in refused.text


async def test_a_refusal_still_carries_the_cors_header_the_bridge_needs(trial: Trial) -> None:
    """A refusal a browser will not let the bridge read is a network error.

    Every fail-closed answer in this file has to be distinguishable from every
    other by the code running inside the storefront page — otherwise "your
    credential expired" and "the harness is down" look identical, and the
    operator is told the wrong thing. `create_app`'s exception handler cannot add
    these headers because it does not know which routes are cross-origin, which
    is why the bridge handlers catch and re-emit.
    """
    # Arrange
    pairing_id, session = await trial.paired()

    # Act
    refused = await trial.before(pairing_id, session, trial.cart(), capture_path="/checkout")

    # Assert
    assert refused.status_code == 400, refused.text
    assert refused.headers["access-control-allow-origin"] == trial.STORE
    assert "Origin" in refused.headers["vary"]
    assert "access-control-allow-credentials" not in refused.headers


async def test_a_rate_limit_refusal_still_carries_the_storefront_cors_header(
    trial: Trial,
) -> None:
    """A middleware 429 must remain readable inside the configured storefront."""
    # Arrange
    created = await trial.create()
    pairing_id = created.json()["pairing_id"]
    credential = trial.credential_in(created.json()["launch_url"])
    for _ in range(100):
        exhausted = await trial.ui.get("/api/v1/workspace")
        if exhausted.status_code == 429:
            break
    else:  # pragma: no cover - protects the test if the public limit changes
        raise AssertionError("the request bucket did not exhaust within the test bound")

    # Act
    refused = await trial.redeem(pairing_id, credential)

    # Assert
    assert refused.status_code == 429, refused.text
    assert refused.headers["access-control-allow-origin"] == trial.STORE
    assert "Origin" in refused.headers["vary"]
    assert int(refused.headers["retry-after"]) >= 1


async def test_an_unknown_field_on_a_bridge_body_is_refused(trial: Trial) -> None:
    """Constitution §5: boundary models "normally forbid unknown fields".

    The practical reason is the one `routes/workspace.py` states about reset: a
    key silently ignored is a caller who believes it took effect. A bridge that
    sent `{"cart": ..., "provenance": "platform_session_api"}` and was answered
    `201` would reasonably conclude it had labelled its own observation — which
    is exactly the label the harness refuses to take from a caller.
    """
    # Arrange
    pairing_id, session = await trial.paired()

    # Act
    refused = await trial.bridge.post(
        f"{trial.PAIRINGS}/{pairing_id}/observations/before",
        json={
            "capture_path": "/cart.js",
            "cart": trial.cart(),
            "provenance": "platform_session_api",
        },
        headers={"Origin": trial.STORE, "Authorization": f"Bearer {session}"},
    )

    # Assert
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"
    assert (await trial.status(pairing_id)).json()["pairing"]["run_id"] is None


async def test_creating_a_pairing_takes_no_body_at_all(trial: Trial) -> None:
    """The common case is the one with nothing to say.

    The contract is server-expanded from configuration, so the UI has no field to
    fill in — and a route that *required* a body would push a client into sending
    `{}` it does not understand. Asserted because the default is easy to lose in
    a later signature change.
    """
    # Act
    created = await trial.ui.post(trial.PAIRINGS)

    # Assert
    assert created.status_code == 201, created.text
    assert created.json()["store_origin"] == trial.STORE
