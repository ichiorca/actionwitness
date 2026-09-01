"""Validating tool arguments against a published input schema (§11.4, §20.2, FR-021).

Every allowlisted tool publishes a `input_schema`, and an agent's arguments are
untrusted input like any other (constitution §5). This validates the second
against the first before anything reaches an adapter.

**Why not a JSON Schema library.** A general validator is built to be permissive
about what it does not recognise — an unknown keyword is ignored, and the
document validates. That is the opposite of what this project needs. FR-021
keeps the declarative surface "to allowlisted scalars" and forbids nested
constructs arriving from an agent, and §11.4 calls these schemas *closed*. So
this validator **refuses a schema keyword it does not implement**, which turns a
silently-ignored constraint into a loud failure at the moment a tool spec is
written rather than a hole discovered later.

The supported subset is exactly what the published specs use: `type`,
`properties`, `required`, `additionalProperties`, `enum`, `minimum`, `maximum`,
`minLength`, `maxLength`, plus the annotations `description`, `title`, and
`default`. Adding a keyword here is a deliberate act with a test, which is the
point.

Errors are `ErrorDetail`s addressed to the offending field, so §10.2's
"machine-readable validation errors suitable for WebMCP tool results" can name
every problem at once rather than one per round trip.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from actionwitness_core.kernel import ContractError, CoreErrorCode, ErrorDetail

__all__ = ["SUPPORTED_KEYWORDS", "validate_arguments", "validate_schema"]

#: Keywords this validator implements. Anything else in a schema is refused.
SUPPORTED_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        # Annotations. They constrain nothing, so they are safe to allow and
        # useful to a human reading the published surface.
        "description",
        "title",
        "default",
    }
)

_SUPPORTED_TYPES: Final[frozenset[str]] = frozenset(
    {"object", "string", "integer", "number", "boolean"}
)


def validate_schema(schema: Mapping[str, Any], *, where: str = "input_schema") -> None:
    """Refuse a schema this validator would silently under-enforce.

    Called when a tool spec is registered rather than on every invocation: a
    schema is authored once and a constraint that is quietly ignored is a hole
    that only shows up as a tool accepting something it declared it would not.
    """
    unknown = sorted(set(schema) - SUPPORTED_KEYWORDS)
    if unknown:
        raise ContractError(
            f"{where} uses schema keywords this validator does not implement: {', '.join(unknown)}",
            code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            details=tuple(
                ErrorDetail(location=f"{where}.{keyword}", message="unsupported schema keyword")
                for keyword in unknown
            ),
        )

    declared = schema.get("type")
    if declared is not None and declared not in _SUPPORTED_TYPES:
        raise ContractError(
            f"{where} declares unsupported type {declared!r}",
            code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            details=(ErrorDetail(location=f"{where}.type", message="unsupported type"),),
        )

    for name, subschema in (schema.get("properties") or {}).items():
        if not isinstance(subschema, Mapping):
            raise ContractError(
                f"{where}.properties.{name} is not a schema object",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        validate_schema(subschema, where=f"{where}.properties.{name}")


def validate_arguments(
    schema: Mapping[str, Any], arguments: Mapping[str, Any], *, tool_name: str
) -> dict[str, Any]:
    """Validate `arguments`, returning them with schema defaults applied.

    Defaults are applied here rather than by the adapter so the values that
    reach the target are the values recorded as evidence. An adapter that
    defaulted internally would execute against arguments the timeline never saw.
    """
    validate_schema(schema)

    problems: list[ErrorDetail] = []
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required: Sequence[str] = schema.get("required") or ()

    if schema.get("additionalProperties") is False:
        for name in sorted(set(arguments) - set(properties)):
            problems.append(ErrorDetail(location=name, message="unknown argument for this tool"))

    for name in required:
        if name not in arguments:
            problems.append(ErrorDetail(location=name, message="required argument is missing"))

    resolved: dict[str, Any] = {}
    for name, subschema in properties.items():
        if name not in arguments:
            if "default" in subschema:
                resolved[name] = subschema["default"]
            continue
        value = arguments[name]
        problem = _check(name, subschema, value)
        if problem is not None:
            problems.append(problem)
        else:
            resolved[name] = value

    if problems:
        raise ContractError(
            f"the arguments for {tool_name!r} do not match its published schema",
            code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            details=tuple(problems),
        )
    return resolved


def _check(name: str, schema: Mapping[str, Any], value: Any) -> ErrorDetail | None:
    """One property. Returns a detail describing the first problem, or `None`."""
    declared = schema.get("type")
    if declared is not None and not _is_type(declared, value):
        return ErrorDetail(location=name, message=f"expected {declared}")

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(option) for option in schema["enum"])
        return ErrorDetail(location=name, message=f"expected one of {allowed}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            return ErrorDetail(location=name, message=f"shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return ErrorDetail(location=name, message=f"longer than {schema['maxLength']}")

    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return ErrorDetail(location=name, message=f"below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            return ErrorDetail(location=name, message=f"above the maximum {schema['maximum']}")

    return None


def _is_type(declared: str, value: Any) -> bool:
    """JSON type checking, with the two traps written out.

    `True` is an `int` in Python, so a boolean would satisfy `integer` and a
    tool declaring a quantity would accept `True` as `1`. And a JSON `number`
    accepts an integer while `integer` does not accept a float, which is the
    asymmetry the specs rely on for `quantity`.
    """
    match declared:
        case "object":
            return isinstance(value, Mapping)
        case "string":
            return isinstance(value, str)
        case "boolean":
            return isinstance(value, bool)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
    return False  # pragma: no cover - `validate_schema` refuses other types first
