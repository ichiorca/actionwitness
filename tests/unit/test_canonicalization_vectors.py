"""RFC 8785 vector-corpus gates (spec §26.1; 001-preflight-baseline AC-1/ADR-0004).

The canonicalizer itself is M1 work. What ships here is the corpus it will be
judged against, and a corpus is only useful if it is right: a vector with a wrong
`expected` string either fails a correct implementation or, worse, gets "fixed" by
weakening one.

So these tests check the corpus against RFC 8785's own rules without implementing
the serializer:

* every `expected` text parses back to its `input` value (catches escaping and
  number-formatting typos);
* every object in an `expected` text has its members in UTF-16 code-unit order
  (§3.2.3) — the ordering rule a code-point sort silently gets wrong;
* no `expected` text carries insignificant whitespace (§3.2.1);
* the reject vectors really are non-finite and really are unrepresentable.

`tests/conftest.py` exposes this corpus as the `canonicalization_vectors` fixture.
"""

import json
import math
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
VECTOR_FILE = (
    REPO_ROOT / "tests" / "fixtures" / "canonicalization" / "rfc8785_vectors.json"
)

SCHEMA_VERSION = 1
NON_FINITE = {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}


def _corpus() -> dict:
    return json.loads(VECTOR_FILE.read_text(encoding="utf-8"))


def _accept() -> list[dict]:
    return _corpus()["accept"]


def _utf16_units(text: str) -> list[int]:
    """Sort key matching RFC 8785 §3.2.3 — UTF-16 code units, not code points."""
    return list(text.encode("utf-16-be"))


def _objects(value: object):
    """Yield every JSON object in a parsed tree, outermost first.

    `json.loads` preserves member order, so a parsed canonical text still carries
    the ordering the canonical text asserted.
    """
    if isinstance(value, dict):
        yield value
        for member in value.values():
            yield from _objects(member)
    elif isinstance(value, list):
        for item in value:
            yield from _objects(item)


def _whitespace_outside_strings(text: str) -> list[int]:
    """Offsets of insignificant whitespace; RFC 8785 §3.2.1 permits none."""
    offsets: list[int] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in " \t\n\r":
            offsets.append(index)
    return offsets


@pytest.mark.unit
def test_corpus_file_exists_and_is_versioned() -> None:
    assert VECTOR_FILE.is_file(), f"expected the vector corpus at {VECTOR_FILE}"
    corpus = _corpus()
    assert corpus["schema_version"] == SCHEMA_VERSION
    assert corpus["specification"] == "RFC 8785"
    assert corpus["accept"], "corpus has no accept vectors"
    assert corpus["reject"], "corpus has no reject vectors"


@pytest.mark.unit
def test_corpus_carries_both_published_and_repository_vectors() -> None:
    """AC-1 requires published vectors *and* repository fixtures, not either."""
    origins = {vector["origin"] for vector in _accept()}
    assert origins == {"published", "repository"}, (
        f"expected both published and repository origins, found {sorted(origins)}"
    )


@pytest.mark.unit
def test_vector_names_are_unique() -> None:
    corpus = _corpus()
    names = [v["name"] for v in corpus["accept"]] + [
        v["name"] for v in corpus["reject"]
    ]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"duplicate vector names: {duplicates}"


@pytest.mark.unit
@pytest.mark.parametrize("vector", _accept(), ids=lambda v: v["name"])
def test_expected_text_round_trips_to_its_input(vector: dict) -> None:
    """A canonical form is a re-encoding, never a different value."""
    assert json.loads(vector["expected"]) == vector["input"], (
        f"{vector['name']}: expected text does not parse back to its input value"
    )


@pytest.mark.parametrize("vector", _accept(), ids=lambda v: v["name"])
@pytest.mark.unit
def test_expected_members_are_in_utf16_code_unit_order(vector: dict) -> None:
    for obj in _objects(json.loads(vector["expected"])):
        keys = list(obj)
        assert keys == sorted(keys, key=_utf16_units), (
            f"{vector['name']}: members {keys} are not in UTF-16 code-unit order"
        )


@pytest.mark.unit
@pytest.mark.parametrize("vector", _accept(), ids=lambda v: v["name"])
def test_expected_text_has_no_insignificant_whitespace(vector: dict) -> None:
    offsets = _whitespace_outside_strings(vector["expected"])
    assert offsets == [], (
        f"{vector['name']}: whitespace outside strings at offsets {offsets}"
    )


@pytest.mark.unit
def test_the_ordering_vector_actually_discriminates_utf16_from_code_point() -> None:
    """Guards the corpus's most load-bearing vector against being weakened.

    An astral key starts with a high surrogate (0xD83D) and so sorts before a BMP
    key above 0xD800 by UTF-16 code unit, but after it by code point. Without a
    key pair that separates the two orderings, a code-point implementation passes.
    """
    by_name = {vector["name"]: vector for vector in _accept()}
    vector = by_name["key-ordering-utf16-code-units"]
    keys = list(json.loads(vector["expected"]))
    assert sorted(keys, key=_utf16_units) != sorted(keys), (
        "the ordering vector no longer distinguishes UTF-16 order from code-point order"
    )


@pytest.mark.unit
@pytest.mark.parametrize("vector", _corpus()["reject"], ids=lambda v: v["name"])
def test_reject_vectors_are_non_finite_and_unrepresentable(vector: dict) -> None:
    assert vector["encoding"] == "float"
    value = NON_FINITE[vector["literal"]]
    assert not math.isfinite(value), f"{vector['name']} is not a non-finite float"
    with pytest.raises(ValueError):
        json.dumps(value, allow_nan=False)
    assert vector["reason"].strip(), f"{vector['name']} states no reason"
