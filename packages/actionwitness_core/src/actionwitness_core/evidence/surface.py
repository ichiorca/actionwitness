"""The tool surface: capture, canonicalisation, identity, and deltas.

Spec v1.9 §9.11 (the surface and its two namespaces), §9.5 (the delta kinds),
§17.2 (schema canonicalisation and tool identity), FR-166 through FR-169;
014-T1/T3.

**The browser reports; the server decides.** A captured definition arrives from
a page that may be running an agent's tools, so every hash here is computed from
the submitted *definitions*, never accepted as a submitted *hash*. A
client-computed identity would be the tool surface vouching for itself — the
same category error as accepting a tool's own success report as proof of an
outcome, and this module exists because that category error is the product's
whole subject.

**Four hashes, not one** (§17.2). A tool's identity is `name_hash`,
`schema_hash`, `hints_hash` and `description_hash` plus a composite
`identity_hash` over the four. FR-169's pre-invocation check compares the
sub-hashes so it can say *which* delta kind changed; a single whole-definition
hash could only report that something did, and §9.5's per-kind strictness —
where a description edit is a warning and a schema mutation is a failure — would
have nothing to select on.

**Schema canonicalisation is normative and load-bearing** (§17.2). RFC 8785
sorts object keys but preserves array order, while a JSON Schema is
order-insensitive for `required`, `enum`, `anyOf` and `oneOf`. Without
normalising those, two semantically identical schemas hash differently and
produce a spurious `schema_change` — a critical-severity failure caused by key
order. A `$ref` cycle is rejected rather than followed, because following one
does not terminate and guessing a depth limit would make the hash depend on the
limit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from actionwitness_core.contracts.enums import SurfaceDeltaKind
from actionwitness_core.evidence.enums import ToolNamespace
from actionwitness_core.kernel import CoreModel, JsonValue
from actionwitness_core.security.canonical import canonicalize, content_hash

__all__ = [
    "MAX_SURFACE_TOOLS",
    "MAX_TOOL_SCHEMA_DEPTH",
    "SurfaceDelta",
    "ToolDefinition",
    "ToolIdentity",
    "ToolNamespace",
    "ToolSurface",
    "canonical_schema",
    "diff_surfaces",
]

#: §20.2 bounds a frontend-submitted surface at 100 tools. Enforced on the model
#: so an oversized payload is refused at the boundary rather than hashed first.
MAX_SURFACE_TOOLS: Final = 100

#: How deep a nested input schema may be before canonicalisation refuses it.
#: A bound is required for the same reason `$ref` cycles are: the walk must
#: terminate on input the harness did not author.
MAX_TOOL_SCHEMA_DEPTH: Final = 32

#: JSON Schema keywords whose array value is a *set*, not a sequence (§17.2).
_UNORDERED_SCALAR_KEYWORDS: Final = frozenset({"required", "enum"})

#: Keywords whose array members are schemas and are unordered; sorted by their
#: own canonical serialization so the order is total and content-derived.
_UNORDERED_SCHEMA_KEYWORDS: Final = frozenset({"anyOf", "oneOf", "allOf"})


class ToolIdentity(CoreModel):
    """§17.2's four hashes plus their composite.

    Separate fields rather than a dict so a caller cannot ask for a sub-hash
    that does not exist, and so the composite is derived here rather than
    recomputed differently by each comparison site.
    """

    name_hash: str
    description_hash: str
    hints_hash: str
    schema_hash: str
    identity_hash: str

    def differing_kinds(self, other: ToolIdentity) -> tuple[SurfaceDeltaKind, ...]:
        """Which §9.5 delta kinds separate these two identities.

        A tuple rather than one kind: a script that swapped a tool's schema
        *and* its description changed two things, and reporting only the first
        would understate what happened. `name_hash` is not a kind — two
        definitions with different names are different tools, compared as an
        `added` and a `removed` rather than as a change.
        """
        kinds: list[SurfaceDeltaKind] = []
        if self.schema_hash != other.schema_hash:
            kinds.append(SurfaceDeltaKind.SCHEMA_CHANGE)
        if self.hints_hash != other.hints_hash:
            kinds.append(SurfaceDeltaKind.HINT_CHANGE)
        if self.description_hash != other.description_hash:
            kinds.append(SurfaceDeltaKind.DESCRIPTION_CHANGE)
        return tuple(kinds)


class ToolDefinition(CoreModel):
    """One tool as the browser reported it (§9.11, FR-166).

    The four fields are exactly what §9.11 says a tool contributes to the
    surface: "its name, description, side-effect hints, and canonicalised input
    schema". Nothing else is recorded, because nothing else is compared — and a
    field that is stored but never compared is evidence that cannot fail.
    """

    name: str
    namespace: ToolNamespace
    description: str = ""
    #: `readOnlyHint` and friends. `None` means the descriptor carried no hint,
    #: which is distinct from an explicit `false`: a tool that stopped
    #: *declaring* itself read-only changed its hints.
    read_only_hint: bool | None = None
    untrusted_content_hint: bool | None = None
    input_schema: Mapping[str, JsonValue] = {}

    def identity(self) -> ToolIdentity:
        """§17.2's identity for this definition."""
        name_hash = content_hash(self.name)
        description_hash = content_hash(self.description)
        hints_hash = content_hash(
            {
                "read_only_hint": self.read_only_hint,
                "untrusted_content_hint": self.untrusted_content_hint,
            }
        )
        schema_hash = content_hash(canonical_schema(self.input_schema))
        return ToolIdentity(
            name_hash=name_hash,
            description_hash=description_hash,
            hints_hash=hints_hash,
            schema_hash=schema_hash,
            identity_hash=content_hash(
                {
                    "name_hash": name_hash,
                    "description_hash": description_hash,
                    "hints_hash": hints_hash,
                    "schema_hash": schema_hash,
                }
            ),
        )

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "namespace": self.namespace.value,
            "description": self.description,
            "read_only_hint": self.read_only_hint,
            "untrusted_content_hint": self.untrusted_content_hint,
            "input_schema": canonical_schema(self.input_schema),
        }


class ToolSurface(CoreModel):
    """Every tool visible at one instant (§9.11).

    Ordered by name on construction, so two captures of the same surface hash
    identically however the browser happened to enumerate them. `getTools()`
    makes no ordering promise, and a surface that hashed differently because an
    array came back shuffled would report a mutation on every capture.
    """

    tools: tuple[ToolDefinition, ...] = ()

    def sorted_tools(self) -> tuple[ToolDefinition, ...]:
        return tuple(sorted(self.tools, key=lambda tool: (tool.namespace.value, tool.name)))

    def partition(self, namespace: ToolNamespace) -> tuple[ToolDefinition, ...]:
        """One namespace's tools. §9.11 applies stability policy to `target`."""
        return tuple(tool for tool in self.sorted_tools() if tool.namespace is namespace)

    def by_name(self, namespace: ToolNamespace) -> dict[str, ToolDefinition]:
        return {tool.name: tool for tool in self.partition(namespace)}

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"tools": [tool.canonical_document() for tool in self.sorted_tools()]}

    def content_hash(self) -> str:
        """§17.2's hash over the whole surface."""
        return content_hash(self.canonical_document())


class SurfaceDelta(CoreModel):
    """One tool's change between two captures (§9.5, FR-167).

    `before` and `after` carry the full definitions because FR-169 requires "a
    side-by-side diff of the tool definition before and after as evidence". A
    delta that named only a kind would tell a reader that a schema changed
    without letting them see what it changed to — which is the difference
    between an alert and evidence.
    """

    #: Empty only for a delta replayed from a §24.3a case that recorded no tool
    #: name. Such a delta can never be excused by `declared_churn_tools`, which
    #: matches on exact names — the safe direction, since the alternative is an
    #: unnamed delta matching an empty allowlist entry and being waved through.
    tool_name: str = ""
    namespace: ToolNamespace
    kind: SurfaceDeltaKind
    before: ToolDefinition | None = None
    after: ToolDefinition | None = None

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "tool_name": self.tool_name,
            "namespace": self.namespace.value,
            "kind": self.kind.value,
            "before": None if self.before is None else self.before.canonical_document(),
            "after": None if self.after is None else self.after.canonical_document(),
        }


def canonical_schema(schema: Mapping[str, JsonValue], _depth: int = 0) -> JsonValue:
    """§17.2's normative schema canonicalisation.

    Sorts the order-insensitive keywords and leaves everything else alone.
    Object keys are not sorted here because RFC 8785 already does that when the
    result is hashed; doing it twice would be a second rule to keep in
    agreement with the first.

    Deliberately conservative about §17.2's "normalise an absent value and its
    documented default to the same form". Only a keyword whose default this
    module can name is normalised, and today that is none of them: inventing a
    default would silently erase a real difference between two schemas, and a
    missed normalisation merely over-reports a `schema_change`, which is visible
    and waivable. Recorded in the 014 deviations ledger.
    """
    return _canonicalize_value(schema, _depth)


def _canonicalize_value(value: JsonValue, depth: int) -> JsonValue:
    if depth > MAX_TOOL_SCHEMA_DEPTH:
        # A bound rather than a recursion error, and refused rather than
        # truncated: a schema silently cut off at a depth limit would hash the
        # same as a different schema that agreed down to that depth.
        raise ValueError(
            f"a tool input schema may nest at most {MAX_TOOL_SCHEMA_DEPTH} levels; "
            "deeper input is refused rather than truncated"
        )

    if isinstance(value, Mapping):
        if _has_ref_cycle(value):
            raise ValueError(
                "a tool input schema contains a $ref cycle and cannot be canonicalised"
            )
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if key in _UNORDERED_SCALAR_KEYWORDS and _is_array(item):
                # `required` and `enum` are sets. Sorted by canonical bytes so
                # mixed-type `enum` members order totally rather than raising.
                result[key] = sorted(item, key=lambda member: canonicalize(member))
            elif key in _UNORDERED_SCHEMA_KEYWORDS and _is_array(item):
                result[key] = sorted(
                    (_canonicalize_value(member, depth + 1) for member in item),
                    key=lambda member: canonicalize(member),
                )
            else:
                result[key] = _canonicalize_value(item, depth + 1)
        return result

    if _is_array(value):
        # Every other array is ordered — `prefixItems`, and anything the
        # vocabulary does not name. Order is preserved because for those it is
        # meaning, not presentation.
        return [_canonicalize_value(item, depth + 1) for item in value]

    return value


def _is_array(value: JsonValue) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _has_ref_cycle(schema: Mapping[str, JsonValue]) -> bool:
    """Whether a `$ref` in this schema points at an ancestor of itself.

    Only local pointers are examined. A `$ref` to another document is not a
    cycle this module can see, and pretending otherwise would refuse a schema
    for a property it does not have.
    """
    targets = _local_refs(schema, set())
    return any(target in _SELF_REFERENTIAL for target in targets)


#: Pointers that name the document root. A `$ref` to the root from inside the
#: root is the cycle that actually appears in hand-written schemas.
_SELF_REFERENTIAL: Final = frozenset({"#", "#/"})


def _local_refs(value: JsonValue, seen: set[int]) -> set[str]:
    """Every local `$ref` string in this document, without revisiting a node."""
    if id(value) in seen:
        return set()
    seen.add(id(value))

    refs: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                refs.add(item)
            else:
                refs |= _local_refs(item, seen)
    elif _is_array(value):
        for item in value:
            refs |= _local_refs(item, seen)
    return refs


def diff_surfaces(
    baseline: ToolSurface, current: ToolSurface, *, namespace: ToolNamespace
) -> tuple[SurfaceDelta, ...]:
    """Every §9.5 delta between two captures of one namespace (FR-167).

    Ordered by tool name and then by kind, so the same pair of captures always
    produces the same list — a report whose delta order varied would make two
    identical runs look different.

    A tool present in both contributes one delta per differing sub-hash, because
    a script that rewrote both a schema and a description did two things and
    §9.5 grades them differently.
    """
    before = baseline.by_name(namespace)
    after = current.by_name(namespace)

    deltas: list[SurfaceDelta] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old is None and new is not None:
            deltas.append(
                SurfaceDelta(
                    tool_name=name,
                    namespace=namespace,
                    kind=SurfaceDeltaKind.ADDED,
                    after=new,
                )
            )
        elif new is None and old is not None:
            deltas.append(
                SurfaceDelta(
                    tool_name=name,
                    namespace=namespace,
                    kind=SurfaceDeltaKind.REMOVED,
                    before=old,
                )
            )
        elif old is not None and new is not None:
            for kind in old.identity().differing_kinds(new.identity()):
                deltas.append(
                    SurfaceDelta(
                        tool_name=name,
                        namespace=namespace,
                        kind=kind,
                        before=old,
                        after=new,
                    )
                )
    return tuple(deltas)
