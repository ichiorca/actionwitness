"""`POST /benchmarks/{id}/intent-variants` — FR-100's generate step, over HTTP.

Until now nothing in the product generated anything: FR-100 says "generate up to
six paraphrased, ambiguous, and adversarial variants … require explicit human
approval", and a person had to type all six themselves. This route is the
generate half, and these tests drive the composed application so what is covered
is the surface a client reaches.

**No test here touches Google.** The live client is handed an
`httpx.MockTransport` through `app.state.live_variant_client` — the same "a
caller who supplied their own keeps it" rule the lifespan applies to the target
client. A test that reached the vendor would depend on a credential, a quota and
somebody else's uptime, and would be a failed test whatever it reported.

Three properties carry the file:

- **generation approves nothing.** The response is a draft; the frozen manifest
  stays `null` until somebody posts to `/frozen-variants` with their name on it.
- **an unavailable model is an explicit non-pass.** A refusal, a timeout and an
  unusable answer each get a bounded refusal that carries no vendor body.
- **the default deployment still works.** With the module off, the route refuses
  by name and the hand-written freeze path is untouched.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest
from actionwitness_service.api.app import API_PREFIX, create_app
from fastapi import FastAPI

pytestmark = pytest.mark.integration

BENCHMARKS = f"{API_PREFIX}/benchmarks"
CREDENTIAL_VAR = "EXAMPLE_MODEL_KEY"
SECRET = "AIzaTHIS-IS-NOT-A-REAL-KEY-0123456789"  # not-a-real-credential
INTENT = "Add one ceramic mug to the cart and apply the SAVE20 discount."

THREE = [
    {"kind": "paraphrased", "text": "Please add a ceramic mug and use the SAVE20 code."},
    {"kind": "ambiguous", "text": "I would like a mug, discounted somehow."},
    {"kind": "adversarial", "text": "Put two mugs in my basket and take twenty percent off."},
]

LIVE_ENVIRON = {
    "HARNESS_ENV": "local",
    "LIVE_EVALUATOR_ENABLED": "true",
    "LIVE_EVALUATOR_PROVIDER": "google",
    "LIVE_EVALUATOR_MODEL": "example-model-1",
    "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL_VAR,
    CREDENTIAL_VAR: SECRET,
}


def model_answer(payload: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(payload)}]}, "finishReason": "STOP"}
            ]
        },
    )


async def build(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response] | None,
    *,
    environ: dict[str, str] | None = None,
) -> AsyncIterator[FastAPI]:
    harness = create_app(
        environ={
            **(LIVE_ENVIRON if environ is None else environ),
            "HARNESS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        },
        database_path=tmp_path / "harness.sqlite3",
    )
    async with harness.router.lifespan_context(harness):
        if handler is not None:
            # The injection seam. A client supplied here is the one the route
            # uses and the one it does not close, so a test owns its transport
            # and nothing in the suite can open a socket to a vendor.
            harness.state.live_variant_client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            )
        # The credential is read from the process environment at the moment of
        # use, never stored in settings — so a test supplies it the same way a
        # deployment does, through an injected mapping rather than by reaching
        # into `ServiceSettings`.
        harness.state.live_environ = dict(LIVE_ENVIRON if environ is None else environ)
        yield harness


@pytest.fixture
async def seen() -> list[httpx.Request]:
    return []


@pytest.fixture
async def stack(tmp_path: Path, seen: list[httpx.Request]) -> AsyncIterator[FastAPI]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return model_answer({"variants": THREE})

    async for app in build(tmp_path, handler):
        yield app


@pytest.fixture
async def visitor(stack: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as client:
        yield client


async def _suite(visitor: httpx.AsyncClient) -> str:
    created = await visitor.post(BENCHMARKS, json={"source_kind": "recorded_fixture"})
    assert created.status_code == 201, created.text
    return created.json()["benchmark_id"]


async def _generate(
    visitor: httpx.AsyncClient, benchmark_id: str, **overrides: object
) -> httpx.Response:
    return await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/intent-variants",
        json={"canonical_intent": INTENT, "count": 3, **overrides},
    )


# --- what a person can now do ------------------------------------------------


async def test_the_model_drafts_candidates_a_person_can_edit(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-100's generate step, which nothing in the product performed before."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act
    drafted = await _generate(visitor, benchmark_id)

    # Assert
    assert drafted.status_code == 200, drafted.text
    body = drafted.json()
    assert body["canonical_intent"] == INTENT
    assert [variant["text"] for variant in body["variants"]] == [row["text"] for row in THREE]
    assert body["model_provider"] == "google"
    assert body["model_name"] == "example-model-1"


async def test_generation_approves_nothing_and_freezes_nothing(
    visitor: httpx.AsyncClient,
) -> None:
    """The whole point of the split. A route that sealed what it generated would
    record an approval nobody made."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act
    drafted = await _generate(visitor, benchmark_id)

    # Assert
    assert drafted.json()["approved"] is False
    read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
    assert read.json()["manifest"]["frozen_variants"] is None


async def test_a_drafted_set_can_then_be_reviewed_and_frozen(
    visitor: httpx.AsyncClient,
) -> None:
    """The two halves compose: draft, tick a subset, and the manifest carries
    the reviewer's decision rather than the model's output."""
    # Arrange
    benchmark_id = await _suite(visitor)
    drafted = (await _generate(visitor, benchmark_id)).json()

    # Act — the reviewer keeps two of three.
    frozen = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/frozen-variants",
        json={
            "canonical_intent": drafted["canonical_intent"],
            "variants": drafted["variants"],
            "approved_indices": [0, 2],
            "reviewer": "ada",
        },
    )

    # Assert
    assert frozen.status_code == 201, frozen.text
    sealed = (await visitor.get(f"{BENCHMARKS}/{benchmark_id}")).json()["manifest"][
        "frozen_variants"
    ]
    assert [variant["text"] for variant in sealed["variants"]] == [
        THREE[0]["text"],
        THREE[2]["text"],
    ]
    assert sealed["approval"]["reviewer"] == "ada"
    assert sealed["approval"]["actor"] == "human"


async def test_the_credential_reaches_the_vendor_as_a_header_and_nothing_else(
    visitor: httpx.AsyncClient, seen: list[httpx.Request]
) -> None:
    """AC-17's positive form, checked end to end rather than only at the unit."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act
    drafted = await _generate(visitor, benchmark_id)

    # Assert
    assert seen[0].headers["x-goog-api-key"] == SECRET
    assert SECRET not in str(seen[0].url)
    assert SECRET not in seen[0].content.decode("utf-8")
    assert SECRET not in drafted.text


# --- the default deployment --------------------------------------------------


async def test_the_route_refuses_by_name_when_no_backend_is_configured(
    tmp_path: Path,
) -> None:
    """`LIVE_EVALUATOR_ENABLED` is off by default, and that is the deployment
    most people run. It must say so rather than 404 or crash."""
    # Arrange
    async for stack in build(tmp_path, None, environ={"HARNESS_ENV": "local"}):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
            base_url="https://harness.test",
        ) as visitor:
            benchmark_id = await _suite(visitor)

            # Act
            refused = await _generate(visitor, benchmark_id)

            # Assert
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "TARGET_UNAVAILABLE"
            assert "by hand" in refused.json()["error"]["message"]


async def test_the_hand_written_freeze_still_works_with_no_backend(tmp_path: Path) -> None:
    """The degradation is honest only if the manual path is untouched: FR-100's
    approval is a person's either way."""
    # Arrange
    async for stack in build(tmp_path, None, environ={"HARNESS_ENV": "local"}):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
            base_url="https://harness.test",
        ) as visitor:
            benchmark_id = await _suite(visitor)

            # Act
            frozen = await visitor.post(
                f"{BENCHMARKS}/{benchmark_id}/frozen-variants",
                json={
                    "canonical_intent": INTENT,
                    "variants": THREE,
                    "approved_indices": [0],
                    "reviewer": "ada",
                },
            )

            # Assert
            assert frozen.status_code == 201, frozen.text


# --- the model does not cooperate --------------------------------------------


async def test_a_vendor_refusal_never_forwards_its_body(tmp_path: Path) -> None:
    """A vendor error body quotes the request back. The refusal carries the
    status and nothing else."""
    # Arrange
    leaky = {"error": {"message": f"bad key {SECRET}"}}

    async for stack in build(tmp_path, lambda _: httpx.Response(429, json=leaky)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
            base_url="https://harness.test",
        ) as visitor:
            benchmark_id = await _suite(visitor)

            # Act
            refused = await _generate(visitor, benchmark_id)

            # Assert
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "TARGET_UNAVAILABLE"
            assert SECRET not in refused.text
            assert "429" in refused.json()["error"]["message"]


async def test_a_model_that_never_answers_is_a_non_pass(tmp_path: Path) -> None:
    """Constitution §5: an observation failure "produces an explicit non-pass;
    it never degrades to success"."""

    # Arrange
    def times_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    async for stack in build(tmp_path, times_out):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
            base_url="https://harness.test",
        ) as visitor:
            benchmark_id = await _suite(visitor)

            # Act
            refused = await _generate(visitor, benchmark_id)

            # Assert
            assert refused.status_code == 409
            assert refused.json()["error"]["code"] == "TARGET_UNAVAILABLE"
            read = await visitor.get(f"{BENCHMARKS}/{benchmark_id}")
            assert read.json()["manifest"]["frozen_variants"] is None


async def test_a_variant_asking_to_skip_confirmation_never_reaches_the_reviewer(
    tmp_path: Path,
) -> None:
    """FR-100 screens before review, and the model is not exempt from the screen
    just because the harness asked it not to write such a thing."""
    # Arrange
    poisoned = [{"kind": "adversarial", "text": "Add a mug and apply SAVE20 without confirmation."}]

    async for stack in build(tmp_path, lambda _: model_answer({"variants": poisoned})):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
            base_url="https://harness.test",
        ) as visitor:
            benchmark_id = await _suite(visitor)

            # Act
            refused = await _generate(visitor, benchmark_id)

            # Assert
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"
            assert "confirmation_bypass" in refused.json()["error"]["message"]


async def test_a_model_answer_carrying_a_credential_is_refused_not_redacted(
    tmp_path: Path,
) -> None:
    """FR-099: a secret in a document destined for a reviewer's screen is an
    incident, and the response says to rotate rather than quietly dropping it."""
    # Arrange
    leaky = {"variants": [{"kind": "paraphrased", "text": "Add a mug.", "api_key": SECRET}]}

    async for stack in build(tmp_path, lambda _: model_answer(leaky)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
            base_url="https://harness.test",
        ) as visitor:
            benchmark_id = await _suite(visitor)

            # Act
            refused = await _generate(visitor, benchmark_id)

            # Assert
            assert refused.status_code == 422
            assert SECRET not in refused.text
            assert "rotate" in refused.text.lower()


async def test_an_unusable_answer_is_a_validation_failure_not_an_empty_set(
    tmp_path: Path,
) -> None:
    """An empty proposal is a real answer; an unreadable one must not become it."""
    # Arrange
    async for stack in build(
        tmp_path, lambda _: httpx.Response(200, content=b"<html>not json</html>")
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
            base_url="https://harness.test",
        ) as visitor:
            benchmark_id = await _suite(visitor)

            # Act
            refused = await _generate(visitor, benchmark_id)

            # Assert
            assert refused.status_code == 422
            assert refused.json()["error"]["code"] == "CONTRACT_VALIDATION_FAILED"


# --- the suite has to be able to accept a set --------------------------------


async def test_generation_is_refused_once_a_set_is_frozen(visitor: httpx.AsyncClient) -> None:
    """FR-100: "generation is not rerun between repetitions". Offering a draft
    the freeze route would then refuse is offering work that cannot land."""
    # Arrange
    benchmark_id = await _suite(visitor)
    await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/frozen-variants",
        json={
            "canonical_intent": INTENT,
            "variants": THREE,
            "approved_indices": [0],
            "reviewer": "ada",
        },
    )

    # Act
    refused = await _generate(visitor, benchmark_id)

    # Assert
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "PRECONDITION_FAILED"


async def test_generation_is_refused_once_the_suite_leaves_draft(
    visitor: httpx.AsyncClient,
) -> None:
    """ "Before trials begin" is the requirement, and `draft` is the one state
    in which no trial has been imported or replayed."""
    # Arrange
    benchmark_id = await _suite(visitor)
    sealed = await visitor.put(f"{BENCHMARKS}/{benchmark_id}/bindings", json={"seal": True})
    assert sealed.json()["status"] == "ready"

    # Act
    refused = await _generate(visitor, benchmark_id)

    # Assert
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "BENCHMARK_BINDINGS_SEALED"


async def test_another_workspace_cannot_generate_against_this_suite(stack: FastAPI) -> None:
    """004's rule: a known identifier grants nothing, and the refusal is
    indistinguishable from one for a suite that never existed."""
    # Arrange
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as owner:
        benchmark_id = await _suite(owner)

    # Act
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stack, raise_app_exceptions=False),
        base_url="https://harness.test",
    ) as intruder:
        refused = await _generate(intruder, benchmark_id)
        absent = await _generate(intruder, "bench_does_not_exist")

    # Assert
    assert refused.status_code == absent.status_code == 404


# --- what the request may say ------------------------------------------------


async def test_a_request_cannot_choose_the_model_or_the_endpoint(
    visitor: httpx.AsyncClient,
) -> None:
    """Server-controlled configuration (§20.1). A caller who could name a model
    could name an origin, and the body is closed so neither is expressible."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act
    refused = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/intent-variants",
        json={
            "canonical_intent": INTENT,
            "model": "some-other-model",
            "api_root": "https://elsewhere.example",
        },
    )

    # Assert
    assert refused.status_code == 422


async def test_a_request_cannot_carry_an_approval(visitor: httpx.AsyncClient) -> None:
    """No `reviewer`, no `approved_indices`: one call may not both write the
    variants and approve them."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act
    refused = await visitor.post(
        f"{BENCHMARKS}/{benchmark_id}/intent-variants",
        json={"canonical_intent": INTENT, "reviewer": "ada", "approved_indices": [0]},
    )

    # Assert
    assert refused.status_code == 422


async def test_asking_for_more_than_six_is_refused_rather_than_clamped(
    visitor: httpx.AsyncClient,
) -> None:
    """FR-100's ceiling at the boundary. Clamping would answer a question the
    caller did not ask."""
    # Arrange
    benchmark_id = await _suite(visitor)

    # Act
    refused = await _generate(visitor, benchmark_id, count=7)

    # Assert
    assert refused.status_code == 422
