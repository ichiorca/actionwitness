"""Shared-kernel gates (spec v1.9 §10.2, §17.2; constitution §1, §5; 002-T1).

The kernel is load-bearing precisely because nothing here looks interesting: a
frozen model, a decimal parser, a timestamp normalizer. Each one is the last
place a defect can be caught cheaply. A mutable record reaches the evidence chain
as a hash that no longer describes its payload; a float total reaches a report as
a wrong number that hashed successfully; a naive datetime reaches ordered
evidence off by the test machine's UTC offset.

So these tests assert the refusals, not the happy paths.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from actionwitness_core.kernel import (
    Clock,
    ContractError,
    CoreError,
    CoreErrorCode,
    CoreModel,
    ErrorDetail,
    IdentifierSource,
    Money,
    RandomSource,
    UtcInstant,
    format_decimal,
    format_instant,
    parse_decimal,
    require_utc,
)
from pydantic import ValidationError


class _Record(CoreModel):
    name: str
    total: Money
    observed_at: UtcInstant


def _record() -> _Record:
    return _Record(name="cart", total="20.00", observed_at=datetime(2026, 1, 1, tzinfo=UTC))


# --- CoreModel --------------------------------------------------------------


@pytest.mark.unit
def test_a_constructed_record_cannot_be_mutated() -> None:
    """An evidence record whose payload can change after hashing is not evidence."""
    record = _record()
    with pytest.raises(ValidationError):
        record.name = "tampered"


@pytest.mark.unit
def test_an_unknown_field_is_rejected_rather_than_retained() -> None:
    """Untrusted input with an unexpected key is a rejection, not an extension."""
    with pytest.raises(ValidationError) as excinfo:
        _Record(
            name="cart",
            total="20.00",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
            surprise="payload",
        )
    assert "surprise" in str(excinfo.value)


@pytest.mark.unit
def test_a_missing_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _Record(name="cart", total="20.00")


# --- money ------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        ("20.00", Decimal("20.00")),
        ("20.0", Decimal("20.0")),
        (20, Decimal(20)),
        (Decimal("-1.5"), Decimal("-1.5")),
        ("0", Decimal(0)),
    ],
)
def test_exact_decimal_sources_are_accepted(value: object, expected: Decimal) -> None:
    assert parse_decimal(value) == expected


@pytest.mark.unit
def test_a_float_is_refused_rather_than_rounded() -> None:
    """`Decimal(0.1)` is not 0.1; accepting it would hash a wrong total as evidence."""
    with pytest.raises(ContractError) as excinfo:
        parse_decimal(0.1)
    assert excinfo.value.code is CoreErrorCode.EVALUATION_INPUT_INVALID
    assert "float" in excinfo.value.message


@pytest.mark.unit
def test_a_boolean_is_not_a_decimal() -> None:
    """`True` is an `int` in Python; a boolean total must not become `1`."""
    with pytest.raises(ContractError):
        parse_decimal(True)


@pytest.mark.unit
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_decimals_are_refused(literal: str) -> None:
    """§17.2: a non-finite number has no canonical form, so it cannot be hashed."""
    with pytest.raises(ContractError) as excinfo:
        parse_decimal(literal)
    assert excinfo.value.code is CoreErrorCode.NON_FINITE_NUMBER


@pytest.mark.unit
@pytest.mark.parametrize("value", ["not-a-number", None, b"20.00", [Decimal(1)]])
def test_unparseable_values_raise_a_structured_error(value: object) -> None:
    with pytest.raises(CoreError):
        parse_decimal(value)


@pytest.mark.unit
def test_decimal_serialization_never_uses_exponent_notation() -> None:
    """`str(Decimal('1E+2'))` is `'1E+2'`; two equal values must serialize alike."""
    assert format_decimal(Decimal("1E+2")) == "100"
    assert format_decimal(Decimal("20.00")) == "20.00"
    assert format_decimal(Decimal("0.000001")) == "0.000001"


@pytest.mark.unit
def test_money_fields_serialize_as_decimal_strings() -> None:
    dumped = _record().model_dump(mode="json")
    assert dumped["total"] == "20.00"
    assert isinstance(dumped["total"], str)


@pytest.mark.unit
def test_a_float_in_a_money_field_fails_model_validation() -> None:
    with pytest.raises(ValidationError):
        _Record(name="cart", total=20.0, observed_at=datetime(2026, 1, 1, tzinfo=UTC))


# --- instants ---------------------------------------------------------------


@pytest.mark.unit
def test_a_naive_instant_is_refused() -> None:
    with pytest.raises(ContractError, match="timezone-aware"):
        require_utc(datetime(2026, 1, 1))


@pytest.mark.unit
def test_an_offset_instant_is_normalized_to_utc() -> None:
    """Two spellings of one instant must not produce two hashes."""
    offset = timezone(timedelta(hours=-5))
    normalized = require_utc(datetime(2025, 12, 31, 19, 0, tzinfo=offset))
    assert normalized == datetime(2026, 1, 1, tzinfo=UTC)
    assert normalized.tzinfo is UTC


@pytest.mark.unit
def test_instants_serialize_with_a_z_suffix() -> None:
    assert format_instant(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"
    assert _record().model_dump(mode="json")["observed_at"] == "2026-01-01T00:00:00Z"


@pytest.mark.unit
def test_a_naive_instant_in_a_model_field_fails_validation() -> None:
    with pytest.raises(ValidationError):
        _Record(name="cart", total="20.00", observed_at=datetime(2026, 1, 1))


# --- structured errors ------------------------------------------------------


@pytest.mark.unit
def test_every_core_error_carries_a_code_and_addressable_details() -> None:
    error = ContractError(
        "two things are wrong",
        details=[
            ErrorDetail("assertions[0].path", "unknown namespace"),
            ErrorDetail("assertions[1].operator", "unknown operator"),
        ],
    )
    assert error.as_dict() == {
        "code": "CONTRACT_VALIDATION_FAILED",
        "message": "two things are wrong",
        "details": [
            {"location": "assertions[0].path", "message": "unknown namespace"},
            {"location": "assertions[1].operator", "message": "unknown operator"},
        ],
    }


@pytest.mark.unit
def test_error_codes_are_upper_snake_case_wire_values() -> None:
    """The service maps these onto its API envelope; a spelling drift breaks that."""
    for code in CoreErrorCode:
        assert code.value == code.name
        assert code.value == code.value.upper()


@pytest.mark.unit
def test_core_errors_carry_no_transport_concern() -> None:
    """The core installs alone (§26.7); an HTTP status here would be a layering leak."""
    error = ContractError("nope")
    assert not hasattr(error, "http_status")
    assert not hasattr(error, "status_code")


# --- determinism seams ------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self._now = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now


class _Ids:
    def __init__(self) -> None:
        self._n = 0

    def next_id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}-{self._n:04d}"


class _Random:
    def next_below(self, upper_bound: int) -> int:
        return upper_bound - 1


@pytest.mark.unit
def test_the_determinism_protocols_are_satisfiable_by_a_plain_object() -> None:
    """A protocol nothing can implement without inheriting is not an injection seam."""
    assert isinstance(_Clock(), Clock)
    assert isinstance(_Ids(), IdentifierSource)
    assert isinstance(_Random(), RandomSource)


@pytest.mark.unit
def test_the_conftest_clock_and_id_source_satisfy_the_core_protocols(
    frozen_clock, id_sequence
) -> None:
    """The suite's builders are the injection the core promises, not a parallel one."""
    assert isinstance(frozen_clock, Clock)

    class _Adapter:
        def next_id(self, prefix: str) -> str:
            return id_sequence.next(prefix)

    assert isinstance(_Adapter(), IdentifierSource)


@pytest.mark.unit
def test_the_kernel_reads_no_wall_clock_or_randomness_of_its_own() -> None:
    """`Clock` is pointless if the module it lives in also calls `datetime.now`.

    Asserted over the parsed module rather than its text: the docstring names the
    very calls being forbidden, so a substring scan would fail on the explanation
    instead of on the defect.
    """
    import ast
    from pathlib import Path

    import actionwitness_core.kernel as kernel

    tree = ast.parse(Path(kernel.__file__).read_text(encoding="utf-8"))

    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imported & {"random", "secrets", "uuid", "time"} == set(), (
        f"the kernel imports a nondeterministic module: {sorted(imported)}"
    )

    called = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "datetime.now" not in called
    assert "datetime.utcnow" not in called


@pytest.mark.unit
def test_python_floats_that_are_non_finite_never_reach_a_money_field() -> None:
    """Belt and braces: the float refusal must fire before the finiteness check."""
    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ContractError, match="float"):
            parse_decimal(value)
