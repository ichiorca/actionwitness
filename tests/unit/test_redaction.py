"""Redaction and bounded-summary gates (spec v1.9 §11.4, §20.3, FR-075; 002-T5).

The assertion that matters most is the last section's: a secret must not survive
into canonical bytes. Everything else here supports it - the default keys, the
glob grammar, the walk over arrays - but a redactor that is correct in isolation
and applied after hashing has removed nothing, because the stored hash still
describes the document that held the secret.

The over-redaction tests are equally deliberate. A default key rule that matched
by substring would redact `cardinality` and `tokens_remaining`, and evidence that
quietly loses ordinary business data is a different failure with the same shape.
"""

from __future__ import annotations

import pytest
from actionwitness_core.kernel import CoreErrorCode, PathError
from actionwitness_core.security.canonical import canonical_text, content_hash
from actionwitness_core.security.limits import (
    MAX_FINDING_VALUE_CHARS,
    MAX_TOOL_RESULT_CHARS,
    TRUNCATION_MARKER,
    ResourceLimits,
    bounded_summary,
)
from actionwitness_core.security.redaction import (
    REDACTED,
    RedactionPattern,
    RedactionPolicy,
    redact,
)

SECRET = "sk-live-do-not-store-this"


# --- default keys (§20.3) ---------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "key",
    [
        "password",
        "secret",
        "token",
        "authorization",
        "cookie",
        "api_key",
        "access_token",
        "cart_token",
        "email",
        "payment",
        "card",
    ],
)
def test_every_default_key_is_redacted(key: str) -> None:
    assert redact({key: SECRET}) == {key: REDACTED}


@pytest.mark.unit
@pytest.mark.parametrize("key", ["Password", "API_KEY", "Cart_Token", "EMAIL", "Cookie"])
def test_default_keys_match_case_insensitively(key: str) -> None:
    assert redact({key: SECRET}) == {key: REDACTED}


@pytest.mark.unit
@pytest.mark.parametrize(
    "key", ["customer_email", "payment_token", "paymentToken", "billing-card", "authToken"]
)
def test_a_compound_key_containing_a_default_word_is_redacted(key: str) -> None:
    """`customer_email` must not survive because no contract happened to name it."""
    assert redact({key: SECRET}) == {key: REDACTED}


@pytest.mark.unit
@pytest.mark.parametrize(
    "key", ["cardinality", "discard", "tokens_remaining", "emails_sent", "secretary", "total"]
)
def test_ordinary_business_keys_are_not_over_redacted(key: str) -> None:
    """A substring rule would eat all of these; matching is by whole word."""
    assert redact({key: "keep me"}) == {key: "keep me"}


@pytest.mark.unit
def test_a_redacted_key_removes_its_whole_subtree() -> None:
    """Descending into a redacted object would leak the values inside it."""
    payload = {"payment": {"card": {"pan": "4111", "expiry": "12/30"}}}
    assert redact(payload) == {"payment": REDACTED}


@pytest.mark.unit
def test_defaults_apply_at_every_depth_and_inside_arrays() -> None:
    payload = {
        "target": {
            "customers": [
                {"id": "c1", "email": "a@example.test"},
                {"id": "c2", "email": "b@example.test"},
            ]
        }
    }
    assert redact(payload) == {
        "target": {"customers": [{"id": "c1", "email": REDACTED}, {"id": "c2", "email": REDACTED}]}
    }


# --- the contract glob grammar (§20.3) --------------------------------------


@pytest.mark.unit
def test_a_double_star_matches_zero_or_more_segments() -> None:
    pattern = RedactionPattern.parse("**.note")
    assert pattern.matches(("note",))
    assert pattern.matches(("target", "note"))
    assert pattern.matches(("target", "cart", "0", "note"))
    assert not pattern.matches(("target", "notes"))


@pytest.mark.unit
def test_a_single_star_matches_exactly_one_segment() -> None:
    pattern = RedactionPattern.parse("target.*.note")
    assert pattern.matches(("target", "cart", "note"))
    assert not pattern.matches(("target", "note"))
    assert not pattern.matches(("target", "cart", "items", "note"))


@pytest.mark.unit
def test_a_single_star_matches_an_array_index() -> None:
    """§20.3: `*` matches exactly one object key *or array index*."""
    policy = RedactionPolicy.from_paths(["target.lines.*.note"])
    payload = {"target": {"lines": [{"note": SECRET}, {"note": SECRET}]}}
    assert redact(payload, policy) == {"target": {"lines": [{"note": REDACTED}] * 2}}


@pytest.mark.unit
def test_a_literal_pattern_selects_exactly_one_path() -> None:
    policy = RedactionPolicy.from_paths(["target.cart.note"])
    payload = {"target": {"cart": {"note": SECRET, "total": "20.00"}, "note": "keep"}}
    assert redact(payload, policy) == {
        "target": {"cart": {"note": REDACTED, "total": "20.00"}, "note": "keep"}
    }


@pytest.mark.unit
def test_contract_patterns_add_to_the_defaults_rather_than_replacing_them() -> None:
    """§20.3: "applied in addition to defaults"."""
    policy = RedactionPolicy.from_paths(["target.note"])
    payload = {"target": {"note": SECRET, "password": SECRET, "total": "20.00"}}
    assert redact(payload, policy) == {
        "target": {"note": REDACTED, "password": REDACTED, "total": "20.00"}
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "",
        ".",
        "target..note",
        "target.",
        "target.no*te",
        "target.***",
        "$.target.note",
        "target.note[0]",
        "^target\\.note$",
        "target.(note|memo)",
        "target.no te",
    ],
)
def test_regex_and_malformed_patterns_are_refused(text: str) -> None:
    """A pattern that can execute is an injection point, not a filter."""
    with pytest.raises(PathError) as excinfo:
        RedactionPattern.parse(text)
    assert excinfo.value.code is CoreErrorCode.INVALID_REDACTION_PATTERN


@pytest.mark.unit
def test_the_specs_own_example_patterns_parse_and_apply() -> None:
    policy = RedactionPolicy.from_paths(["**.email", "**.payment_token"])
    payload = {"a": {"b": {"email": SECRET}}, "payment_token": SECRET, "kept": 1}
    assert redact(payload, policy) == {
        "a": {"b": {"email": REDACTED}},
        "payment_token": REDACTED,
        "kept": 1,
    }


# --- purity and determinism -------------------------------------------------


@pytest.mark.unit
def test_redaction_does_not_mutate_its_input() -> None:
    """A caller that hashed the object it redacted in place would get two answers."""
    payload = {"target": {"password": SECRET}}
    redact(payload)
    assert payload == {"target": {"password": SECRET}}


@pytest.mark.unit
def test_redaction_is_idempotent() -> None:
    payload = {"target": {"password": SECRET, "total": "20.00"}}
    once = redact(payload)
    assert redact(once) == once


@pytest.mark.unit
def test_redaction_preserves_everything_it_does_not_select() -> None:
    payload = {
        "target": {
            "cart": {"items": [{"sku": "mug", "quantity": 1}], "total": "20.00", "discount": None},
            "order": {"created": False},
        }
    }
    assert redact(payload) == payload


# --- redaction happens before hashing (FR-075) ------------------------------


@pytest.mark.unit
def test_a_redacted_value_never_reaches_the_canonical_bytes() -> None:
    """The load-bearing assertion: what is hashed is what was redacted."""
    payload = {"target": {"customer": {"email": "shopper@example.test"}}, "token": SECRET}
    text = canonical_text(redact(payload))
    assert SECRET not in text
    assert "shopper@example.test" not in text
    assert text.count(REDACTED) == 2


@pytest.mark.unit
def test_two_documents_differing_only_in_redacted_values_hash_identically() -> None:
    """Proves the secret is gone before hashing, not merely absent from display."""
    first = {"target": {"total": "20.00"}, "api_key": "key-one"}
    second = {"target": {"total": "20.00"}, "api_key": "key-two"}
    assert content_hash(redact(first)) == content_hash(redact(second))


@pytest.mark.unit
def test_a_changed_business_value_still_changes_the_redacted_hash() -> None:
    """Redaction must not flatten the document into something that cannot differ."""
    first = {"target": {"total": "20.00"}, "api_key": "key-one"}
    second = {"target": {"total": "25.00"}, "api_key": "key-one"}
    assert content_hash(redact(first)) != content_hash(redact(second))


# --- bounded summaries (§11.4, §23.3) ---------------------------------------


@pytest.mark.unit
def test_a_short_value_is_returned_unchanged_and_unmarked() -> None:
    summary = bounded_summary("cart updated", MAX_TOOL_RESULT_CHARS)
    assert summary.text == "cart updated"
    assert summary.truncated is False
    assert summary.original_length == len("cart updated")


@pytest.mark.unit
def test_a_long_value_is_cut_within_budget_and_marked() -> None:
    """A silently truncated value reads as a complete one."""
    summary = bounded_summary("x" * 5_000, MAX_TOOL_RESULT_CHARS)
    assert summary.truncated is True
    assert summary.original_length == 5_000
    assert len(summary.text) == MAX_TOOL_RESULT_CHARS
    assert summary.text.endswith(TRUNCATION_MARKER)


@pytest.mark.unit
def test_the_marker_is_counted_inside_the_budget_not_added_to_it() -> None:
    """A marker that pushed the result over its limit would defeat the limit."""
    for limit in (MAX_FINDING_VALUE_CHARS, MAX_TOOL_RESULT_CHARS, 20):
        assert len(bounded_summary("y" * 10_000, limit).text) <= limit


@pytest.mark.unit
def test_a_budget_smaller_than_the_marker_still_stays_within_budget() -> None:
    summary = bounded_summary("z" * 100, 3)
    assert len(summary.text) == 3
    assert summary.truncated is True
    assert summary.original_length == 100


@pytest.mark.unit
def test_a_value_exactly_at_the_budget_is_not_marked() -> None:
    summary = bounded_summary("a" * MAX_FINDING_VALUE_CHARS, MAX_FINDING_VALUE_CHARS)
    assert summary.truncated is False
    assert summary.text == "a" * MAX_FINDING_VALUE_CHARS


@pytest.mark.unit
def test_a_negative_budget_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="negative"):
        bounded_summary("anything", -1)


# --- resource limits --------------------------------------------------------


@pytest.mark.unit
def test_the_specified_tool_budgets_are_the_defaults() -> None:
    limits = ResourceLimits()
    assert limits.max_tool_result_chars == 1_500
    assert limits.max_findings_result_chars == 4_000
    assert limits.max_finding_value_chars == 120
    assert limits.max_annotation_chars == 500


@pytest.mark.unit
def test_an_unset_counting_cap_enforces_nothing_rather_than_defaulting_to_a_guess() -> None:
    """The core reads no configuration; an unset cap means the caller has not set one."""
    limits = ResourceLimits()
    assert limits.exceeds_events(10_000_000) is False
    assert limits.exceeds_payload(10_000_000) is False


@pytest.mark.unit
def test_a_configured_cap_is_enforced_at_its_boundary() -> None:
    limits = ResourceLimits(max_events_per_run=3, max_payload_bytes=1_024)
    assert limits.exceeds_events(3) is False
    assert limits.exceeds_events(4) is True
    assert limits.exceeds_payload(1_024) is False
    assert limits.exceeds_payload(1_025) is True


@pytest.mark.unit
def test_resource_limits_are_immutable() -> None:
    from pydantic import ValidationError

    limits = ResourceLimits()
    with pytest.raises(ValidationError):
        limits.max_tool_result_chars = 999_999
