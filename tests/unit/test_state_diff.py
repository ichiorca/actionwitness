"""013-T1 — the FR-157 full-state diff.

FR-157 requires a *complete* recursive diff "independent of which paths the
contract names", using §17.2's canonicalisation and decimal handling. The tests
below are grouped by the three properties the rest of the feature rests on:
what counts as a change, where a change is reported, and whether the same input
produces the same bytes.

The decimal cases are the ones worth reading first. §17.2 says a target that
reformats a decimal must not produce a spurious `undeclared_state_change`, and
that failure would be critical-severity and caused by cosmetics.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.paths import ObservationPath
from actionwitness_core.engine.diff import (
    MAX_CHANGE_EXCERPT_CHARS,
    ChangeKind,
    StateChange,
    changed_paths_of,
    diff_states,
    values_differ,
)

pytestmark = pytest.mark.unit


def paths_of(changes: tuple[StateChange, ...]) -> list[str]:
    return [str(change.path) for change in changes]


def kinds_of(changes: tuple[StateChange, ...]) -> dict[str, ChangeKind]:
    return {str(change.path): change.kind for change in changes}


# --- what counts as a change (§17.2) ----------------------------------------


def test_an_identical_document_reports_nothing() -> None:
    state = {"target": {"cart": {"total": "25.00", "items": {"mug": {"quantity": 1}}}}}

    assert diff_states(state, state) == ()


@pytest.mark.parametrize(
    "before,after",
    [
        ("20.00", "20.0"),
        ("20.0", "20"),
        ("0.10", "0.1000"),
        ("-0.0", "0.0"),
    ],
)
def test_a_reformatted_decimal_is_not_a_change(before: str, after: str) -> None:
    """§17.2, normative: comparison uses `Decimal` equality, not string equality.

    Without this a target that renders `20.0` where it once rendered `20.00`
    fails a critical policy for a cosmetic difference — which is exactly the
    spurious `undeclared_state_change` the rule exists to prevent.

    Every case here is plain decimal notation differing only in how the
    fraction is written, which is the whole of what §17.2's example licenses.
    `"1E+2"` used to sit in this list and now does not: see
    `test_a_respelled_number_string_is_a_change`.
    """
    assert not values_differ(before, after)
    assert diff_states({"target": {"total": before}}, {"target": {"total": after}}) == ()


def test_an_actual_decimal_change_is_still_a_change() -> None:
    """The guard on the rule above: it must not swallow real movement."""
    changes = diff_states({"target": {"total": "25.00"}}, {"target": {"total": "20.00"}})

    assert paths_of(changes) == ["target.total"]
    assert changes[0].kind is ChangeKind.CHANGED
    assert changes[0].before == '"25.00"'
    assert changes[0].after == '"20.00"'


@pytest.mark.parametrize("spelling", ["NaN", "nan", "sNaN", "Infinity", "-Infinity"])
def test_a_non_finite_spelling_compares_as_a_string_and_never_raises(spelling: str) -> None:
    """`Decimal` parses these, and neither result can be compared safely.

    Two quiet `NaN`s compare *unequal*, which reported an untouched path as
    changed — a spurious critical `undeclared_state_change` on state that never
    moved. A signaling `sNaN` raises `InvalidOperation` from the comparison
    itself, so one string in an untrusted target payload stopped verification
    mid-diff. Both are excluded from the decimal rule, so the strings answer
    for themselves and identical ones are identical.
    """
    state = {"target": {"x": spelling}}

    assert not values_differ(spelling, spelling)
    assert diff_states(state, dict(state)) == ()


def test_two_different_non_finite_spellings_are_a_change() -> None:
    """The guard on the rule above: falling back to strings must still see movement."""
    changes = diff_states({"target": {"x": "NaN"}}, {"target": {"x": "Infinity"}})

    assert paths_of(changes) == ["target.x"]
    assert changes[0].kind is ChangeKind.CHANGED


@pytest.mark.parametrize(
    "before,after",
    [
        ("007", "7"),
        ("1e2", "100"),
        ("1E+2", "100"),
        ("+7", "7"),
        (".5", "0.5"),
    ],
)
def test_a_respelled_number_string_is_a_change(before: str, after: str) -> None:
    """A numeric-looking SKU, code, or identifier that was rewritten really changed.

    These pairs are equal as `Decimal` values and are not the same string, and
    treating them as equal made a real edit invisible — the outcome this
    module's docstring calls the worst one available, because under-reporting is
    neither visible nor waivable through `allow_paths`. §17.2's normative
    example is a reformatted decimal *fraction*, so the rule is confined to
    plain decimal notation and every other spelling compares exactly.
    """
    assert values_differ(before, after)
    assert paths_of(diff_states({"t": {"sku": before}}, {"t": {"sku": after}})) == ["t.sku"]


def test_a_boolean_never_compares_equal_to_a_number() -> None:
    """`bool` subclasses `int` in Python, so `True == 1` without an explicit guard.

    A flag flipping from `1` to `true` would then be invisible — a silent state
    change in the one function whose job is to notice silent state changes.
    """
    assert values_differ(True, 1)
    assert values_differ(False, 0)
    assert paths_of(diff_states({"t": {"paid": 1}}, {"t": {"paid": True}})) == ["t.paid"]


def test_a_number_and_its_decimal_string_are_different_values() -> None:
    """One is JSON's number type and the other is a string; the shape changed."""
    assert values_differ(20, "20")


def test_null_is_a_value_and_not_an_absence() -> None:
    """§9.4 makes present-null distinct from missing, and the diff agrees.

    A key set to null was *changed*; a key that disappeared was *removed*. The
    two are different events with different causes.
    """
    changed = diff_states({"t": {"discount": {"code": "SAVE20"}}}, {"t": {"discount": None}})
    removed = diff_states({"t": {"discount": {"code": "SAVE20"}}}, {"t": {}})

    assert kinds_of(changed) == {"t.discount": ChangeKind.CHANGED}
    assert kinds_of(removed) == {"t.discount": ChangeKind.REMOVED}


# --- where a change is reported ---------------------------------------------


def test_a_leaf_change_is_reported_at_the_leaf() -> None:
    before = {"target": {"cart": {"items": {"mug": {"quantity": 1, "unit_price": "25.00"}}}}}
    after = {"target": {"cart": {"items": {"mug": {"quantity": 2, "unit_price": "25.00"}}}}}

    assert paths_of(diff_states(before, after)) == ["target.cart.items.mug.quantity"]


def test_a_removed_subtree_is_one_change_at_its_root() -> None:
    """Not one change per descendant.

    Two reasons, and the second is load bearing: the undeclared-path list stays
    proportional to what happened rather than to how deep the document is, and a
    whole-subtree removal can be covered by an effect prefix naming that subtree
    (§13.4) instead of needing a prefix per leaf.
    """
    before = {"t": {"order": {"id": "order-1", "lines": [{"sku": "mug"}], "total": "25.00"}}}
    after = {"t": {}}

    changes = diff_states(before, after)

    assert paths_of(changes) == ["t.order"]
    assert changes[0].kind is ChangeKind.REMOVED


def test_a_shape_change_is_one_change_rather_than_a_removal_and_an_addition() -> None:
    """One thing happened to one path; reporting it twice would double-count it."""
    changes = diff_states({"t": {"note": {"text": "hi"}}}, {"t": {"note": "hi"}})

    assert kinds_of(changes) == {"t.note": ChangeKind.CHANGED}


def test_arrays_compare_positionally() -> None:
    """RFC 8785 preserves array order, so position is a stable identity.

    Matching elements by content instead would need a rule for which of two
    reorderings counts as "the same list", and any such rule is a guess that
    silently changes what a policy failure means.
    """
    changes = diff_states({"t": {"lines": ["a", "b"]}}, {"t": {"lines": ["b", "a"]}})

    assert paths_of(changes) == ["t.lines.0", "t.lines.1"]


def test_a_lengthened_array_reports_the_new_index_as_added() -> None:
    changes = diff_states({"t": {"lines": ["a"]}}, {"t": {"lines": ["a", "b"]}})

    assert kinds_of(changes) == {"t.lines.1": ChangeKind.ADDED}
    assert changes[0].before is None, "an added path has no before value"


def test_a_shortened_array_reports_the_lost_index_as_removed() -> None:
    changes = diff_states({"t": {"lines": ["a", "b"]}}, {"t": {"lines": ["a"]}})

    assert kinds_of(changes) == {"t.lines.1": ChangeKind.REMOVED}
    assert changes[0].after is None, "a removed path has no after value"


def test_an_array_index_is_an_ordinary_path_segment() -> None:
    """So an effect prefix naming the array covers changes inside it (§13.4)."""
    changes = diff_states(
        {"t": {"cart": {"lines": [{"quantity": 1}]}}},
        {"t": {"cart": {"lines": [{"quantity": 2}]}}},
    )
    prefix = ObservationPath.parse("t.cart.lines")

    assert paths_of(changes) == ["t.cart.lines.0.quantity"]
    assert prefix.is_ancestor_of(changes[0].path)


def test_a_key_that_cannot_be_a_path_segment_is_reported_at_its_parent() -> None:
    """Never silently dropped.

    A key with a space cannot be named in a dotted path — the path would not
    parse back to itself. Skipping it would let a change vanish, and this whole
    feature exists to stop a change from vanishing, so the parent is reported
    instead and the change still reaches the undeclared set.
    """
    before = {"t": {"prefs": {"2 for 1": False}}}
    after = {"t": {"prefs": {"2 for 1": True}}}

    changes = diff_states(before, after)

    assert paths_of(changes) == ["t.prefs"]
    assert changes[0].kind is ChangeKind.CHANGED
    # And the reported path is a real one: it parses back to itself.
    assert ObservationPath.parse(str(changes[0].path)) == changes[0].path


def test_an_unrepresentable_namespace_is_refused_rather_than_absorbed() -> None:
    """At the top level there is no parent to fall back to.

    `Observation` validates a namespace as a `Token` before a snapshot can
    exist, so reaching this means a context was assembled by something that
    skipped that validation — and dropping it would hide a whole provider.
    """
    with pytest.raises(ValueError, match="namespaces are not valid path segments"):
        diff_states({"not a token": {"a": 1}}, {"not a token": {"a": 2}})


# --- determinism (acceptance criterion 1) ------------------------------------


def test_the_same_snapshots_produce_a_byte_identical_result() -> None:
    """Exit-gate criterion 1, at the level this function can answer it."""
    before = {"target": {"cart": {"items": {"mug": {"q": 1}}, "total": "25.00"}, "prefs": {}}}
    after = {"target": {"cart": {"items": {"mug": {"q": 2}}, "total": "50.00"}, "prefs": {"a": 1}}}

    first = diff_states(before, after)
    second = diff_states(dict(before), dict(after))

    assert first == second
    assert [change.canonical_document() for change in first] == [
        change.canonical_document() for change in second
    ]


def test_ordering_does_not_depend_on_key_insertion_order() -> None:
    """A dict built in a different order is the same document."""
    before = {"t": {"a": 1, "b": 1}}
    after_one = {"t": {"a": 2, "b": 2}}
    after_two = {"t": {"b": 2, "a": 2}}

    assert diff_states(before, after_one) == diff_states(before, after_two)


def test_changes_are_ordered_lexicographically_by_path() -> None:
    before = {"t": {"z": 1, "a": 1, "m": {"y": 1, "b": 1}}}
    after = {"t": {"z": 2, "a": 2, "m": {"y": 2, "b": 2}}}

    assert paths_of(diff_states(before, after)) == ["t.a", "t.m.b", "t.m.y", "t.z"]


def test_changed_paths_of_preserves_that_order() -> None:
    before = {"t": {"z": 1, "a": 1}}
    after = {"t": {"z": 2, "a": 2}}

    changes = diff_states(before, after)

    assert [str(path) for path in changed_paths_of(changes)] == ["t.a", "t.z"]


# --- bounded excerpts (§11.4) -------------------------------------------------


def test_a_large_value_is_excerpted_rather_than_copied() -> None:
    """A report must never carry an unbounded payload out of a snapshot."""
    before = {"t": {"blob": "x" * 5_000}}
    after = {"t": {"blob": "y" * 5_000}}

    changes = diff_states(before, after)
    excerpt = changes[0].after

    assert excerpt is not None
    assert len(excerpt) <= MAX_CHANGE_EXCERPT_CHARS
    assert "…[truncated]" in excerpt


def test_a_small_value_is_not_marked_as_truncated() -> None:
    changes = diff_states({"t": {"a": "short"}}, {"t": {"a": "also short"}})

    assert changes[0].after == '"also short"'
