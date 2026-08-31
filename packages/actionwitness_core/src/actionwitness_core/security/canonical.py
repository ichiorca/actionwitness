"""RFC 8785 (JCS) canonical JSON and SHA-256 content hashing.

Spec v1.9 §17.2; ADR-0004 (`docs/adr/0004-rfc-8785-canonicalization.md`) fixes the
implementation contract, and `tests/fixtures/canonicalization/rfc8785_vectors.json`
is the corpus it is judged against.

Why this is written out rather than imported: ADR-0004 records the decision and
the four alternatives it rejected. The short version is that
`json.dumps(sort_keys=True, separators=(",", ":"))` is wrong in four independent
ways - it sorts by code point rather than UTF-16 code unit, formats numbers by
Python's rules rather than ES6's, escapes non-ASCII, and accepts NaN - while
looking exactly like canonical JSON.

Every rejection here exists because the alternative is a hash that succeeds over
corrupt input. A NaN encoded as `null`, an integer rounded to a neighbouring
value, or a lone surrogate replaced during encoding would all produce a
well-formed hash describing something other than the observation it came from.

One deliberate departure from ADR-0004's letter is recorded at
`_check_integer_is_representable`.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from actionwitness_core.kernel import CanonicalizationError, CoreErrorCode, ErrorDetail, JsonValue

__all__ = [
    "MAX_CANONICAL_DEPTH",
    "canonical_text",
    "canonicalize",
    "content_hash",
    "document_content_hash",
    "sha256_hex",
]

#: Maximum nesting depth accepted from untrusted input.
#:
#: Project-allocated rather than specified: the specification bounds payload
#: *size* but not nesting, and an unbounded recursive walk over an attacker-shaped
#: document is a crash rather than a rejection (constitution §5). It also
#: terminates a reference cycle, which no size bound would.
MAX_CANONICAL_DEPTH: Final = 100

#: Escapes RFC 8785 §3.2.2.2 requires. Everything else below U+0020 becomes a
#: lower-case `\uXXXX`; solidus and non-ASCII are emitted literally.
_SHORT_ESCAPES: Final[dict[str, str]] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}

_SURROGATE_LOW: Final = 0xD800
_SURROGATE_HIGH: Final = 0xDFFF


def _fail(message: str, *, code: CoreErrorCode, location: str) -> CanonicalizationError:
    return CanonicalizationError(
        message,
        code=code,
        details=(ErrorDetail(location=location, message=message),),
    )


def _encode_string(value: str, location: str) -> str:
    if any(_SURROGATE_LOW <= ord(character) <= _SURROGATE_HIGH for character in value):
        raise _fail(
            "a lone surrogate has no UTF-8 encoding and cannot be canonicalized",
            code=CoreErrorCode.CANONICALIZATION_FAILED,
            location=location,
        )
    pieces = ['"']
    for character in value:
        escape = _SHORT_ESCAPES.get(character)
        if escape is not None:
            pieces.append(escape)
        elif character < " ":
            pieces.append(f"\\u{ord(character):04x}")
        else:
            pieces.append(character)
    pieces.append('"')
    return "".join(pieces)


def _check_integer_is_representable(value: int, location: str) -> float:
    """Refuse an integer a double cannot carry, and return it as a double.

    ADR-0004 states the rule as "integers outside ±(2^53 − 1) are rejected" and
    gives the reason: "a larger integer cannot round-trip and would canonicalize
    to a neighbouring value". The two disagree at exactly one value. 2^53 is
    exactly representable and round-trips, and the committed corpus contains it
    as the `two_pow_53` vector with an accepted canonical form, so the literal
    bound would fail a vector T4 requires to pass.

    This implements the ADR's stated *reason* - reject what cannot round-trip -
    which rejects 2^53 + 1 and every other lossy integer while accepting the
    corpus. The discrepancy is recorded in `specs/002-core-kernel/plan.md`.
    """
    as_double = float(value)
    if not math.isfinite(as_double) or int(as_double) != value:
        raise _fail(
            f"integer {value} cannot be represented exactly as an IEEE-754 double, so "
            "canonicalizing it would silently change its value",
            code=CoreErrorCode.NUMBER_NOT_REPRESENTABLE,
            location=location,
        )
    return as_double


def _encode_number(value: float, location: str) -> str:
    """Serialize a double per ECMAScript `Number::toString` (RFC 8785 §3.2.2.3)."""
    if not math.isfinite(value):
        raise _fail(
            "a non-finite number has no JSON representation and no canonical form",
            code=CoreErrorCode.NON_FINITE_NUMBER,
            location=location,
        )
    if value == 0:
        # Covers -0.0: §3.2.2.3 emits `0`, so two values that compare equal
        # produce one hash.
        return "0"
    sign = "-" if value < 0 else ""
    magnitude = abs(value)

    # `repr` gives the shortest round-tripping digits; `Decimal` then exposes them
    # as (digits, exponent) so the ES6 layout rules can be applied directly.
    _, raw_digits, exponent = Decimal(repr(magnitude)).as_tuple()
    digits = list(raw_digits)
    assert isinstance(exponent, int)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    text = "".join(str(digit) for digit in digits)
    count = len(text)
    position = exponent + count  # ES6's `n`: the decimal point's index.

    if count <= position <= 21:
        return sign + text + "0" * (position - count)
    if 0 < position <= 21:
        return sign + text[:position] + "." + text[position:]
    if -6 < position <= 0:
        return sign + "0." + "0" * -position + text
    mantissa = text if count == 1 else text[0] + "." + text[1:]
    power = position - 1
    return f"{sign}{mantissa}e{'+' if power >= 0 else '-'}{abs(power)}"


def _sort_key(key: str) -> bytes:
    """RFC 8785 §3.2.3 orders members by UTF-16 code unit, not by code point.

    Comparing big-endian UTF-16 bytes is the same comparison, unit by unit, and
    it is the only ordering that puts an astral key before a BMP key above
    U+D800. `sorted()` alone gets that pair backwards.
    """
    return key.encode("utf-16-be", errors="surrogatepass")


def _encode(value: object, location: str, depth: int) -> str:
    if depth > MAX_CANONICAL_DEPTH:
        raise _fail(
            f"nesting deeper than {MAX_CANONICAL_DEPTH} levels is refused",
            code=CoreErrorCode.RESOURCE_LIMIT_EXCEEDED,
            location=location,
        )
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return _encode_number(_check_integer_is_representable(value, location), location)
    if isinstance(value, float):
        return _encode_number(value, location)
    if isinstance(value, str):
        return _encode_string(value, location)
    if isinstance(value, Mapping):
        # Member names are checked before sorting: the sort key encodes the name,
        # so a non-string name would surface as an AttributeError from inside
        # `sorted` rather than as a structured rejection.
        for key in value:
            if not isinstance(key, str):
                raise _fail(
                    f"object member names must be strings, not {type(key).__name__}",
                    code=CoreErrorCode.UNSUPPORTED_JSON_TYPE,
                    location=location,
                )
        members = []
        for key in sorted(value, key=_sort_key):
            encoded_value = _encode(value[key], f"{location}.{key}", depth + 1)
            members.append(f"{_encode_string(key, location)}:{encoded_value}")
        return "{" + ",".join(members) + "}"
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items = [
            _encode(item, f"{location}[{index}]", depth + 1) for index, item in enumerate(value)
        ]
        return "[" + ",".join(items) + "]"
    if isinstance(value, Decimal):
        raise _fail(
            "an exact decimal must be carried as a decimal string before canonicalization; "
            "encoding it as a JSON number would round it",
            code=CoreErrorCode.UNSUPPORTED_JSON_TYPE,
            location=location,
        )
    raise _fail(
        f"{type(value).__name__} has no JSON representation",
        code=CoreErrorCode.UNSUPPORTED_JSON_TYPE,
        location=location,
    )


def canonical_text(value: JsonValue) -> str:
    """Return the RFC 8785 canonical form of `value` as text."""
    return _encode(value, "$", 0)


def canonicalize(value: JsonValue) -> bytes:
    """Return the RFC 8785 canonical form of `value` as UTF-8 bytes.

    Hashing consumes these bytes rather than a re-decoded string, so no encoding
    step sits between what was canonicalized and what was hashed.
    """
    return canonical_text(value).encode("utf-8")


def sha256_hex(payload: bytes) -> str:
    """Lower-case hex SHA-256 of `payload`."""
    return hashlib.sha256(payload).hexdigest()


def content_hash(value: JsonValue) -> str:
    """The `sha256:...` content hash of a value's canonical bytes (§17.2)."""
    return f"sha256:{sha256_hex(canonicalize(value))}"


def document_content_hash(document: Mapping[str, JsonValue]) -> str:
    """Hash a top-level artifact, excluding its own `content_hash` member.

    §17.2: an eval-case, benchmark, or JSON-report hash "covers the complete
    top-level object except its own top-level `content_hash` member". Excluding
    it here rather than at each call site is what makes the hash verifiable by a
    reader who has only the stored document.
    """
    if not isinstance(document, Mapping):
        raise _fail(
            f"a hashable artifact must be a JSON object, not {type(document).__name__}",
            code=CoreErrorCode.UNSUPPORTED_JSON_TYPE,
            location="$",
        )
    return content_hash({key: value for key, value in document.items() if key != "content_hash"})
