"""Default and contract-specified redaction, applied before hashing or storage.

Spec v1.9 §20.3 (default keys, the dotted glob grammar, "redaction walks both
objects and arrays before persistence, hashing, or export"), FR-075 (reports
apply configured and default redaction before persistence or export);
constitution §4 (secrets are forbidden in databases, evidence bundles, fixtures,
logs, and regression artifacts).

Ordering is the whole point. Redaction that runs after hashing produces an
evidence chain whose hashes describe the unredacted document, so verifying a
stored artifact would require the secret it was redacted to remove. Redaction
that runs after persistence has already written the secret to disk. So the
redacted document is the *only* document that continues downstream:
`redact` returns a new value and every hashing, storage, and export path takes
its input from there.

The glob grammar is separate from the assertion-path grammar of §10.2 on purpose.
Assertion paths select a value to judge and must be exact; redaction patterns
select values to remove and are safer when they over-match, which is why `*` and
`**` exist here and nowhere else.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from actionwitness_core.contracts.limits import MAX_OBSERVATION_PATH_LENGTH
from actionwitness_core.kernel import CoreErrorCode, CoreModel, ErrorDetail, JsonValue, PathError

__all__ = [
    "DEFAULT_REDACTION_KEYS",
    "REDACTED",
    "RedactionPattern",
    "RedactionPolicy",
    "redact",
]

#: The placeholder a removed value is replaced with. A marker rather than a
#: deletion so that a reader can see that something was removed - a silently
#: absent field is indistinguishable from a field that never existed.
REDACTED: Final = "[REDACTED]"

#: §20.3's default keys, matched case-insensitively.
DEFAULT_REDACTION_KEYS: Final[frozenset[str]] = frozenset(
    {
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
    }
)

#: Single-word defaults are additionally matched against each word of a compound
#: key, so `customer_email` and `paymentToken` are covered without a contract
#: naming them. Matching is by whole word, not substring: `cardinality`,
#: `discard`, and `tokens_remaining` are ordinary data and must survive, and a
#: substring rule would redact all three.
_WORD_DEFAULTS: Final[frozenset[str]] = frozenset(
    {"password", "secret", "token", "authorization", "cookie", "email", "payment", "card"}
)

_WORD_BOUNDARY: Final = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")

_LITERAL_SEGMENT: Final = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*)$")


def _words(key: str) -> set[str]:
    return {part.lower() for part in _WORD_BOUNDARY.split(key) if part}


def _is_default_redacted_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in DEFAULT_REDACTION_KEYS or bool(_words(key) & _WORD_DEFAULTS)


@dataclass(frozen=True, slots=True)
class RedactionPattern:
    """One `**.email`-style pattern (spec §20.3).

    `*` matches exactly one object key or array index; `**` matches zero or more
    segments. Regular expressions and executable selectors are refused - a
    pattern is a filter, and a filter that can execute is an injection point.
    """

    segments: tuple[str, ...]

    @classmethod
    def parse(cls, text: object) -> RedactionPattern:
        if not isinstance(text, str):
            raise PathError(
                f"a redaction pattern must be a string, not {type(text).__name__}",
                code=CoreErrorCode.INVALID_REDACTION_PATTERN,
            )
        if not text:
            raise cls._reject(text, "a redaction pattern may not be empty")
        if len(text) > MAX_OBSERVATION_PATH_LENGTH:
            raise cls._reject(
                text,
                f"a redaction pattern may be at most {MAX_OBSERVATION_PATH_LENGTH} characters",
            )
        segments = text.split(".")
        for segment in segments:
            if segment in {"*", "**"}:
                continue
            if not segment:
                raise cls._reject(text, "an empty segment (leading, trailing, or doubled dot)")
            if not _LITERAL_SEGMENT.match(segment):
                raise cls._reject(
                    text,
                    f"segment {segment!r} is neither a literal key, `*`, nor `**`; "
                    "regular expressions and executable selectors are not accepted",
                )
        return cls(tuple(segments))

    @staticmethod
    def _reject(text: str, message: str) -> PathError:
        return PathError(
            f"invalid redaction pattern {text!r}: {message}",
            code=CoreErrorCode.INVALID_REDACTION_PATTERN,
            details=(ErrorDetail(location=text, message=message),),
        )

    def matches(self, path: tuple[str, ...]) -> bool:
        """True when this pattern selects the value at `path`."""
        return _match(self.segments, path)

    def __str__(self) -> str:
        return ".".join(self.segments)


def _match(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """Glob match with `**` spanning zero or more segments.

    Written as an explicit two-index walk rather than a translated regular
    expression: compiling untrusted pattern text into a regex is how a redaction
    rule becomes a denial-of-service, and the recursion here is bounded by the
    pattern's own length.
    """
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_match(rest, path[index:]) for index in range(len(path) + 1))
    if not path:
        return False
    if head != "*" and head != path[0]:
        return False
    return _match(rest, path[1:])


class RedactionPolicy(CoreModel):
    """The default keys plus a contract's own patterns (spec §20.3).

    "Contract-specific redaction paths are applied in addition to defaults", so
    a contract can widen redaction and never narrow it: `apply_defaults` exists
    for the one caller that redacts an already-redacted document, not as a way
    for a contract to switch the defaults off.
    """

    patterns: tuple[RedactionPattern, ...] = ()
    apply_defaults: bool = True

    @classmethod
    def from_paths(cls, paths: Sequence[str]) -> RedactionPolicy:
        """Build a policy from a contract's `redaction.paths` list."""
        return cls(patterns=tuple(RedactionPattern.parse(path) for path in paths))

    def selects(self, path: tuple[str, ...], *, is_object_key: bool) -> bool:
        if not path:
            return False
        if self.apply_defaults and is_object_key and _is_default_redacted_key(path[-1]):
            return True
        return any(pattern.matches(path) for pattern in self.patterns)


def redact(value: JsonValue, policy: RedactionPolicy | None = None) -> JsonValue:
    """Return a copy of `value` with every selected value replaced by `REDACTED`.

    Pure: the input is never mutated, so a caller cannot accidentally hash the
    same object it redacted in place and get a different answer depending on
    ordering. A selected value is replaced whole - redacting a key whose value is
    an object removes the subtree rather than descending into it.
    """
    return _walk(value, policy or RedactionPolicy(), ())


def _walk(value: JsonValue, policy: RedactionPolicy, path: tuple[str, ...]) -> JsonValue:
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if policy.selects((*path, str(key)), is_object_key=True)
                else _walk(item, policy, (*path, str(key)))
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            (
                REDACTED
                if policy.selects((*path, str(index)), is_object_key=False)
                else _walk(item, policy, (*path, str(index)))
            )
            for index, item in enumerate(value)
        ]
    return value
