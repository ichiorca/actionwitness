"""Canonicalizer gates (spec v1.9 §17.2; ADR-0004; 002-T4).

`test_canonicalization_vectors.py` proves the corpus is *correct*. This module
runs the implementation against it, and then covers the rejections and the
determinism property the corpus cannot express.

The corpus is the floor, not the ceiling: eight accept vectors cannot cover the
number space, so the number rules ADR-0004 calls out as the likeliest to be got
wrong - the 1e21 and 1e-6 switch points, unpadded exponents, and `-0` - are
asserted individually as well.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path

import pytest
from actionwitness_core.kernel import CanonicalizationError, CoreErrorCode
from actionwitness_core.security.canonical import (
    MAX_CANONICAL_DEPTH,
    canonical_text,
    canonicalize,
    content_hash,
    document_content_hash,
    sha256_hex,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VECTOR_FILE = REPO_ROOT / "tests" / "fixtures" / "canonicalization" / "rfc8785_vectors.json"


def _corpus() -> dict:
    return json.loads(VECTOR_FILE.read_text(encoding="utf-8"))


# --- the committed corpus ---------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("vector", _corpus()["accept"], ids=lambda v: v["name"])
def test_every_accept_vector_canonicalizes_to_its_expected_text(vector: dict) -> None:
    """AC-2: published vectors *and* repository fixture vectors pass."""
    assert canonical_text(vector["input"]) == vector["expected"], vector["description"]


@pytest.mark.unit
@pytest.mark.parametrize("vector", _corpus()["accept"], ids=lambda v: v["name"])
def test_canonical_bytes_are_utf8_of_the_expected_text(vector: dict) -> None:
    """Hashing consumes bytes; a mismatch here would hash a different document."""
    assert canonicalize(vector["input"]) == vector["expected"].encode("utf-8")


@pytest.mark.unit
def test_the_corpus_covers_both_published_and_repository_origins() -> None:
    """A green run against repository vectors alone would not satisfy AC-2."""
    assert {vector["origin"] for vector in _corpus()["accept"]} == {"published", "repository"}


# --- number formatting (RFC 8785 §3.2.2.3) ----------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (-0.0, "0"),
        (0.0, "0"),
        (1, "1"),
        (-1, "-1"),
        (4.5, "4.5"),
        (-1.5, "-1.5"),
        (0.002, "0.002"),
        (1e-6, "0.000001"),
        (1e-7, "1e-7"),
        (1e-27, "1e-27"),
        (1e20, "100000000000000000000"),
        (1e21, "1e+21"),
        (1e30, "1e+30"),
        (333333333.3333333, "333333333.3333333"),
        (9007199254740991, "9007199254740991"),
        (9007199254740992, "9007199254740992"),
        (10**21, "1e+21"),
        (1.5e300, "1.5e+300"),
    ],
)
def test_numbers_follow_the_es6_layout_rules(value: object, expected: str) -> None:
    assert canonical_text(value) == expected


@pytest.mark.unit
def test_minus_zero_and_zero_hash_identically() -> None:
    """Two values that compare equal must not produce two evidence hashes."""
    assert content_hash({"total": -0.0}) == content_hash({"total": 0})


@pytest.mark.unit
def test_an_exponent_is_never_zero_padded() -> None:
    """Python's `repr` writes `1e-05`; ES6 writes `1e-5`, and the hash differs."""
    assert canonical_text(1e-7) == "1e-7"
    assert "e-07" not in canonical_text(1e-7)


@pytest.mark.unit
def test_a_large_integer_switches_to_exponential_like_es6() -> None:
    """`str(10**21)` is not the canonical form; the integer path must not shortcut."""
    assert canonical_text(10**21) != str(10**21)
    assert canonical_text(10**21) == "1e+21"


@pytest.mark.unit
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_refused(value: float) -> None:
    """§17.2: NaN and ±Infinity are rejected before serialization."""
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_text({"total": value})
    assert excinfo.value.code is CoreErrorCode.NON_FINITE_NUMBER


@pytest.mark.unit
@pytest.mark.parametrize("value", _corpus()["reject"], ids=lambda v: v["name"])
def test_every_reject_vector_is_refused(value: dict) -> None:
    literal = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[value["literal"]]
    with pytest.raises(CanonicalizationError):
        canonicalize(literal)


@pytest.mark.unit
@pytest.mark.parametrize("value", [2**53 + 1, -(2**53) - 1, 2**70 + 1])
def test_an_integer_a_double_cannot_carry_is_refused(value: int) -> None:
    """ADR-0004: silent precision loss inside an evidence hash is the worse outcome."""
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_text(value)
    assert excinfo.value.code is CoreErrorCode.NUMBER_NOT_REPRESENTABLE


@pytest.mark.unit
def test_two_pow_53_is_accepted_because_it_round_trips() -> None:
    """The corpus requires it, and the ADR's rationale is round-trip fidelity.

    Documented as a deviation from ADR-0004's literal ±(2^53 − 1) bound in
    `specs/002-core-kernel/plan.md`.
    """
    assert int(float(2**53)) == 2**53
    assert canonical_text(2**53) == "9007199254740992"


# --- strings (RFC 8785 §3.2.2.2) --------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        ("a/b", '"a/b"'),
        ("\\u0000\\u0001\\u001f", '"\\\\u0000\\\\u0001\\\\u001f"'),
        ("\b\f\n\r\t", '"\\b\\f\\n\\r\\t"'),
        ('"\\', '"\\"\\\\"'),
        ("café", '"café"'),
        ("", '""'),
    ],
)
def test_only_quote_backslash_and_c0_controls_are_escaped(value: str, expected: str) -> None:
    """Over-escaping is the common failure and it changes the hash."""
    assert canonical_text(value) == expected


@pytest.mark.unit
def test_control_escapes_use_lower_case_hex() -> None:
    assert canonical_text("\\u001f") == '"\\\\u001f"'
    assert canonical_text("\\u001f") != '"\\\\u001F"'


@pytest.mark.unit
def test_a_lone_surrogate_is_refused_rather_than_replaced() -> None:
    """Encoding with a replacement character would hash a document nobody wrote."""
    with pytest.raises(CanonicalizationError, match="lone surrogate"):
        canonical_text({"text": "\ud800"})


# --- object member ordering (RFC 8785 §3.2.3) -------------------------------


@pytest.mark.unit
def test_members_sort_by_utf16_code_unit_not_code_point() -> None:
    """The astral/BMP pair is where a `sorted()` implementation gets it backwards."""
    payload = {"\U0001f600": 1, "ﬀ": 2}
    assert list(json.loads(canonical_text(payload))) == ["\U0001f600", "ﬀ"]
    assert sorted(payload) == ["ﬀ", "\U0001f600"]


@pytest.mark.unit
def test_member_order_in_the_input_does_not_change_the_output() -> None:
    forward = canonical_text({"a": 1, "b": 2, "c": 3})
    reversed_input = canonical_text({"c": 3, "b": 2, "a": 1})
    assert forward == reversed_input


@pytest.mark.unit
def test_array_order_is_preserved() -> None:
    """RFC 8785 sorts object members; array order is data, not presentation."""
    assert canonical_text([3, 1, 2]) == "[3,1,2]"


@pytest.mark.unit
def test_a_non_string_member_name_is_refused() -> None:
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_text({1: "one"})
    assert excinfo.value.code is CoreErrorCode.UNSUPPORTED_JSON_TYPE


# --- unsupported types and bounds -------------------------------------------


@pytest.mark.unit
def test_a_decimal_is_refused_with_an_actionable_message() -> None:
    """Money is carried as a decimal string; encoding it as a number would round it."""
    with pytest.raises(CanonicalizationError, match="decimal string"):
        canonical_text({"total": Decimal("20.00")})


@pytest.mark.unit
@pytest.mark.parametrize("value", [b"bytes", {1, 2}, object()])
def test_a_non_json_type_is_refused(value: object) -> None:
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_text(value)
    assert excinfo.value.code is CoreErrorCode.UNSUPPORTED_JSON_TYPE


@pytest.mark.unit
def test_nesting_beyond_the_depth_bound_is_refused_rather_than_crashing() -> None:
    """Untrusted input must not be able to turn a walk into a RecursionError."""
    payload: object = "leaf"
    for _ in range(MAX_CANONICAL_DEPTH + 2):
        payload = [payload]
    with pytest.raises(CanonicalizationError) as excinfo:
        canonical_text(payload)
    assert excinfo.value.code is CoreErrorCode.RESOURCE_LIMIT_EXCEEDED


@pytest.mark.unit
def test_a_reference_cycle_terminates_as_a_rejection() -> None:
    cyclic: dict = {}
    cyclic["self"] = cyclic
    with pytest.raises(CanonicalizationError):
        canonical_text(cyclic)


@pytest.mark.unit
def test_no_insignificant_whitespace_is_emitted() -> None:
    text = canonical_text({"b": [1, 2], "a": {"c": "d e"}})
    assert text == '{"a":{"c":"d e"},"b":[1,2]}'


# --- hashing ----------------------------------------------------------------


@pytest.mark.unit
def test_content_hash_is_sha256_over_the_canonical_bytes() -> None:
    payload = {"provider": "fake_state", "state": {"ticket": {"status": "open"}}}
    assert content_hash(payload) == f"sha256:{sha256_hex(canonicalize(payload))}"
    assert content_hash(payload).startswith("sha256:")


@pytest.mark.unit
def test_identical_inputs_produce_byte_identical_output_and_hash() -> None:
    """Determinism is the property every immutable record depends on."""
    payload = {"z": [1, {"b": None, "a": True}], "y": "x"}
    assert canonicalize(payload) == canonicalize(dict(reversed(list(payload.items()))))
    assert content_hash(payload) == content_hash(payload)


@pytest.mark.unit
def test_a_changed_value_changes_the_hash() -> None:
    assert content_hash({"total": "20.00"}) != content_hash({"total": "20.01"})


@pytest.mark.unit
def test_a_document_hash_excludes_its_own_content_hash_member() -> None:
    """§17.2: the artifact hash covers everything except the hash field itself."""
    document = {"schema_version": "1.0", "run_id": "run_1"}
    expected = content_hash(document)
    assert document_content_hash(document) == expected
    assert document_content_hash({**document, "content_hash": "sha256:stale"}) == expected


@pytest.mark.unit
def test_a_document_hash_still_covers_a_nested_content_hash() -> None:
    """Only the *top-level* member is excluded; a nested one is real content."""
    with_nested = {"contract": {"content_hash": "sha256:abc"}}
    without_nested = {"contract": {}}
    assert document_content_hash(with_nested) != document_content_hash(without_nested)


@pytest.mark.unit
def test_a_non_object_artifact_is_refused() -> None:
    with pytest.raises(CanonicalizationError):
        document_content_hash([1, 2, 3])  # type: ignore[arg-type]
