"""§15.7's five endpoints, driven end to end (AC-18, 011-T10).

The service underneath these routes was written and never exposed. These tests
are deliberately **HTTP-only**: none of them imports an application module or
constructs a service, because the thing that was missing was not the logic but
the door, and a test that reaches past the door cannot notice a missing one.

What each file covers is split by shape rather than by requirement.
`test_shopify_bridge_refusals.py` holds every way a trial is refused; this file
holds the trial that works, the properties that must hold while it does, and the
two containment promises AC-18 makes about what a passing trial may not leak.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app

#: The `trial` fixture's driver (`tests/shopify/conftest.py`). Annotated
#: loosely on purpose: `tests/` is not a package, so importing the conftest by
#: name would depend on whichever directory pytest happened to put on the path.
Trial = Any

pytestmark = [pytest.mark.shopify, pytest.mark.integration]

RUNS = f"{API_PREFIX}/runs"


async def test_the_bridge_carries_one_trial_from_pairing_to_a_verdict(trial: Trial) -> None:
    """AC-18's journey, through the API a bridge and a UI actually have.

    One assertion per step rather than a single end-state check: a trial that
    reached `passed` through the wrong intermediate states would be a trial
    nobody could replay, and §16.5's machine is the part a later change is most
    likely to shortcut.
    """
    # Arrange / Act - create, as the UI.
    created = await trial.create()

    # Assert - FR-111's credential, delivered only in the fragment.
    assert created.status_code == 201, created.text
    minted = created.json()
    assert created.headers["cache-control"] == "no-store"
    assert minted["store_origin"] == trial.STORE
    assert minted["launch_url"].startswith(f"{trial.STORE}/#actionwitness={minted['pairing_id']}.")

    # Act - redeem, as the bridge.
    pairing_id = minted["pairing_id"]
    redeemed = await trial.redeem(pairing_id, trial.credential_in(minted["launch_url"]))

    # Assert - §20.1's CORS answer, and no credentialed-cookie CORS at all.
    assert redeemed.status_code == 200, redeemed.text
    assert redeemed.headers["cache-control"] == "no-store"
    assert redeemed.headers["access-control-allow-origin"] == trial.STORE
    assert "Origin" in redeemed.headers["vary"]
    assert "access-control-allow-credentials" not in redeemed.headers
    assert redeemed.json()["pairing"]["status"] == "paired"
    session = redeemed.json()["bridge_session_credential"]

    # Act - the empty-cart baseline. Only this may create the run (§16.5).
    captured = await trial.before(pairing_id, session, trial.cart())

    # Assert
    assert captured.status_code == 201, captured.text
    assert captured.json()["status"] == "armed"
    assert captured.json()["replayed"] is False
    run_id = captured.json()["run_id"]
    assert run_id is not None

    # Act - the final cart, with exactly one configured variant in it.
    verified = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))

    # Assert - FR-115's compact verdict, and the same run.
    assert verified.status_code == 200, verified.text
    assert verified.json() == {
        "pairing_id": pairing_id,
        "run_id": run_id,
        "verdict": "passed",
        "content_hash": verified.json()["content_hash"],
        "replayed": False,
    }

    # Assert - and the UI sees the same trial through its own cookie.
    read = await trial.status(pairing_id)
    assert read.status_code == 200, read.text
    assert read.json()["pairing"]["status"] == "passed"
    assert read.json()["pairing"]["run_id"] == run_id
    assert read.json()["pairing"]["bridge_version"] == "1.0.0"
    assert read.json()["pairing"]["theme_build_id"] == "theme-build-7"


async def test_the_status_endpoint_offers_a_report_link_that_resolves(trial: Trial) -> None:
    """§15.7 asks the status endpoint for a "report link".

    The path is written out in `routes/shopify.py` because `API_PREFIX` cannot be
    imported from `api/app.py` without a cycle, so it is fetched here rather than
    string-compared: a duplicated constant that nobody exercises is a link that
    rots the first time a prefix moves.
    """
    # Arrange
    pairing_id, session = await trial.armed()
    await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))

    # Act
    link = (await trial.status(pairing_id)).json()["report_path"]
    served = await trial.ui.get(link)

    # Assert
    assert served.status_code == 200, served.text
    assert served.json()["status"] == "passed"


async def test_no_report_link_is_offered_before_a_verdict_exists(trial: Trial) -> None:
    """A link to a document nothing has written is worse than no link."""
    # Arrange / Act
    pairing_id, _session = await trial.armed()

    # Assert
    body = (await trial.status(pairing_id)).json()
    assert body["pairing"]["status"] == "armed"
    assert body["report_path"] is None


async def test_a_repeated_initial_capture_returns_the_first_result(trial: Trial) -> None:
    """§15.7: "a repeat with the same hash returns the existing result".

    The second call must not create a second run, and `replayed` is what makes
    that legible to a bridge whose response was lost in flight.
    """
    # Arrange
    pairing_id, session = await trial.paired()
    first = await trial.before(pairing_id, session, trial.cart())
    assert first.status_code == 201, first.text

    # Act
    again = await trial.before(pairing_id, session, trial.cart())

    # Assert
    assert again.status_code == 201, again.text
    assert again.json()["replayed"] is True
    assert again.json()["run_id"] == first.json()["run_id"]
    assert again.json()["content_hash"] == first.json()["content_hash"]


async def test_a_repeated_verification_returns_the_recorded_verdict(trial: Trial) -> None:
    """The same idempotency on the `after` phase, and no second verdict.

    A retried `verify` is the realistic case, not a contrived one: FR-115's tool
    runs in a storefront tab, and an ambiguous timeout there is exactly when the
    project rules say to re-observe rather than re-mutate.
    """
    # Arrange
    pairing_id, session = await trial.armed()
    final = trial.cart(variant=trial.VARIANT)
    first = await trial.verify(pairing_id, session, final)
    assert first.status_code == 200, first.text

    # Act
    again = await trial.verify(pairing_id, session, final)

    # Assert
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert again.json()["verdict"] == first.json()["verdict"] == "passed"
    assert again.json()["content_hash"] == first.json()["content_hash"]


async def test_no_raw_credential_reaches_a_log_line_or_any_later_response(
    trial: Trial, caplog: pytest.LogCaptureFixture
) -> None:
    """FR-111 and §15.7's containment promise, asserted rather than assumed.

    "The raw one-time and bridge-session credentials are never logged,
    persisted, returned by the status endpoint, or included in an artifact."

    Checked against the emitted records and the actual response bodies rather
    than against the implementation, because that is the property: a future
    field on `PairingView`, a future log key, or a well-meaning debug line would
    each break the promise without touching anything this test names. The two
    responses that *mint* a credential are the two the specification exempts,
    and each is `no-store` for that reason.
    """
    # Arrange
    caplog.set_level(logging.DEBUG)

    # Act - the whole trial, so every response this feature can produce is seen.
    created = await trial.create()
    pairing_id = created.json()["pairing_id"]
    one_time = trial.credential_in(created.json()["launch_url"])
    redeemed = await trial.redeem(pairing_id, one_time)
    session = redeemed.json()["bridge_session_credential"]
    captured = await trial.before(pairing_id, session, trial.cart())
    verified = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))
    status = await trial.status(pairing_id)
    report = await trial.ui.get(status.json()["report_path"])

    # Assert - the guard on everything below: two real, distinct secrets.
    assert one_time != session
    assert min(len(one_time), len(session)) >= 40

    # Assert - only the minting responses carry one.
    for name, response in (
        ("redeem", redeemed),
        ("before", captured),
        ("verify", verified),
        ("status", status),
        ("report", report),
    ):
        assert one_time not in response.text, f"the one-time credential escaped into {name}"
    for name, response in (
        ("before", captured),
        ("verify", verified),
        ("status", status),
        ("report", report),
    ):
        assert session not in response.text, f"the bridge session credential escaped into {name}"

    # Assert - nor does a digest, which is not a credential but is an offline
    # target for anyone who captures a response.
    assert "token_hash" not in status.text

    # Assert - and nothing reached the log, at any level.
    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert one_time not in emitted
    assert session not in emitted


async def test_the_pairing_and_its_run_reach_a_terminal_state_together(trial: Trial) -> None:
    """§16.5: "Pairing and run terminal results must agree".

    Both are written inside one transaction by the sealing callback, so this is
    an assertion about atomicity read from the two surfaces that publish it.
    Reading them separately is the point: a pairing updated after verification
    returned would satisfy the sentence's words and leave a `failed` run under a
    `verifying` pairing whenever the process died in between.
    """
    # Arrange
    pairing_id, session = await trial.armed()

    # Act
    verified = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))

    # Assert
    run_id = verified.json()["run_id"]
    run = await trial.ui.get(f"{RUNS}/{run_id}")
    pairing = (await trial.status(pairing_id)).json()["pairing"]

    assert run.status_code == 200, run.text
    assert run.json()["status"] == "passed"
    assert run.json()["overall_result"] == "passed"
    assert run.json()["completed_at"] is not None
    assert pairing["status"] == "passed"
    assert pairing["completed_at"] is not None
    assert pairing["run_id"] == run_id


async def test_the_unobservable_layers_stay_not_evaluated(trial: Trial) -> None:
    """AC-18: "leaves model selection, observed trajectory, and tool execution
    `not_evaluated` unless separately supported by correlated evaluator evidence".

    This is the assertion the whole Tier 3 design exists to keep true. The bridge
    cannot see which Shopify tools the agent chose or whether they succeeded, so
    inferring any of the three from a cart that changed would be the
    self-report-as-proof error the product exists to refuse — committed against
    a target nobody can re-run. The business-outcome layer is asserted beside
    them so the test cannot pass by evaluating nothing at all.
    """
    # Arrange
    pairing_id, session = await trial.armed()
    verified = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))

    # Act
    report = await trial.ui.get(f"{RUNS}/{verified.json()['run_id']}/report")

    # Assert
    layers = report.json()["report"]["layers"]
    assert layers["model_tool_selection"] == "not_evaluated"
    assert layers["observed_trajectory"] == "not_evaluated"
    assert layers["tool_execution"] == "not_evaluated"
    assert layers["business_outcome"] == "passed"
    # And no tool call was invented to support them.
    assert report.json()["report"]["counts"]["tool_calls"] == 0


async def test_the_timeline_records_the_independent_provenance(trial: Trial) -> None:
    """FR-117 and AC-18: provider `shopify_cart_state`, provenance
    `platform_session_api`, and an actor that says who did the looking.

    A verdict is only worth reading if the record says where the evidence came
    from. `platform_session_api` is the claim that the cart was read from the
    shopper's own session rather than from what a tool said about it, and actor
    `external` is the harness declining to claim it took the reading itself —
    it received one.
    """
    # Arrange
    pairing_id, session = await trial.armed()
    verified = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))

    # Act
    timeline = await trial.ui.get(f"{RUNS}/{verified.json()['run_id']}/events")

    # Assert
    events = timeline.json()["events"]
    snapshots = [event for event in events if event["event_type"] == "snapshot_captured"]
    assert [event["redacted_payload"]["phase"] for event in snapshots] == ["before", "after"]
    initial = snapshots[0]
    assert initial["redacted_payload"]["provider"] == "shopify_cart_state"
    assert initial["redacted_payload"]["provenance"] == "platform_session_api"
    assert initial["actor"] == "external"


async def test_the_verified_run_manufactures_no_tool_event(trial: Trial) -> None:
    """§16: "This exception does not manufacture tool events."

    `verification_gate` was taught to accept `external_observation_received` as
    the completed action precisely so that nothing here would be tempted to
    write a `tool_invocation_completed` that no invocation produced. This is the
    assertion that keeps the two halves of that decision together: if a later
    change satisfies the gate by inventing an invocation instead, the gate goes
    on passing and this fails.
    """
    # Arrange
    pairing_id, session = await trial.armed()
    verified = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))

    # Act
    timeline = await trial.ui.get(f"{RUNS}/{verified.json()['run_id']}/events")

    # Assert
    kinds = [event["event_type"] for event in timeline.json()["events"]]
    assert not [kind for kind in kinds if kind.startswith("tool_invocation")]
    assert kinds.count("external_observation_received") == 1


async def test_the_bridge_authorizes_by_credential_and_never_by_a_workspace(
    app: Any, trial: Trial
) -> None:
    """FR-006: bridge endpoints "never authorize with a caller-supplied workspace ID".

    The bridge client here holds a *different* workspace from the UI that created
    the pairing — which is what a real theme has, since `SameSite=Strict` keeps
    the harness cookie off a storefront request and the middleware then mints a
    fresh workspace for it. The trial still completes, so the credential is doing
    the whole of the work. A route that resolved the workspace from the cookie
    would find the wrong one and fail.

    The other half is that knowing the identifier grants nothing: a third client
    with its own cookie gets a 404, not a 403, because a 403 would confirm the
    pairing exists.
    """
    # Arrange
    pairing_id, session = await trial.armed()
    ui_workspace = (await trial.ui.get(f"{API_PREFIX}/workspace")).json()["workspace_id"]
    bridge_workspace = (await trial.bridge.get(f"{API_PREFIX}/workspace")).json()["workspace_id"]

    # Assert - the guard: the two callers really are different workspaces.
    assert ui_workspace != bridge_workspace

    # Act
    verified = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))

    # Assert
    assert verified.status_code == 200, verified.text

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as stranger:
        looked = await stranger.get(f"{trial.PAIRINGS}/{pairing_id}")
    assert looked.status_code == 404, looked.text
    assert looked.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


async def test_the_preflight_grants_the_configured_store_and_nobody_else(trial: Trial) -> None:
    """§20.1's Shopify CORS clause, on the three routes a storefront page calls.

    `Vary: Origin` is asserted on both answers, including the refusal: a cache
    that did not know the answer depends on the origin would hand the granted
    response to the next caller.
    """
    # Arrange
    pairing_id, _session = await trial.paired()

    for route in ("redeem", "observations/before", "verify"):
        # Act
        granted = await trial.bridge.request(
            "OPTIONS",
            f"{trial.PAIRINGS}/{pairing_id}/{route}",
            headers={
                "Origin": trial.STORE,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization, content-type",
            },
        )
        refused = await trial.bridge.request(
            "OPTIONS",
            f"{trial.PAIRINGS}/{pairing_id}/{route}",
            headers={"Origin": trial.STRANGER, "Access-Control-Request-Method": "POST"},
        )

        # Assert
        assert granted.status_code == 204, granted.text
        assert granted.headers["access-control-allow-origin"] == trial.STORE
        assert granted.headers["access-control-allow-methods"] == "POST, OPTIONS"
        assert granted.headers["access-control-allow-headers"] == "authorization, content-type"
        assert "Origin" in granted.headers["vary"]
        assert "access-control-allow-credentials" not in granted.headers

        assert "access-control-allow-origin" not in refused.headers
        assert "Origin" in refused.headers["vary"]


async def test_reset_cancels_a_live_pairing_and_kills_its_bridge_session(trial: Trial) -> None:
    """FR-013: reset "shall cancel nonterminal runs, benchmarks, pairings".

    The cancellation has to *reach the storefront tab that is still open*. A
    theme holding a live bridge credential would otherwise keep submitting carts
    into a trial the operator ended, and §20.2 requires the harness to "fail
    closed when either credential is stale or reused". So the stored session
    digest is cleared with the status, and the refusal below is `403` rather
    than the `409` a merely-cancelled pairing would give — which is what proves
    the credential itself stopped working rather than only the state moving.
    """
    # Arrange
    pairing_id, session = await trial.armed()
    run_id = (await trial.status(pairing_id)).json()["pairing"]["run_id"]

    # Act
    reset = await trial.ui.post(f"{API_PREFIX}/workspace/reset")

    # Assert
    assert reset.status_code == 200, reset.text
    assert (await trial.status(pairing_id)).json()["pairing"]["status"] == "cancelled"
    assert (await trial.ui.get(f"{RUNS}/{run_id}")).json()["status"] == "cancelled"

    refused = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "AUDIT_NOT_AUTHORIZED"


async def test_reset_frees_the_workspace_to_start_another_trial(trial: Trial) -> None:
    """The other half of cancelling: §17.1's one live slot has to be released.

    A reset that cancelled the pairing without freeing the slot would leave the
    operator permanently unable to start another trial, which is the failure the
    Tier 2 audit surface had before its cancel route existed.
    """
    # Arrange
    first, _session = await trial.paired()
    blocked = await trial.create()
    assert blocked.status_code == 409, blocked.text

    # Act
    await trial.ui.post(f"{API_PREFIX}/workspace/reset")
    second = await trial.create()

    # Assert
    assert second.status_code == 201, second.text
    assert second.json()["pairing_id"] != first


async def test_a_workspace_may_accumulate_only_five_pairings(trial: Trial) -> None:
    """FR-008's ceiling, counted over the workspace's lifetime.

    Distinct from §17.1's "one *nonterminal* pairing", which the reset between
    each create clears: that one bounds concurrency, this one bounds how many
    trials a workspace may accumulate at all, and a workspace can be at five
    with none of them live.
    """
    # Arrange / Act - five complete cycles, each freeing the live slot.
    for attempt in range(5):
        created = await trial.create()
        assert created.status_code == 201, f"pairing {attempt + 1}: {created.text}"
        await trial.ui.post(f"{API_PREFIX}/workspace/reset")

    sixth = await trial.create()

    # Assert
    assert sixth.status_code == 409, sixth.text
    assert sixth.json()["error"]["code"] == "WORKSPACE_LIMIT_EXCEEDED"
    assert "Shopify pairings" in sixth.json()["error"]["message"]


async def test_the_bridge_surface_is_absent_when_no_store_is_configured(tmp_path: Any) -> None:
    """009-T12: a module the deployment reports as off is off *everywhere*.

    Absent rather than mounted-and-refusing, which is the opposite of the choice
    `routes/audits.py` makes and is deliberate: the cut-hygiene gate reads the
    mounted paths, and a Tier 3 route that answers 500 because its settings are
    `None` is the half-shipped failure that gate exists to catch. Asserted over
    HTTP because this FastAPI version hides included routers behind a wrapper
    whose `path` is `None`, so a scan of `app.routes` would pass whatever the
    mounting did.
    """
    # Arrange
    application = create_app(
        environ={"HARNESS_ENV": "local", "BUGGY_STORE_ENABLED": "false"},
        database_path=tmp_path / "unconfigured.sqlite3",
    )

    # Act
    async with (
        application.router.lifespan_context(application),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application, raise_app_exceptions=False),
            base_url="https://harness.test",
        ) as client,
    ):
        created = await client.post(f"{API_PREFIX}/shopify/pairings", json={})
        workspace = await client.get(f"{API_PREFIX}/workspace")

    # Assert
    assert created.status_code == 404, created.text
    # And it says so rather than going quiet (§21.1).
    assert workspace.json()["modules"]["shopify"]["status"] == "disabled"
    assert workspace.json()["modules"]["shopify"]["reason"].strip()


async def test_the_report_contains_complete_external_target_provenance(trial: Trial) -> None:
    """FR-117/§23.9: provenance belongs in the sealed report, not only its timeline."""
    pairing_id, session = await trial.armed()
    verified = await trial.verify(pairing_id, session, trial.cart(variant=trial.VARIANT))

    response = await trial.ui.get(f"{RUNS}/{verified.json()['run_id']}/report")

    assert response.status_code == 200, response.text
    external = response.json()["report"]["external_target"]
    assert external["target_type"] == "shopify_development_store"
    assert external["origin"] == trial.STORE
    assert external["pairing_id"] == pairing_id
    assert external["bridge_version"] == "1.0.0"
    assert external["theme_build_id"] == "theme-build-7"
    assert external["observation_provider"] == "shopify_cart_state"
    assert external["provenance"] == "platform_session_api"
    assert external["safe_scope_result"] == "passed"
    assert set(external["captures"]) == {"before", "after"}
    for capture in external["captures"].values():
        assert capture["path"] == "/cart.js"
        assert capture["captured_at"].endswith("Z")
        assert capture["content_hash"].startswith("sha256:")
