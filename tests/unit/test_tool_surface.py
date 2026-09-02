"""014-T1 — surface capture, §17.2 canonicalisation, identity, and deltas.

Three properties carry the rest of the milestone.

**Identity is four hashes, not one** (§17.2). FR-169's pre-invocation check has
to report *which* delta kind changed, because §9.5 grades them differently — a
description edit is a warning and a schema mutation fails the run. A single
whole-definition hash could only report that something changed, and the
per-kind strictness would have nothing to select on.

**Canonicalisation must not invent a delta.** A JSON Schema is order-insensitive
for `required`, `enum`, `anyOf` and `oneOf`, while RFC 8785 preserves array
order. Without normalising those, re-serialising an unchanged schema produces a
`schema_change` — a critical failure caused by key order.

**Canonicalisation must not erase one either.** The tests below pin both
directions, because the safe-looking fix for the first property is to normalise
more aggressively, and every normalisation is a difference the diff can no
longer see.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.enums import SurfaceDeltaKind
from actionwitness_core.evidence.enums import ToolNamespace
from actionwitness_core.evidence.surface import (
    MAX_TOOL_SCHEMA_DEPTH,
    ToolDefinition,
    ToolSurface,
    canonical_schema,
    diff_surfaces,
)

pytestmark = pytest.mark.unit

TARGET = ToolNamespace.TARGET


def tool(name: str = "apply_discount", **over: object) -> ToolDefinition:
    defaults: dict[str, object] = {
        "name": name,
        "namespace": TARGET,
        "description": "Apply a discount code to the cart.",
        "read_only_hint": False,
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
            "additionalProperties": False,
        },
    }
    return ToolDefinition(**{**defaults, **over})  # type: ignore[arg-type]


def kinds(deltas: tuple[object, ...]) -> list[str]:
    return [delta.kind.value for delta in deltas]  # type: ignore[attr-defined]


# --- identity (§17.2) --------------------------------------------------------


def test_an_unchanged_definition_has_a_stable_identity() -> None:
    assert tool().identity() == tool().identity()


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"description": "Apply a discount."}, SurfaceDeltaKind.DESCRIPTION_CHANGE),
        ({"read_only_hint": True}, SurfaceDeltaKind.HINT_CHANGE),
        (
            {"input_schema": {"type": "object", "properties": {"code": {"type": "number"}}}},
            SurfaceDeltaKind.SCHEMA_CHANGE,
        ),
    ],
)
def test_each_sub_hash_isolates_its_own_delta_kind(
    change: dict[str, object], expected: SurfaceDeltaKind
) -> None:
    """The reason identity is four hashes rather than one."""
    assert tool().identity().differing_kinds(tool(**change).identity()) == (expected,)


def test_a_definition_that_changed_twice_reports_both_kinds() -> None:
    """A script that rewrote a schema *and* a description did two things.

    Reporting only the first would understate what happened, and §9.5 grades the
    two differently — one fails by default and one warns.
    """
    changed = tool(description="Totally safe.", read_only_hint=True)

    assert set(tool().identity().differing_kinds(changed.identity())) == {
        SurfaceDeltaKind.DESCRIPTION_CHANGE,
        SurfaceDeltaKind.HINT_CHANGE,
    }


def test_an_absent_hint_differs_from_an_explicit_false() -> None:
    """A tool that stopped *declaring* itself read-only changed its hints.

    Collapsing absent into `false` would let a descriptor drop its safety
    annotation without anything noticing.
    """
    assert tool(read_only_hint=None).identity().hints_hash != tool().identity().hints_hash


# --- canonicalisation must not invent a delta (§17.2) ------------------------


@pytest.mark.parametrize(
    "first,second",
    [
        ({"required": ["a", "b"]}, {"required": ["b", "a"]}),
        ({"enum": [3, 1, 2]}, {"enum": [1, 2, 3]}),
        (
            {"anyOf": [{"type": "string"}, {"type": "number"}]},
            {"anyOf": [{"type": "number"}, {"type": "string"}]},
        ),
        (
            {"properties": {"b": {"type": "string"}, "a": {"type": "string"}}},
            {"properties": {"a": {"type": "string"}, "b": {"type": "string"}}},
        ),
    ],
)
def test_order_insensitive_keywords_hash_identically(
    first: dict[str, object], second: dict[str, object]
) -> None:
    """§17.2: "two semantically identical schemas hash differently and produce a
    spurious `schema_change` delta that fails `stable_tool_surface`"."""
    assert tool(input_schema=first).identity().schema_hash == (
        tool(input_schema=second).identity().schema_hash
    )


def test_an_ordered_keyword_keeps_its_order() -> None:
    """`prefixItems` is positional; sorting it would change what it means."""
    first = {"prefixItems": [{"type": "string"}, {"type": "number"}]}
    second = {"prefixItems": [{"type": "number"}, {"type": "string"}]}

    assert tool(input_schema=first).identity().schema_hash != (
        tool(input_schema=second).identity().schema_hash
    )


# --- canonicalisation must not erase one either ------------------------------


@pytest.mark.parametrize(
    "first,second",
    [
        ({"required": ["code"]}, {"required": ["code", "amount"]}),
        ({"enum": ["a"]}, {"enum": ["a", "b"]}),
        ({"type": "object"}, {"type": "array"}),
        ({"additionalProperties": False}, {"additionalProperties": True}),
        ({"additionalProperties": False}, {}),
    ],
)
def test_a_real_schema_difference_survives_canonicalisation(
    first: dict[str, object], second: dict[str, object]
) -> None:
    """The guard on the tests above.

    The safe-looking fix for a spurious delta is to normalise harder, and every
    normalisation is a difference the diff can no longer see. The last case is
    deliberate: an absent `additionalProperties` is *not* normalised to `false`,
    because a schema that stopped forbidding extra properties got looser.
    """
    assert tool(input_schema=first).identity().schema_hash != (
        tool(input_schema=second).identity().schema_hash
    )


def test_a_ref_cycle_is_refused_rather_than_followed() -> None:
    """Following one does not terminate, and a depth guess would make the hash
    depend on the guess."""
    with pytest.raises(ValueError, match=r"\$ref cycle"):
        canonical_schema({"type": "object", "properties": {"self": {"$ref": "#"}}})


def test_an_absurdly_deep_schema_is_refused_rather_than_truncated() -> None:
    """A schema cut off at a depth limit would hash the same as a different one
    that agreed down to that limit."""
    schema: dict[str, object] = {"type": "string"}
    for _ in range(MAX_TOOL_SCHEMA_DEPTH + 2):
        schema = {"type": "object", "properties": {"next": schema}}

    with pytest.raises(ValueError, match="nest at most"):
        canonical_schema(schema)


# --- the surface and its partition (§9.11) -----------------------------------


def test_a_surface_hashes_the_same_however_the_browser_ordered_it() -> None:
    """`getTools()` makes no ordering promise.

    A surface that hashed differently because an array came back shuffled would
    report a mutation on every capture, and the policy would be useless within a
    minute of being switched on.
    """
    one = ToolSurface(tools=(tool("a"), tool("b")))
    other = ToolSurface(tools=(tool("b"), tool("a")))

    assert one.content_hash() == other.content_hash()


def test_the_partition_separates_harness_tools_from_target_tools() -> None:
    """§9.11: stability policy applies to the target partition by default."""
    surface = ToolSurface(
        tools=(tool("verify_outcome", namespace=ToolNamespace.HARNESS), tool("apply_discount"))
    )

    assert [t.name for t in surface.partition(TARGET)] == ["apply_discount"]
    assert [t.name for t in surface.partition(ToolNamespace.HARNESS)] == ["verify_outcome"]


# --- deltas (§9.5, FR-167) ---------------------------------------------------


def test_a_quiet_surface_produces_no_delta() -> None:
    surface = ToolSurface(tools=(tool(),))

    assert diff_surfaces(surface, surface, namespace=TARGET) == ()


def test_a_new_tool_is_added_and_a_vanished_tool_is_removed() -> None:
    baseline = ToolSurface(tools=(tool("apply_discount"),))
    current = ToolSurface(tools=(tool("look_alike"),))

    deltas = diff_surfaces(baseline, current, namespace=TARGET)

    # Ordered by tool name, so the vanished tool is reported before the new one.
    assert [d.tool_name for d in deltas] == ["apply_discount", "look_alike"]
    assert kinds(deltas) == ["removed", "added"]


def test_a_delta_carries_both_definitions_for_the_side_by_side_diff() -> None:
    """FR-169 requires "a side-by-side diff of the tool definition before and
    after as evidence".

    A delta naming only a kind would tell a reader a schema changed without
    letting them see what it changed to — an alert rather than evidence.
    """
    baseline = ToolSurface(tools=(tool(),))
    current = ToolSurface(tools=(tool(description="Definitely not exfiltrating anything."),))

    (delta,) = diff_surfaces(baseline, current, namespace=TARGET)

    assert delta.kind is SurfaceDeltaKind.DESCRIPTION_CHANGE
    assert delta.before is not None
    assert delta.after is not None
    assert delta.before.description != delta.after.description


def test_a_harness_partition_change_does_not_appear_in_the_target_diff() -> None:
    """§9.11's whole reason for existing: the workspace's own tools come and go
    with the run's phase, and that is not a mutation."""
    baseline = ToolSurface(
        tools=(tool(), tool("arm_outcome_contract", namespace=ToolNamespace.HARNESS))
    )
    current = ToolSurface(tools=(tool(), tool("verify_outcome", namespace=ToolNamespace.HARNESS)))

    assert diff_surfaces(baseline, current, namespace=TARGET) == ()
    # ...and the harness partition *did* see it, so this test is about the
    # partition rather than about the diff having found nothing at all.
    assert kinds(diff_surfaces(baseline, current, namespace=ToolNamespace.HARNESS)) == [
        "removed",
        "added",
    ]


def test_deltas_are_ordered_deterministically() -> None:
    """Two identical runs must not produce differently ordered evidence."""
    baseline = ToolSurface(tools=(tool("a"), tool("b"), tool("c")))
    current = ToolSurface(tools=(tool("a", description="x"), tool("b", read_only_hint=True)))

    assert diff_surfaces(baseline, current, namespace=TARGET) == diff_surfaces(
        baseline, current, namespace=TARGET
    )
    assert [d.tool_name for d in diff_surfaces(baseline, current, namespace=TARGET)] == [
        "a",
        "b",
        "c",
    ]
