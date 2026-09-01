"""Declared target-effect evidence (FR-032, §13.4, §12.2).

FR-032: "Mutation completions shall also record redacted canonical state hashes
and bounded before/after values for their declared target-effect paths **so
idempotency and false-success evidence do not depend on tool-return text or
later actions**."

That closing clause is the requirement. Without it, deciding whether a mutation
did anything would mean reading the tool's own summary — the channel under test
— or diffing whole snapshots taken at arming and verification, by which time
several other actions may have moved the same paths. Recording the declared
paths either side of *this* call pins the evidence to *this* call.

**An adapter that declares no effect paths gets nothing here, and that is
correct.** §12.2: "missing effect metadata disables only causal false-success
attribution" and the harness must never infer an effect it was not told about.
So an empty declaration produces an empty mapping rather than a guess assembled
from whatever happened to change.

**Absence and null are distinguished.** A path that does not resolve is not a
path whose value is `null`: the first means the observation cannot answer the
question and the second is an answer. Collapsing them would let a missing branch
of the target read as an explicit empty one, which is exactly the confusion
`MISSING` exists to prevent elsewhere in the core.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from actionwitness_core.contracts.paths import ObservationPath, resolve
from actionwitness_core.kernel import JsonValue
from actionwitness_core.ports.models import Observation
from actionwitness_core.security.limits import MAX_FINDING_VALUE_CHARS
from actionwitness_core.security.redaction import RedactionPolicy, redact

__all__ = [
    "TRUNCATION_MARKER",
    "bounded",
    "effect_context",
    "effect_evidence",
    "redacted_observation",
]

#: §11.4 bounds a displayed value and requires "an explicit truncation marker",
#: so a reader can tell a shortened value from a short one.
TRUNCATION_MARKER: Final = "…[truncated]"


def bounded(value: JsonValue, *, limit: int = MAX_FINDING_VALUE_CHARS) -> JsonValue:
    """Bound one value for storage beside an event.

    Only strings are shortened. A number, boolean, or `null` is already small
    and rewriting it as a string would change its type in the evidence — a
    reader comparing `"1"` to `1` would see a difference that never happened.
    Containers are bounded element by element, so the shape survives.
    """
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + TRUNCATION_MARKER
    if isinstance(value, Mapping):
        return {str(key): bounded(item, limit=limit) for key, item in value.items()}
    if isinstance(value, list):
        return [bounded(item, limit=limit) for item in value]
    return value


def effect_evidence(
    effect_paths: Sequence[str | ObservationPath],
    *,
    before: Mapping[str, JsonValue] | None,
    after: Mapping[str, JsonValue] | None,
    policy: RedactionPolicy | None = None,
    limit: int = MAX_FINDING_VALUE_CHARS,
) -> dict[str, JsonValue]:
    """Resolve each declared path either side of one invocation.

    `before` and `after` are evaluation contexts — `Observation.as_context()` —
    so the declared `target.…` paths resolve directly against them.

    Values are redacted *before* they are bounded and stored (§20.3: "before
    persistence, hashing, or export"). Redacting afterwards would mean the
    stored evidence had already been written in the clear once.
    """
    # An observation that could not be taken makes every path unknowable, which
    # is different from a path that resolved and was absent. Deciding it once
    # here keeps the per-path logic about the target rather than about whether
    # the harness managed to look.
    unobserved = before is None or after is None

    evidence: dict[str, JsonValue] = {}
    for declared in effect_paths:
        path = (
            declared if isinstance(declared, ObservationPath) else ObservationPath.parse(declared)
        )
        key = str(path)
        seen_before = _value(path, before, policy, limit)
        seen_after = _value(path, after, policy, limit)
        evidence[key] = {
            "before": seen_before.value,
            "after": seen_after.value,
            # Absence is reported separately from the value, because a path that
            # does not resolve is not a path whose value is `null`.
            "before_present": seen_before.present,
            "after_present": seen_after.present,
            # `None` when the evidence cannot answer — a third outcome, never a
            # quiet `False` and never a `True` inferred from a failed read.
            "changed": None if unobserved else _changed(seen_before, seen_after),
        }
    return evidence


class _Seen:
    """One resolved value plus whether the path resolved at all."""

    __slots__ = ("present", "value")

    def __init__(self, *, present: bool, value: JsonValue) -> None:
        self.present = present
        self.value = value


def _value(
    path: ObservationPath,
    context: Mapping[str, JsonValue] | None,
    policy: RedactionPolicy | None,
    limit: int,
) -> _Seen:
    if context is None:
        return _Seen(present=False, value=None)
    resolution = resolve(path, context)
    # `found`, not `value is None`: §9.4 makes the distinction load-bearing, and
    # a path resolving to a present `null` is an answer while an unresolved one
    # is not.
    if not resolution.found:
        return _Seen(present=False, value=None)
    return _Seen(present=True, value=bounded(redact(resolution.value, policy), limit=limit))


def _changed(before: _Seen, after: _Seen) -> bool | None:
    """Whether this path moved, given that both observations were taken.

    Compared on the redacted, bounded values on purpose: those are what was
    stored, and a comparison against something that was never persisted could
    not be re-derived by a reader of the evidence.

    A path absent from both observations returns `None` rather than `False`. It
    did not change, but saying so would imply the harness watched something —
    and §12.2 forbids inferring anything about a path the target never had.
    """
    if not before.present and not after.present:
        return None
    if before.present != after.present:
        return True
    return before.value != after.value


def redacted_observation(
    observation: Observation, policy: RedactionPolicy | None = None
) -> Observation:
    """Apply a redaction policy to an observation's payload (§20.3).

    §20.3 requires redaction "before persistence, hashing, or export", and
    `Observation.content_hash()` hashes whatever payload it was built with — so
    redacting afterwards would store a hash describing a document nobody kept.

    The redacted observation is then used for **everything**: evaluation,
    hashing, and storage. Evaluating against the unredacted value while storing
    the redacted one would produce a verdict a reader of the evidence could not
    reproduce, which defeats replay (§24). A contract asserting on a redacted
    path therefore fails, and that is the right answer — a contract should not
    be asserting on a secret.

    Metadata is carried through unchanged: `state_version`, `provenance`, and
    the namespace are not business payload (§9.3) and redacting them would break
    the identity of the observation rather than protect anything.
    """
    payload = redact(dict(observation.payload), policy)
    if payload == dict(observation.payload):
        # Nothing matched, so the original is returned rather than a copy that
        # would compare equal but not be identical.
        return observation
    return observation.model_copy(update={"payload": payload})


def effect_context(
    effect_paths: Sequence[str | ObservationPath],
    context: Mapping[str, JsonValue] | None,
    *,
    policy: RedactionPolicy | None = None,
) -> dict[str, JsonValue] | None:
    """The post-call observation, pruned to the declared effect paths.

    `RunEvent.post_call_effect_state` is "a namespace-rooted context fragment",
    because FR-055's false-success test resolves the *assertion's* path against
    it — so it has to be shaped like an evaluation context, not like the
    path-keyed audit mapping `effect_evidence` produces. The two are different
    views of the same reading and both are stored: one is what a person audits,
    the other is what the classifier resolves against.

    Pruned rather than stored whole, because the alternative is a full copy of
    the observation on every invocation event. Only the declared subtrees are
    kept, which is exactly the evidence §13.4 says the adapter vouched for.

    `None` in, `None` out: no observation means no fragment, and FR-055 reads
    that absence as a reason to fall back rather than to accuse.
    """
    if context is None:
        return None

    fragment: dict[str, JsonValue] = {}
    for declared in effect_paths:
        path = (
            declared if isinstance(declared, ObservationPath) else ObservationPath.parse(declared)
        )
        resolution = resolve(path, context)
        if not resolution.found:
            continue
        _graft(fragment, path.segments, redact(resolution.value, policy))
    return fragment


def _graft(target: dict[str, JsonValue], segments: Sequence[str], value: JsonValue) -> None:
    """Place `value` at `segments` inside `target`, creating intermediate maps.

    A shallower path already grafted wins: if `target.cart` is present, a later
    `target.cart.total` is already inside it, and overwriting the subtree with a
    single leaf would lose the rest of what was vouched for.
    """
    node = target
    for segment in segments[:-1]:
        existing = node.get(segment)
        if not isinstance(existing, dict):
            if existing is not None:
                return
            existing = {}
            node[segment] = existing
        node = existing
    leaf = segments[-1]
    if leaf not in node:
        node[leaf] = value
