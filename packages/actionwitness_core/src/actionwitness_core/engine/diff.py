"""FR-157 — the full-state diff between two canonical observations.

Spec v1.9 FR-157, §9.10, §17.2; 013-T1.

FR-157: "Verification shall compute a complete recursive diff of the initial and
final canonical snapshots, **independent of which paths the contract names**. The
diff shall use the same canonicalisation and decimal handling as Section 17.2."

That independence is the point of the whole feature. Assertion evaluation is only
as complete as the contract author's imagination; this walk is what notices the
shipping address a journey rewrote while every named assertion stayed green. So
nothing here consults the contract — the partition into declared and undeclared
happens afterwards, in the policy engine, against paths this function found
without knowing what anybody expected.

Three decisions carry the determinism the report depends on.

**Decimal comparison, not string comparison** (§17.2, normative). Money is
serialized as a decimal string, and `"20.00"` and `"20.0"` are the same value.
Comparing them as strings makes a target that reformats a total emit a spurious
`undeclared_state_change` — a critical-severity failure caused by cosmetics. The
rule is applied to numbers too: JSON has one number type, so `1` and `1.0` are
the same value for the same reason.

That reinterpretation is narrow on purpose, because it is the one rule here that
can make a change *disappear*, and the docstring above says why that is the worst
outcome available. A string is compared as a decimal only when it is written in
**plain finite decimal notation** — an optional minus sign, an integer part with
no redundant leading zero, and an optional fractional part. Everything else falls
back to exact string equality, which keeps two facts true at once: identical
strings are always equal, and different strings are only ever equal when §17.2
actually says so.

Two families of string are excluded by that rule, and both were bugs before it
existed.

* **Non-finite spellings.** `Decimal` happily parses `"NaN"`, `"sNaN"` and
  `"Infinity"`, and neither result can be compared safely. Two `"NaN"` strings
  compare *unequal*, so an untouched path was reported as changed — a spurious
  critical failure on state that never moved. A *signaling* `"sNaN"` is worse:
  the comparison itself raises `InvalidOperation`, so one string in an untrusted
  target payload killed verification mid-diff. §17.2 rejects non-finite numbers
  before serialization, so no legitimate snapshot loses anything here.
* **Alternative spellings of the same number.** `"007"` and `"7"`, or `"1e2"`
  and `"100"`, are equal as decimals and are not the same string. A SKU, order
  code, or external identifier that a target rewrote from one to the other is a
  real edit, and the old rule dropped it silently. §17.2's normative example is
  a reformatted *decimal fraction* — `"20.00"` becoming `"20.0"` — and that is
  the whole of what this rule is licensed to forgive.

Both exclusions match `engine.assertions._as_exact`, which already refuses a
non-finite decimal for the same reason. Whatever the payload, comparison is
**total**: it returns a bool for any pair of values and never raises.

**Arrays compare positionally.** RFC 8785 sorts object keys and *preserves* array
order, so position is meaningful in the canonical form and index `0` is a stable
identity. The alternative — matching elements by content — would need a rule for
which of two reorderings is "the same list", and any such rule is a guess that
changes what a policy failure means. A reordered array therefore reports element
changes, which is honest: the canonical document did change.

**One change per leaf, one change per added or removed subtree.** Recursion stops
where a key exists on one side only, so a deleted object reports its own path
rather than one path per descendant. That keeps the undeclared-path list
proportional to what happened rather than to how deep the document is, and it is
what lets a whole-subtree removal be covered by an effect prefix naming that
subtree.

Excerpts are bounded through `security.limits`, so a report never carries an
unbounded payload copied out of a snapshot.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Final

from pydantic import Field

from actionwitness_core.contracts.paths import (
    ObservationPath,
    ObservationPathField,
    is_valid_segment,
)
from actionwitness_core.kernel import CoreModel, JsonValue
from actionwitness_core.security.canonical import canonicalize
from actionwitness_core.security.limits import MAX_FINDING_VALUE_CHARS, bounded_summary

__all__ = [
    "MAX_CHANGE_EXCERPT_CHARS",
    "ChangeKind",
    "StateChange",
    "changed_paths_of",
    "diff_states",
    "values_differ",
]

#: Each `before` and `after` excerpt, in characters. §11.4's finding-value budget
#: rather than a new number: these values are rendered in exactly the places that
#: budget already governs.
MAX_CHANGE_EXCERPT_CHARS: Final = MAX_FINDING_VALUE_CHARS


class ChangeKind(StrEnum):
    """How one path differs between the two snapshots.

    Deliberately **not** in `engine.enums` and so not in the exported registry.
    The registry exists so "API handlers, UI, and tests share names" for
    vocabularies that cross the boundary; the §23.1 report block carries paths
    and counts, and no API payload names a change kind. Registering it would
    publish a name nothing outside this module reads, and `registry.json` is a
    committed artifact that should gain rows only when something needs them.
    """

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


class StateChange(CoreModel):
    """One path that differs, with bounded excerpts of both sides.

    `before` and `after` are rendered strings rather than raw values: they exist
    to be read in a finding, they must be bounded (§11.4), and a raw value would
    put an arbitrary sub-document into a report that promises not to carry one.
    A missing side is `None` — distinct from the string `"null"`, which is a
    present JSON null.
    """

    path: ObservationPathField
    kind: ChangeKind
    before: Annotated[str, Field(max_length=MAX_CHANGE_EXCERPT_CHARS)] | None = None
    after: Annotated[str, Field(max_length=MAX_CHANGE_EXCERPT_CHARS)] | None = None

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "path": str(self.path),
            "kind": self.kind.value,
            "before": self.before,
            "after": self.after,
        }


def values_differ(before: JsonValue, after: JsonValue) -> bool:
    """§17.2's comparison: decimal-aware, type-aware, and total.

    Exposed because the assertion engine and candidate derivation answer the same
    question, and two implementations of "did this value change" is exactly the
    disagreement that produces a finding nobody can reproduce.
    """
    return not _equal(before, after)


def diff_states(
    before: Mapping[str, JsonValue],
    after: Mapping[str, JsonValue],
    *,
    excerpt_chars: int = MAX_CHANGE_EXCERPT_CHARS,
) -> tuple[StateChange, ...]:
    """Every path that differs, ordered deterministically.

    `before` and `after` are evaluation contexts — `{namespace: payload}`, the
    shape `Observation.as_context()` produces — so the paths returned are the
    same `target.cart.total` paths a contract and an effect map speak.

    Ordering is lexicographic by canonical path string. Any total order would
    make the output byte-identical for identical input; this one also makes the
    list readable, and siblings sort together.
    """
    changes: list[StateChange] = []
    _walk((), before, after, changes, excerpt_chars)
    return tuple(sorted(changes, key=lambda change: str(change.path)))


def changed_paths_of(changes: Sequence[StateChange]) -> tuple[ObservationPath, ...]:
    """Just the paths, in the same deterministic order.

    The policy engine partitions paths and does not read excerpts, so it takes
    this rather than the changes — which keeps `PolicyEvidence` free of values
    it has no business comparing.
    """
    return tuple(change.path for change in changes)


# --- the walk ----------------------------------------------------------------


def _walk(
    prefix: tuple[str, ...],
    before: JsonValue,
    after: JsonValue,
    changes: list[StateChange],
    excerpt_chars: int,
) -> None:
    """Recurse where both sides agree on shape; emit where they do not."""
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        _walk_mapping(prefix, before, after, changes, excerpt_chars)
        return

    if _is_array(before) and _is_array(after):
        _walk_array(prefix, before, after, changes, excerpt_chars)
        return

    # Either a scalar pair, or a shape change (object became a list, list became
    # a string). A shape change is one `changed` at this path rather than a
    # removal plus an addition: one thing happened to one path, and reporting it
    # twice would double-count it in the undeclared total.
    if not _equal(before, after):
        changes.append(_change(prefix, ChangeKind.CHANGED, before, after, excerpt_chars))


def _walk_mapping(
    prefix: tuple[str, ...],
    before: Mapping[str, JsonValue],
    after: Mapping[str, JsonValue],
    changes: list[StateChange],
    excerpt_chars: int,
) -> None:
    unrepresentable = sorted(key for key in set(before) | set(after) if not is_valid_segment(key))
    if unrepresentable and not prefix:
        # The top level of a context is provider namespaces, which `Observation`
        # validates as `Token` before a snapshot can exist. There is no parent to
        # fall back to here, and silently dropping one would hide an entire
        # provider's changes, so this is raised rather than absorbed: it means a
        # context was assembled by something that skipped that validation.
        raise ValueError(f"context namespaces are not valid path segments: {unrepresentable}")
    if unrepresentable:
        # A document key that is not a legal path segment — a space, a leading
        # digit, a dot of its own — cannot be named in a finding, because the
        # dotted path it would produce does not parse back to itself.
        #
        # Reporting the *parent* is the only honest answer available. Skipping
        # the key would let a change vanish, and this feature exists precisely to
        # stop a change from vanishing; raising would fail verification on a
        # document the target is entitled to return. So the subtree is compared
        # as one value and reported at a path that can be read.
        if not _equal(before, after):
            changes.append(_change(prefix, ChangeKind.CHANGED, before, after, excerpt_chars))
        return

    for key in sorted(set(before) | set(after)):
        child = (*prefix, key)
        in_before, in_after = key in before, key in after
        if in_before and in_after:
            _walk(child, before[key], after[key], changes, excerpt_chars)
        elif in_after:
            changes.append(_change(child, ChangeKind.ADDED, None, after[key], excerpt_chars))
        else:
            changes.append(_change(child, ChangeKind.REMOVED, before[key], None, excerpt_chars))


def _walk_array(
    prefix: tuple[str, ...],
    before: Sequence[JsonValue],
    after: Sequence[JsonValue],
    changes: list[StateChange],
    excerpt_chars: int,
) -> None:
    """Positional, per the module docstring.

    Indices are path segments, so an element change reads as
    `target.cart.lines.0.quantity` and is covered by an effect prefix naming
    `target.cart.lines` exactly as an object key would be.
    """
    for index in range(max(len(before), len(after))):
        child = (*prefix, str(index))
        in_before, in_after = index < len(before), index < len(after)
        if in_before and in_after:
            _walk(child, before[index], after[index], changes, excerpt_chars)
        elif in_after:
            changes.append(_change(child, ChangeKind.ADDED, None, after[index], excerpt_chars))
        else:
            changes.append(_change(child, ChangeKind.REMOVED, before[index], None, excerpt_chars))


def _change(
    prefix: tuple[str, ...],
    kind: ChangeKind,
    before: JsonValue,
    after: JsonValue,
    excerpt_chars: int,
) -> StateChange:
    return StateChange(
        path=ObservationPath(segments=prefix),
        kind=kind,
        before=None if kind is ChangeKind.ADDED else _excerpt(before, excerpt_chars),
        after=None if kind is ChangeKind.REMOVED else _excerpt(after, excerpt_chars),
    )


def _excerpt(value: JsonValue, limit: int) -> str:
    """A bounded, canonical rendering of one value.

    Canonical bytes rather than `repr` or `str`: the same value must render the
    same way on every machine and in every replay, and RFC 8785 is the project's
    existing answer to that. Bounding happens after rendering, so a large
    sub-document is cut rather than summarised into something unrecoverable.
    """
    rendered = canonicalize(value).decode("utf-8")
    return bounded_summary(rendered, limit).text


# --- comparison (§17.2) -------------------------------------------------------


#: The only string shape §17.2's decimal rule may reinterpret: an optional minus
#: sign, an integer part carrying no redundant leading zero, and an optional
#: fractional part. Deliberately narrower than what `Decimal` will parse — it
#: admits `"20.00"` and `"-0.0"` and refuses `"007"`, `"1e2"`, `"+7"`, `".5"`,
#: `"NaN"`, `"sNaN"` and `"Infinity"`. Matched with `fullmatch`, so no anchors
#: and no `$` newline tolerance.
_PLAIN_DECIMAL: Final = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")


def _is_array(value: JsonValue) -> bool:
    """A JSON array. `str` and `bytes` are sequences in Python and are not."""
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _equal(before: JsonValue, after: JsonValue) -> bool:
    """§17.2's decimal rule, applied structurally."""
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        return set(before) == set(after) and all(_equal(before[key], after[key]) for key in before)
    if _is_array(before) and _is_array(after):
        return len(before) == len(after) and all(
            _equal(item, other) for item, other in zip(before, after, strict=True)
        )

    # The decimal rule applies *within* a JSON type, never across one. §17.2's
    # concern is a target that reformats a decimal string — `"20.00"` becoming
    # `"20.0"` — and JSON's single number type, where `20` and `20.0` are one
    # value. A path that switched from the number `20` to the string `"20"`
    # changed shape, not formatting, and suppressing that would hide a target
    # silently altering its output contract: precisely the invisible change this
    # module exists to surface. Over-reporting is visible and waivable through
    # `allow_paths`; under-reporting is neither.
    if (isinstance(before, str) and isinstance(after, str)) or (
        _is_number(before) and _is_number(after)
    ):
        left, right = _as_decimal(before), _as_decimal(after)
        if left is not None and right is not None:
            return left == right
        # One side the decimal rule may not reinterpret — a non-finite spelling,
        # an exponent, a padded integer — so the strings answer for themselves.
        # This is what makes the comparison total as well as honest: identical
        # strings are equal whatever they spell, and `"sNaN"` never reaches a
        # `Decimal` comparison that would raise.
        return before == after
    return before == after and type(before) is type(after)


def _is_number(value: JsonValue) -> bool:
    """A JSON number.

    `bool` is excluded explicitly. It subclasses `int` in Python, so without this
    `True` would compare equal to `1` and a flag flipping from `1` to `true`
    would be invisible — a silent change in exactly the tool this feature exists
    to catch.
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


def _as_decimal(value: JsonValue) -> Decimal | None:
    """The decimal value of a number or plain decimal string, or `None`.

    `None` is not "zero" and not "unparseable": it means *this side is not
    something §17.2's decimal rule is allowed to reinterpret*, and `_equal` then
    falls back to exact equality. Both filters below produce it, and the module
    docstring records why each one exists.

    **Finiteness**, the same refusal `engine.assertions._as_exact` already makes.
    A non-finite `Decimal` cannot be compared safely: quiet `NaN` compares
    unequal to itself, and signaling `sNaN` raises from the comparison operator.
    Neither may reach `==`.

    **Plain notation** for strings: `-?(0|[1-9][0-9]*)(\\.[0-9]+)?`. Anything
    else — a leading zero, an exponent, a `+`, a bare `.5`, a non-finite
    spelling — is a *different string*, and only exact equality can say whether
    it changed. Numbers skip this filter because they are not strings: JSON has
    one number type and no spelling to preserve.
    """
    if _is_number(value):
        candidate = Decimal(str(value))
        return candidate if candidate.is_finite() else None
    if isinstance(value, str) and _PLAIN_DECIMAL.fullmatch(value):
        try:
            candidate = Decimal(value)
        except InvalidOperation:  # pragma: no cover - the pattern admits nothing else
            # Kept anyway: this comparison's contract is totality. A diff that
            # raises on an untrusted payload stops verification, and no payload
            # is entitled to do that.
            return None
        return candidate if candidate.is_finite() else None
    return None
