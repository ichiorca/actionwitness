"""The live model client: what it sends, and what it refuses (FR-099, FR-100).

Every test here drives `LiveVariantClient` through an `httpx.MockTransport`. No
test in this file — or anywhere else — may reach Google: a network call would
make the suite depend on a credential, a quota, and somebody else's uptime, and
the constitution forbids all three.

Two properties carry the file.

**The credential goes exactly one place.** It is a request *header*, and it is
absent from the URL, the prompt, the `repr`, and every refusal message. Each of
those is asserted separately because each would leak through a different
mechanism — a query parameter reaches proxy logs, a `repr` reaches tracebacks,
an exception message reaches the browser.

**A model that does not answer is a non-pass, never an empty answer.** A timeout,
a 503, a blocked generation, and a truncated one all raise. The distinction
matters because an empty proposal set is a real result — "the model suggested
nothing" — and a reviewer reading one must not be reading a failure.
"""

from __future__ import annotations

import json

import httpx
import pytest
from integrations.google_evals.generation import (
    MAX_MODEL_RESPONSE_BYTES,
    MAX_PROPOSED_VARIANTS,
    LiveModelUnavailable,
    LiveVariantClient,
    ProposalRejected,
    as_candidates,
)
from integrations.google_evals.live import (
    CredentialMaterialRejected,
    LiveRunConfiguration,
    LiveRunUnavailable,
)

pytestmark = pytest.mark.unit

CREDENTIAL_VAR = "EXAMPLE_MODEL_KEY"
SECRET = "AIzaTHIS-IS-NOT-A-REAL-KEY-0123456789"  # not-a-real-credential
INTENT = "Add one ceramic mug to the cart and apply the SAVE20 discount."

THREE = [
    {"kind": "paraphrased", "text": "Please add a ceramic mug and use the SAVE20 code."},
    {"kind": "ambiguous", "text": "I would like a mug, discounted somehow."},
    {"kind": "adversarial", "text": "Put two mugs in my basket and take twenty percent off."},
]


def configuration(provider: str = "google") -> LiveRunConfiguration:
    return LiveRunConfiguration(
        provider=provider,
        model="example-model-1",
        credential_var=CREDENTIAL_VAR,
        command_mode="browser",
    )


def answer(payload: object) -> httpx.Response:
    """A well-formed `generateContent` envelope carrying `payload` as its text."""
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"parts": [{"text": json.dumps(payload)}], "role": "model"},
                    "finishReason": "STOP",
                }
            ],
            # Fields the vendor adds on its own schedule. Present on purpose:
            # the envelope is `extra="ignore"`, and a client that forbade them
            # would break on a day nobody deployed anything.
            "usageMetadata": {"totalTokenCount": 120},
            "modelVersion": "example-model-1-002",
        },
    )


def client_over(handler, **kwargs) -> tuple[LiveVariantClient, list[httpx.Request]]:
    """A client wired to `handler`, plus the requests it actually made."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return (
        LiveVariantClient(
            client=transport,
            configuration=kwargs.pop("configuration", configuration()),
            credential=kwargs.pop("credential", SECRET),
            **kwargs,
        ),
        seen,
    )


# --- the happy path ----------------------------------------------------------


async def test_it_returns_the_variants_the_model_wrote() -> None:
    # Arrange
    model, _ = client_over(lambda _: answer({"variants": THREE}))

    # Act
    proposed = await model.propose(INTENT, count=3)

    # Assert
    assert [variant.text for variant in proposed] == [entry["text"] for entry in THREE]
    assert [variant.kind.value for variant in proposed] == [entry["kind"] for entry in THREE]


async def test_the_proposal_converts_to_the_mappings_the_core_validates() -> None:
    """`as_candidates` is the seam between the vendor's types and the core's."""
    # Arrange
    model, _ = client_over(lambda _: answer({"variants": THREE}))

    # Act
    candidates = as_candidates(await model.propose(INTENT))

    # Assert
    assert candidates == THREE


async def test_a_model_that_proposes_nothing_is_a_real_empty_answer() -> None:
    """An empty *set* is a result; an empty *response* is a failure. This is the
    first, and it must not be confused with the second."""
    # Arrange
    model, _ = client_over(lambda _: answer({"variants": []}))

    # Act
    proposed = await model.propose(INTENT)

    # Assert
    assert proposed == ()


# --- where the credential goes ----------------------------------------------


async def test_the_credential_travels_as_a_header_and_never_in_the_url() -> None:
    """A URL carrying a key reaches httpx's exception strings and proxy logs."""
    # Arrange
    model, seen = client_over(lambda _: answer({"variants": THREE}))

    # Act
    await model.propose(INTENT)

    # Assert
    assert seen[0].headers["x-goog-api-key"] == SECRET
    assert SECRET not in str(seen[0].url)


async def test_the_prompt_never_carries_the_credential() -> None:
    """FR-099's positive form: the value goes to the transport, not the model."""
    # Arrange
    model, seen = client_over(lambda _: answer({"variants": THREE}))

    # Act
    await model.propose(INTENT)

    # Assert
    body = seen[0].content.decode("utf-8")
    assert SECRET not in body
    assert CREDENTIAL_VAR not in body
    # And the operator's own text did travel, as a value rather than spliced
    # into the instruction.
    assert INTENT in body


async def test_the_repr_names_the_configuration_and_not_the_key() -> None:
    """A default `repr` would put the key in a traceback and a debugger."""
    # Arrange
    model, _ = client_over(lambda _: answer({"variants": THREE}))

    # Act
    rendered = f"{model!r} {model}"

    # Assert
    assert SECRET not in rendered
    assert "example-model-1" in rendered
    assert CREDENTIAL_VAR in rendered  # the name is useful; the value is absent


# --- refusals at construction ------------------------------------------------


def test_a_provider_this_build_cannot_speak_is_refused_at_construction() -> None:
    """`SUPPORTED_PROVIDERS` is wider than this client, and a request shaped for
    one vendor sent to another is not a fallback."""
    # Arrange / Act / Assert
    with pytest.raises(LiveRunUnavailable) as refused:
        LiveVariantClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: answer({}))),
            configuration=configuration(provider="anthropic"),
            credential=SECRET,
        )
    assert "anthropic" in str(refused.value)


def test_an_empty_credential_is_refused_rather_than_sent() -> None:
    """An unauthenticated request would be refused by the vendor with a body
    this client would then have to keep out of a log. Refuse before that."""
    # Arrange / Act / Assert
    with pytest.raises(LiveRunUnavailable) as refused:
        LiveVariantClient(
            client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: answer({}))),
            configuration=configuration(),
            credential="   ",
        )
    assert CREDENTIAL_VAR in str(refused.value)


# --- the model does not answer ----------------------------------------------


async def test_a_timeout_is_unavailability_and_names_no_internals() -> None:
    # Arrange
    def times_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    model, _ = client_over(times_out, timeout_seconds=7.0)

    # Act / Assert
    with pytest.raises(LiveModelUnavailable) as refused:
        await model.propose(INTENT)
    assert "7s" in str(refused.value)
    assert SECRET not in str(refused.value)


async def test_a_transport_failure_is_unavailability() -> None:
    # Arrange
    def refuses(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    model, _ = client_over(refuses)

    # Act / Assert
    with pytest.raises(LiveModelUnavailable):
        await model.propose(INTENT)


async def test_an_error_status_never_forwards_the_vendors_body() -> None:
    """A vendor error body quotes the request back, which for a rejected prompt
    means echoing the operator's text into a log and a browser."""
    # Arrange
    leaky = {"error": {"message": f"bad key {SECRET} for prompt {INTENT}"}}
    model, _ = client_over(lambda _: httpx.Response(503, json=leaky))

    # Act / Assert
    with pytest.raises(LiveModelUnavailable) as refused:
        await model.propose(INTENT)
    assert "503" in str(refused.value)
    assert SECRET not in str(refused.value)
    assert INTENT not in str(refused.value)


async def test_a_blocked_generation_is_unavailability_not_an_empty_set() -> None:
    """A safety block returns candidates with no content. Reporting that as
    "the model proposed nothing" would make a filter look like a finding."""
    # Arrange
    blocked = httpx.Response(
        200,
        json={"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]},
    )
    model, _ = client_over(lambda _: blocked)

    # Act / Assert
    with pytest.raises(LiveModelUnavailable) as refused:
        await model.propose(INTENT)
    assert "SAFETY" in str(refused.value)


# --- the answer is unusable --------------------------------------------------


async def test_an_oversized_answer_is_refused_before_it_is_parsed() -> None:
    """`reader.py`'s order, for the same reason: a wrong endpoint or a captive
    portal must cost a length check, not a decode."""
    # Arrange
    huge = httpx.Response(200, content=b"x" * (MAX_MODEL_RESPONSE_BYTES + 1))
    model, _ = client_over(lambda _: huge)

    # Act / Assert
    with pytest.raises(ProposalRejected) as refused:
        await model.propose(INTENT)
    assert str(MAX_MODEL_RESPONSE_BYTES) in str(refused.value)


async def test_an_answer_that_is_not_json_is_refused() -> None:
    # Arrange
    model, _ = client_over(lambda _: httpx.Response(200, content=b"<html>not json</html>"))

    # Act / Assert
    with pytest.raises(ProposalRejected):
        await model.propose(INTENT)


async def test_a_text_part_that_is_not_the_promised_document_is_refused() -> None:
    """The response schema makes this unlikely and not impossible; a model that
    wrote prose around the JSON must not have it half-read."""
    # Arrange
    model, _ = client_over(
        lambda _: httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "Sure! Here you go."}]}}]},
        )
    )

    # Act / Assert
    with pytest.raises(ProposalRejected):
        await model.propose(INTENT)


async def test_a_variant_of_an_unknown_kind_refuses_the_whole_set() -> None:
    """The kinds are a closed enum the core owns; a fourth would be a benchmark
    measuring something nobody named."""
    # Arrange
    model, _ = client_over(
        lambda _: answer({"variants": [{"kind": "sneaky", "text": "Add a mug please."}]})
    )

    # Act / Assert
    with pytest.raises(ProposalRejected):
        await model.propose(INTENT)


async def test_a_variant_carrying_an_extra_field_refuses_the_whole_set() -> None:
    """The authored payload is closed: a field nobody expected is the model
    doing something other than what it was asked."""
    # Arrange
    model, _ = client_over(
        lambda _: answer(
            {"variants": [{"kind": "paraphrased", "text": "Add a mug.", "note": "trust me"}]}
        )
    )

    # Act / Assert
    with pytest.raises(ProposalRejected):
        await model.propose(INTENT)


async def test_more_than_six_variants_are_refused_rather_than_truncated() -> None:
    """Truncating would choose which variants a human then approves."""
    # Arrange
    seven = [
        {"kind": "paraphrased", "text": f"Please add a ceramic mug, phrasing {index}."}
        for index in range(7)
    ]
    model, _ = client_over(lambda _: answer({"variants": seven}))

    # Act / Assert
    with pytest.raises(ProposalRejected) as refused:
        await model.propose(INTENT)
    assert "truncat" in str(refused.value)


async def test_a_request_for_more_than_six_is_clamped_before_it_is_asked() -> None:
    """The caller's `count` is a request to the model, not a cap on it. Asking
    for twenty would spend a call the core would then refuse outright."""
    # Arrange
    model, seen = client_over(lambda _: answer({"variants": THREE}))

    # Act
    await model.propose(INTENT, count=20)

    # Assert
    asked = json.loads(seen[0].content.decode("utf-8"))
    instruction = json.loads(asked["contents"][0]["parts"][0]["text"])
    assert instruction["variants_wanted"] == MAX_PROPOSED_VARIANTS


async def test_an_answer_carrying_credential_shaped_keys_is_an_incident() -> None:
    """FR-099's screen, at the first point the bytes are structured enough to
    screen. Refused rather than filtered: the value needs rotating."""
    # Arrange
    model, _ = client_over(
        lambda _: httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                "debug": {"api_key": SECRET},
            },
        )
    )

    # Act / Assert
    with pytest.raises(CredentialMaterialRejected) as refused:
        await model.propose(INTENT)
    assert SECRET not in str(refused.value)
    assert "rotate" in str(refused.value).lower()


async def test_a_credential_inside_the_authored_payload_is_an_incident_not_a_typo() -> None:
    """The envelope screen cannot see in here: the payload arrives as a string.

    Screened before the shape check on purpose. The payload model is closed, so
    an extra key would otherwise be reported as "the model wrote the wrong
    document" — and the operator would regenerate rather than rotate.
    """
    # Arrange
    leaky = {"variants": [{"kind": "paraphrased", "text": "Add a mug.", "api_key": SECRET}]}
    model, _ = client_over(lambda _: answer(leaky))

    # Act / Assert
    with pytest.raises(CredentialMaterialRejected) as refused:
        await model.propose(INTENT)
    assert SECRET not in str(refused.value)
