"""Restricted dotted observation paths: parse, validate, resolve exactly.

Spec v1.9 §9.3 (provider payloads mounted under an adapter-declared namespace),
§10.2 ("a restricted dotted-key grammar such as `target.cart.items.mug.quantity`;
JSONPath expressions, filters, executable expressions, and wildcards are not
allowed"), §10.4 (200-character limit), §12.6/FR-051 (a missing path is a
structured mismatch, never an unhandled exception), §13.4 (overlap at a
dotted-key boundary).

The grammar is restricted because a path is untrusted input that selects data.
An expression language here would turn a contract into a program the harness
executes on behalf of whoever wrote it, and a wildcard would make an assertion
match a value its author never saw. So a path is a fixed list of literal keys and
nothing else: no `*`, no `$`, no `[0]`, no filters, no traversal that reaches a
Python attribute.

Resolution walks only mappings and sequences. It never calls `getattr`, so a
segment like `__class__` is an ordinary key that simply is not there.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Final

from pydantic import PlainSerializer, PlainValidator, WithJsonSchema

from actionwitness_core.contracts.limits import MAX_OBSERVATION_PATH_LENGTH
from actionwitness_core.kernel import CoreErrorCode, ErrorDetail, JsonValue, PathError

__all__ = [
    "MISSING",
    "ObservationPath",
    "ObservationPathField",
    "Resolution",
    "resolve",
]

#: One path segment: an identifier-shaped object key, or a non-negative integer
#: that may index a sequence. Leading zeros are refused so that `01` and `1`
#: cannot name the same element through two spellings.
_SEGMENT: Final = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_-]*|0|[1-9][0-9]*)$")

#: Characters whose presence identifies an expression language rather than a
#: typo, reported by name so the author is told what was rejected and why.
_FORBIDDEN_CONSTRUCTS: Final[tuple[tuple[str, str], ...]] = (
    ("*", "wildcards are not allowed in an observation path"),
    ("$", "JSONPath expressions are not allowed"),
    ("[", "bracket indexing is not allowed; use a dotted integer segment"),
    ("]", "bracket indexing is not allowed; use a dotted integer segment"),
    ("(", "filters and function calls are not allowed"),
    (")", "filters and function calls are not allowed"),
    ("@", "JSONPath context references are not allowed"),
    ("?", "filter expressions are not allowed"),
    ("'", "quoted keys are not allowed"),
    ('"', "quoted keys are not allowed"),
    ("\\", "escapes are not allowed"),
    ("/", "slash-separated pointers are not allowed"),
    (":", "slicing is not allowed"),
    (" ", "an observation path contains no whitespace"),
)


def _reject(text: str, message: str) -> PathError:
    return PathError(
        f"invalid observation path {text!r}: {message}",
        code=CoreErrorCode.INVALID_OBSERVATION_PATH,
        details=(ErrorDetail(location=text, message=message),),
    )


@dataclass(frozen=True, slots=True, order=True)
class ObservationPath:
    """A validated dotted path into the evaluation context.

    Ordering is by segment tuple, which makes lexicographic ordering by canonical
    path (§17.2, §23) a plain sort rather than a convention each call site has to
    remember.
    """

    segments: tuple[str, ...]

    @classmethod
    def parse(cls, text: object) -> ObservationPath:
        """Validate `text` and return the parsed path, or raise `PathError`."""
        if not isinstance(text, str):
            raise PathError(
                f"an observation path must be a string, not {type(text).__name__}",
                code=CoreErrorCode.INVALID_OBSERVATION_PATH,
            )
        if not text:
            raise _reject(text, "an observation path may not be empty")
        if len(text) > MAX_OBSERVATION_PATH_LENGTH:
            raise _reject(
                text,
                f"an observation path may be at most {MAX_OBSERVATION_PATH_LENGTH} characters",
            )
        for construct, message in _FORBIDDEN_CONSTRUCTS:
            if construct in text:
                raise _reject(text, message)
        if any(character < " " or character == "\x7f" for character in text):
            raise _reject(text, "control characters are not allowed")

        raw_segments = text.split(".")
        for segment in raw_segments:
            if not segment:
                raise _reject(text, "an empty segment (leading, trailing, or doubled dot)")
            if not _SEGMENT.match(segment):
                raise _reject(
                    text,
                    f"segment {segment!r} is not an identifier-shaped key or a "
                    "non-negative integer",
                )
        return cls(tuple(raw_segments))

    @property
    def namespace(self) -> str:
        """The provider namespace this path is mounted under (§9.3)."""
        return self.segments[0]

    def is_ancestor_of(self, other: ObservationPath) -> bool:
        """True when `other` lies under this path at a dotted-key boundary.

        Segment-wise comparison rather than string prefixing, because
        `target.cart` textually prefixes `target.cartridge` while naming an
        unrelated value (§13.4).
        """
        return len(self.segments) <= len(other.segments) and (
            other.segments[: len(self.segments)] == self.segments
        )

    def overlaps(self, other: ObservationPath) -> bool:
        """True when either path is an ancestor of the other (§13.4)."""
        return self.is_ancestor_of(other) or other.is_ancestor_of(self)

    def __str__(self) -> str:
        return ".".join(self.segments)

    def __len__(self) -> int:
        return len(self.segments)


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of resolving one path.

    `found` is separate from `value` because §9.4 makes the distinction load
    bearing: `exists` passes on a present `null`, and `absent` fails on one. A
    resolver that returned `None` for both would make the two operators
    indistinguishable.
    """

    found: bool
    value: JsonValue = None


#: The single "no such path" result. Shared because it carries no information
#: beyond its absence.
MISSING: Final = Resolution(found=False, value=None)


def resolve(path: ObservationPath, context: Mapping[str, JsonValue]) -> Resolution:
    """Resolve `path` against a namespace-keyed evaluation context.

    Returns `MISSING` rather than raising when the path does not resolve
    (FR-051). The walk is exact:

    * a mapping is indexed by the literal segment text;
    * a list or tuple is indexed by an integer segment, and only within range;
    * a string is never indexed, so `target.name.0` does not silently yield a
      character;
    * anything else terminates the walk as unresolved.
    """
    current: JsonValue = context
    for segment in path.segments:
        if isinstance(current, Mapping):
            if segment not in current:
                return MISSING
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes):
            if not segment.isdigit():
                return MISSING
            index = int(segment)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return Resolution(found=True, value=current)


def _coerce_path(value: object) -> ObservationPath:
    return value if isinstance(value, ObservationPath) else ObservationPath.parse(value)


#: A path as a Pydantic field: validated from the dotted text on the way in and
#: serialized back to that exact text on the way out.
#:
#: Shared rather than redeclared per model. Contracts, tool specs, and policy
#: waivers all carry paths, and a second declaration would eventually validate
#: them by a second rule - which is how one surface starts accepting a path
#: another rejects.
type ObservationPathField = Annotated[
    ObservationPath,
    PlainValidator(_coerce_path),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string", "description": "Restricted dotted observation path (§10.2)"}),
]
