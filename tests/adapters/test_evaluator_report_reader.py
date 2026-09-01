"""008-T3 — reading an untrusted evaluator report (FR-090, §26.5, ADR-0005).

The requirement this file exists to hold is an *ordering*, and ordering is
invisible in a result: a reader that redacted after hashing would produce the
same `ImportedReport` fields and the wrong stored hash. So the tests below check
the order through its observable consequences — the hash is over the redacted
document, and an oversized report is refused with nothing parsed.

§26.5's list is the outline: known-good fixtures import, malformed and oversized
reports are rejected, and the whole path runs with no Node, no API key, no
Shopify, and no Buggy Store package. Nothing here imports any of those.
"""

from __future__ import annotations

import json

import pytest
from actionwitness_core.kernel import CoreErrorCode
from actionwitness_core.security.canonical import content_hash
from actionwitness_core.security.redaction import RedactionPolicy
from integrations.google_evals.pins import REPORTER_SCHEMA, REPORTER_VERSION
from integrations.google_evals.reader import (
    ImportLimits,
    ReportRejected,
    read_report,
)

pytestmark = pytest.mark.adapters


def _trial(name: str, outcome: str, **extra: object) -> dict:
    return {
        "test": {"name": name},
        "response": "the assistant's reply",
        "outcome": outcome,
        "runIndex": 0,
        **extra,
    }


def _report(*trials: dict, config: dict | None = None) -> bytes:
    results = list(trials)
    document = {
        "config": {
            "reporterSchema": REPORTER_SCHEMA,
            "evaluatorVersion": REPORTER_VERSION,
            **(config or {}),
        },
        "results": {
            "results": results,
            "testCount": len(results),
            "passCount": sum(1 for trial in results if trial["outcome"] == "pass"),
            "failCount": sum(1 for trial in results if trial["outcome"] == "fail"),
            "errorCount": sum(1 for trial in results if trial["outcome"] == "error"),
        },
    }
    return json.dumps(document).encode("utf-8")


# --- the happy path ----------------------------------------------------------


def test_a_pinned_report_imports() -> None:
    """§26.5: "import known-good JSON fixtures for every allowlisted version"."""
    # Arrange
    raw = _report(_trial("adds a mug", "pass"), _trial("applies the discount", "fail"))

    # Act
    imported = read_report(raw)

    # Assert
    assert imported.trial_count == 2
    assert imported.reporter_schema == REPORTER_SCHEMA
    assert imported.content_hash.startswith("sha256:")


def test_the_import_needs_no_service_configuration() -> None:
    """§26.5's headline constraint, asserted as an import-time property.

    The limits arrive as an argument rather than from settings, so this package
    stays usable with no environment at all — which is what lets AC-16 run in CI
    with no Node, no key, and no target package installed.
    """
    # Arrange
    raw = _report(_trial("one", "pass"))

    # Act
    imported = read_report(raw, limits=ImportLimits(max_bytes=4096, max_trials=5))

    # Assert
    assert imported.trial_count == 1


# --- the caps ----------------------------------------------------------------


def test_an_oversized_report_is_refused_before_it_is_parsed() -> None:
    """FR-090's 1 MiB cap, checked on bytes.

    The payload is deliberately *unparseable*: if the reader had decoded before
    measuring, this would fail as malformed JSON instead, and the test would
    catch a reordering that a well-formed payload would hide.
    """
    # Arrange
    raw = b"{" + b"x" * 4096

    # Act / Assert
    with pytest.raises(ReportRejected) as refused:
        read_report(raw, limits=ImportLimits(max_bytes=64))
    assert refused.value.code is CoreErrorCode.RESOURCE_LIMIT_EXCEEDED
    assert "the limit is 64" in str(refused.value)


def test_a_report_at_exactly_the_limit_is_accepted() -> None:
    """A cap that rejected its own boundary would be a different cap."""
    # Arrange
    raw = _report(_trial("one", "pass"))

    # Act
    imported = read_report(raw, limits=ImportLimits(max_bytes=len(raw)))

    # Assert
    assert imported.trial_count == 1


def test_too_many_trials_are_refused() -> None:
    """FR-090's 100-trial cap, applied to the array length."""
    # Arrange
    raw = _report(*[_trial(f"t{index}", "pass") for index in range(6)])

    # Act / Assert
    with pytest.raises(ReportRejected) as refused:
        read_report(raw, limits=ImportLimits(max_trials=5))
    assert refused.value.code is CoreErrorCode.RESOURCE_LIMIT_EXCEEDED


def test_the_trial_cap_is_applied_before_any_trial_is_validated() -> None:
    """Ordering again, made observable: every trial in this report is invalid,
    so a reader that validated first would report a schema failure.

    The cap must win, because the whole point of a count check is to avoid
    paying per-element costs on a report that is already too big.
    """
    # Arrange
    raw = _report(*[{"outcome": "nonsense"} for _ in range(6)])

    # Act / Assert
    with pytest.raises(ReportRejected) as refused:
        read_report(raw, limits=ImportLimits(max_trials=5))
    assert refused.value.code is CoreErrorCode.RESOURCE_LIMIT_EXCEEDED


# --- malformed input ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        (b"not json at all", "not valid JSON"),
        (b"[1, 2, 3]", "must be a JSON object"),
        (b'"a string"', "must be a JSON object"),
        (b"\xff\xfe not utf-8", "not valid UTF-8"),
    ],
)
def test_a_malformed_report_is_refused(raw: bytes, fragment: str) -> None:
    """§26.5: "reject malformed or oversized reports"."""
    # Arrange / Act / Assert
    with pytest.raises(ReportRejected) as refused:
        read_report(raw)
    assert fragment in str(refused.value)


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"config": {}},
        {"results": {"results": []}},
        {"config": {}, "results": {"results": []}, "extra": 1},
    ],
)
def test_an_unrecognised_top_level_shape_is_refused(document: dict) -> None:
    """ADR-0005 decision 2: the `{config, results}` document, exactly.

    An unknown top-level key means this is not the document the pin describes,
    and reading it would be guessing which field means what.
    """
    # Arrange
    raw = json.dumps(document).encode("utf-8")

    # Act / Assert
    with pytest.raises(ReportRejected):
        read_report(raw)


def test_an_unknown_reporter_schema_is_refused() -> None:
    """The pin is a refusal, not a preference (ADR-0005)."""
    # Arrange
    raw = _report(_trial("one", "pass"), config={"reporterSchema": "webmcp-evals/0.0.9"})

    # Act / Assert
    with pytest.raises(ReportRejected) as refused:
        read_report(raw)
    assert refused.value.code is CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION


def test_an_unknown_evaluator_version_is_refused() -> None:
    """Announced version and pin must agree, or provenance is a guess."""
    # Arrange
    raw = _report(_trial("one", "pass"), config={"evaluatorVersion": "0.0.9"})

    # Act / Assert
    with pytest.raises(ReportRejected) as refused:
        read_report(raw)
    assert refused.value.code is CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION


def test_an_unrecognised_outcome_is_refused() -> None:
    """`outcome` is the call-level verdict itself; an unknown value cannot be
    normalized to `null` the way unknown metadata can."""
    # Arrange
    raw = _report(_trial("one", "flaky"))

    # Act / Assert
    with pytest.raises(ReportRejected) as refused:
        read_report(raw)
    assert refused.value.code is CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION


def test_an_unknown_trial_key_is_tolerated() -> None:
    """The counterpart to the strict top level, and deliberately different.

    Upstream adds per-trial diagnostic fields between patch releases. Refusing a
    whole report over one would make the pin useless without making it safer;
    FR-093 preserves what is not understood as `null` at normalization instead.
    """
    # Arrange
    raw = _report(_trial("one", "pass", someNewUpstreamField={"depth": 2}))

    # Act
    imported = read_report(raw)

    # Assert
    assert imported.trial_count == 1


# --- redaction before hashing ------------------------------------------------


def test_a_secret_bearing_field_is_removed() -> None:
    """FR-090: "redact configured secret fields"."""
    # Arrange
    raw = _report(_trial("one", "pass", response="contact me at real.person@example.com"))
    document = json.loads(raw)
    document["config"]["apiKey"] = "sk-live-not-a-real-key"
    raw = json.dumps(document).encode("utf-8")

    # Act
    imported = read_report(raw)

    # Assert
    assert "sk-live-not-a-real-key" not in json.dumps(dict(imported.document))
    assert imported.redacted is True


def test_the_hash_is_over_the_redacted_document() -> None:
    """The ordering requirement, stated as the property that proves it.

    The stored hash *is* what gets compared later, so hashing before redaction
    would commit the unredacted bytes to the evidence chain permanently. Checked
    by recomputing the hash over the redacted document the reader returned.
    """
    # Arrange
    document = json.loads(_report(_trial("one", "pass")))
    document["config"]["authorization"] = "Bearer abc123"
    raw = json.dumps(document).encode("utf-8")

    # Act
    imported = read_report(raw)

    # Assert
    assert imported.content_hash == content_hash(dict(imported.document))
    assert imported.content_hash != content_hash(document)


def test_a_contract_policy_widens_redaction_rather_than_replacing_it() -> None:
    """§20.3: contract paths apply "in addition to defaults"."""
    # Arrange
    document = json.loads(_report(_trial("one", "pass")))
    document["config"]["internalNote"] = "commercially sensitive"
    document["config"]["token"] = "still-a-secret"
    raw = json.dumps(document).encode("utf-8")

    # Act
    imported = read_report(raw, policy=RedactionPolicy.from_paths(["config.internalNote"]))

    # Assert
    text = json.dumps(dict(imported.document))
    assert "commercially sensitive" not in text
    assert "still-a-secret" not in text


def test_a_report_with_nothing_to_redact_says_so() -> None:
    """`redacted` is a fact about this document, not a claim the reader makes
    about every document — a fixture asserting it was redacted should be able to
    be checked rather than believed."""
    # Arrange
    raw = _report(_trial("one", "pass"))

    # Act
    imported = read_report(raw)

    # Assert
    assert imported.redacted is False


def test_reading_the_same_report_twice_produces_the_same_hash() -> None:
    """FR-089's interchange promise: a reader who was handed this artifact can
    verify it without trusting the sender."""
    # Arrange
    raw = _report(_trial("one", "pass"), _trial("two", "fail"))

    # Act
    first = read_report(raw)
    second = read_report(raw)

    # Assert
    assert first.content_hash == second.content_hash


def test_a_camel_case_credential_is_removed_by_the_adapters_own_policy() -> None:
    """The gap this adapter closes deliberately.

    The core's default keys match `api_key` and not `apiKey`, and widening the
    shared matcher to a bare `key` would redact `idempotency_key` — a value 004
    and 005 store as evidence on purpose. So the reporter's camelCase names are
    named by the adapter that knows the format.
    """
    # Arrange
    document = json.loads(_report(_trial("one", "pass")))
    document["config"]["apiKey"] = "sk-live-not-a-real-key"
    raw = json.dumps(document).encode("utf-8")

    # Act
    imported = read_report(raw)

    # Assert
    assert "sk-live-not-a-real-key" not in json.dumps(dict(imported.document))


def test_a_caller_policy_cannot_switch_the_adapters_own_redaction_off() -> None:
    """§20.3 lets a policy widen redaction and never narrow it.

    A caller passing their own patterns must not be able to un-redact the
    reporter's known secret fields by omitting them.
    """
    # Arrange
    document = json.loads(_report(_trial("one", "pass")))
    document["config"]["apiKey"] = "sk-live-not-a-real-key"
    raw = json.dumps(document).encode("utf-8")

    # Act
    imported = read_report(raw, policy=RedactionPolicy.from_paths(["config.somethingElse"]))

    # Assert
    assert "sk-live-not-a-real-key" not in json.dumps(dict(imported.document))
