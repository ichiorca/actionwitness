"""Shared kernel: the base model, structured errors, and determinism seams.

Spec v1.9 §10.2 (Pydantic model requirements), §17.2 (canonical hashing inputs),
constitution §1 (exact `Decimal` money, timezone-aware UTC instants, injected
clock/identifier/randomness) and §5 (untrusted input, fail closed).

Everything downstream of this module is built on three decisions made here:

1. **`CoreModel` is frozen and forbids unknown fields.** A record that can be
   mutated after construction cannot be hash-linked evidence, and a model that
   silently accepts an unknown key turns a typo in untrusted input into data.
2. **Money is `Decimal`, never `float`.** A float total is wrong before it is
   ever hashed, so `parse_decimal` refuses one outright rather than rounding it.
3. **Time, identity, and randomness arrive through protocols.** Replay is a
   product requirement (AC-12/AC-15); a `datetime.now()` inside an evaluation
   path would make a report unreproducible and the defect would surface a
   milestone later, in CI, as flake.

This module is deliberately a flat module rather than a subpackage: spec §18
fixes the subpackage list, and the shared base of all of them belongs beside
them, not inside one of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, BeforeValidator, ConfigDict, PlainSerializer

__all__ = [
    "CanonicalizationError",
    "Clock",
    "ContractError",
    "CoreError",
    "CoreErrorCode",
    "CoreModel",
    "ErrorDetail",
    "EvaluationError",
    "IdentifierSource",
    "JsonValue",
    "LimitError",
    "Money",
    "PathError",
    "RandomSource",
    "TransitionError",
    "UtcInstant",
    "format_decimal",
    "format_instant",
    "parse_decimal",
    "require_utc",
]

#: Any value that survives a round trip through canonical JSON (§17.2). Money is
#: carried as a decimal *string* inside this union by design - see `Money`.
type JsonValue = None | bool | int | float | str | Sequence["JsonValue"] | Mapping[str, "JsonValue"]


class CoreErrorCode(StrEnum):
    """Stable, target-neutral failure codes raised by the core.

    These carry no HTTP status: mapping a domain failure onto a response is the
    service's job (see `actionwitness_service.api.errors`), and putting a status
    here would drag a transport concern into a library that must install alone.
    """

    CANONICALIZATION_FAILED = "CANONICALIZATION_FAILED"
    NON_FINITE_NUMBER = "NON_FINITE_NUMBER"
    NUMBER_NOT_REPRESENTABLE = "NUMBER_NOT_REPRESENTABLE"
    UNSUPPORTED_JSON_TYPE = "UNSUPPORTED_JSON_TYPE"
    INVALID_OBSERVATION_PATH = "INVALID_OBSERVATION_PATH"
    INVALID_REDACTION_PATTERN = "INVALID_REDACTION_PATTERN"
    CONTRACT_VALIDATION_FAILED = "CONTRACT_VALIDATION_FAILED"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    RESOURCE_LIMIT_EXCEEDED = "RESOURCE_LIMIT_EXCEEDED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    EVALUATION_INPUT_INVALID = "EVALUATION_INPUT_INVALID"


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """One machine-readable reason, addressed to a location in the input.

    `location` is a dotted or bracketed pointer into the offending document, so a
    WebMCP tool result can name the field a human has to fix (§10.2: "machine-
    readable validation errors suitable for WebMCP tool results").
    """

    location: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"location": self.location, "message": self.message}


class CoreError(ValueError):
    """Base for every failure the core raises deliberately.

    A bare `ValueError` escaping the core would reach the service with nothing to
    map and nothing to show a caller. Every rejection therefore carries a code and
    an addressable detail list instead (constitution §5: fail closed, visibly).

    It derives from `ValueError` so that a rejection raised inside a Pydantic
    validator is collected into a `ValidationError` alongside the field that
    caused it, rather than escaping the model boundary uncaught. Callers that
    reach the helper directly still see the structured subclass.
    """

    default_code: ClassVar[CoreErrorCode] = CoreErrorCode.EVALUATION_INPUT_INVALID

    def __init__(
        self,
        message: str,
        *,
        code: CoreErrorCode | None = None,
        details: Sequence[ErrorDetail] = (),
    ) -> None:
        super().__init__(message)
        self.code = code or self.default_code
        self.message = message
        self.details: tuple[ErrorDetail, ...] = tuple(details)

    def as_dict(self) -> dict[str, object]:
        return {
            "code": str(self.code),
            "message": self.message,
            "details": [detail.as_dict() for detail in self.details],
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


class CanonicalizationError(CoreError):
    """A value cannot be canonicalized, so it must never be hashed (§17.2)."""

    default_code = CoreErrorCode.CANONICALIZATION_FAILED


class PathError(CoreError):
    """A restricted dotted observation path is malformed (§10.2)."""

    default_code = CoreErrorCode.INVALID_OBSERVATION_PATH


class ContractError(CoreError):
    """An outcome contract is invalid or exceeds a §10.4 limit."""

    default_code = CoreErrorCode.CONTRACT_VALIDATION_FAILED


class TransitionError(CoreError):
    """A lifecycle transition the state machine of §16 does not permit."""

    default_code = CoreErrorCode.INVALID_STATE_TRANSITION


class LimitError(CoreError):
    """A hard resource ceiling was reached (§21, FR-005ff)."""

    default_code = CoreErrorCode.RESOURCE_LIMIT_EXCEEDED


class EvaluationError(CoreError):
    """Evidence handed to the engine is internally inconsistent."""

    default_code = CoreErrorCode.EVALUATION_INPUT_INVALID


def parse_decimal(value: object) -> Decimal:
    """Coerce an exact decimal, refusing every lossy source.

    `float` is rejected rather than converted. `Decimal(0.1)` is
    `0.1000000000000000055511151231257827...`, and a total that wrong is worse
    than a total that fails: it hashes successfully and reads as evidence. `bool`
    is rejected too, because `True` is an `int` in Python and a boolean silently
    becoming `1.00` in a money field is a defect nobody looks for.
    """
    if isinstance(value, bool):
        raise ContractError(
            "a boolean is not a decimal value",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )
    if isinstance(value, float):
        raise ContractError(
            "money and other exact decimals must not arrive as float; "
            "use a decimal string such as '20.00'",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int | str):
        try:
            candidate = Decimal(value)
        except InvalidOperation as exc:
            raise ContractError(
                f"{value!r} is not a decimal value",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            ) from exc
    else:
        raise ContractError(
            f"{type(value).__name__} is not a decimal value",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )
    if not candidate.is_finite():
        raise ContractError(
            "a non-finite decimal has no canonical form",
            code=CoreErrorCode.NON_FINITE_NUMBER,
        )
    return candidate


def format_decimal(value: Decimal) -> str:
    """Serialize a decimal losslessly, without exponent notation.

    `str(Decimal('1E+2'))` is `'1E+2'`; two values that compare equal would then
    serialize differently and hash differently. Normalizing through a plain
    format string keeps the stored form stable while `Decimal` comparison stays
    value-based (§17.2 "decimal comparison").
    """
    return f"{value:f}"


def require_utc(value: datetime) -> datetime:
    """Normalize a persisted instant to timezone-aware UTC.

    A naive datetime is refused rather than assumed local or assumed UTC: the two
    assumptions disagree by hours, and the disagreement would land inside ordered
    evidence.
    """
    if value.tzinfo is None:
        raise ContractError(
            "a persisted instant must be timezone-aware",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )
    return value.astimezone(UTC)


def format_instant(value: datetime) -> str:
    """RFC 3339 in UTC with a `Z` suffix - one spelling per instant."""
    return require_utc(value).isoformat().replace("+00:00", "Z")


#: An exact decimal. Accepts `int`, `str` and `Decimal`; refuses `float`, `bool`
#: and non-finite values. Serializes to a decimal string (§17.2).
type Money = Annotated[
    Decimal,
    BeforeValidator(parse_decimal),
    PlainSerializer(format_decimal, return_type=str, when_used="always"),
]

#: A timezone-aware UTC instant, serialized as RFC 3339 with `Z`.
type UtcInstant = Annotated[
    datetime,
    BeforeValidator(require_utc),
    PlainSerializer(format_instant, return_type=str, when_used="always"),
]


class CoreModel(BaseModel):
    """The base every core record inherits.

    `frozen` makes a constructed record immutable, which is what lets it be
    hash-linked; `extra="forbid"` makes an unrecognised key a rejection rather
    than a silently retained field (constitution §5). `validate_default` is on so
    a default that would fail validation fails at import time, not on the one
    code path that omits the argument.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        str_strip_whitespace=False,
        arbitrary_types_allowed=False,
    )


@runtime_checkable
class Clock(Protocol):
    """The only sanctioned source of time inside the core.

    Injected rather than imported so a replay produces the instants the source
    run recorded (constitution §1).
    """

    def now(self) -> datetime:
        """Return the current instant as timezone-aware UTC."""
        ...


@runtime_checkable
class IdentifierSource(Protocol):
    """Deterministic identifier allocation.

    `prefix` keeps identifiers readable (`run-0001`) and keeps two independent
    counters from colliding inside one replay.
    """

    def next_id(self, prefix: str) -> str:
        """Return the next identifier for `prefix`."""
        ...


@runtime_checkable
class RandomSource(Protocol):
    """Randomness, injected so a run that uses it can still be replayed.

    Nothing in the deterministic engine may call this; it exists so that a future
    caller that genuinely needs randomness takes it from the run's recorded seed
    instead of the module-level `random`.
    """

    def next_below(self, upper_bound: int) -> int:
        """Return an integer in `[0, upper_bound)`."""
        ...
