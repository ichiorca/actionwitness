"""Safe minimization of a case's fixture and trajectory (§24.2 steps 2–4).

Minimization is where a regression case quietly stops testing what it was cut
for, so every rule here errs toward keeping evidence. The cost of keeping too
much is a larger file; the cost of dropping too much is a case that passes for a
reason nobody intended, which is worse than having no case at all.

The three rules, and what each protects:

**Keep the whole state under `no_undeclared_changes`** (step 2). That policy is
defined over paths the contract does *not* name, so pruning to the named paths
would remove exactly the evidence it judges — and the policy would then pass
vacuously on every replay.

**Drop a read-only call only when it is irrelevant three ways over** (step 3):
to every contract check, to ordering, and to every later mutation. A
`search_catalog` that produced the product id a later `update_cart` used is not
irrelevant, even though nothing asserts on it.

**Never drop a mutation, and never collapse repeated request IDs** (step 4). An
idempotency failure *is* a repeated request id; minimizing it away deletes the
bug the case exists to reproduce.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from actionwitness_core.contracts.enums import PolicyType
from actionwitness_core.contracts.models import OutcomeContract
from actionwitness_core.kernel import JsonValue

__all__ = [
    "minimize_fixture",
    "prune_trajectory",
    "referenced_roots",
    "requires_complete_state",
]

#: The state root a contract path addresses. §9.3 mounts observations under
#: `target.`, so `target.cart.total` keeps the `cart` subtree.
_TARGET_PREFIX = "target."


def requires_complete_state(contract: OutcomeContract) -> bool:
    """Whether §24.2 step 2's exception applies.

    `no_undeclared_changes` asserts about paths the contract never names, so it
    can only be evaluated against the state in full.
    """
    return any(policy.type is PolicyType.NO_UNDECLARED_CHANGES for policy in contract.policies)


def referenced_roots(contract: OutcomeContract) -> frozenset[str]:
    """Top-level state keys the contract mentions, from every path it carries.

    Preconditions count as well as assertions: a replay whose precondition
    cannot be evaluated is a replay that cannot start, and a fixture missing
    that subtree would fail for a reason unrelated to the regression.
    """
    paths = [assertion.path for assertion in contract.assertions]
    paths.extend(precondition.path for precondition in contract.preconditions)

    roots: set[str] = set()
    for path in paths:
        text = str(path)
        if not text.startswith(_TARGET_PREFIX):
            continue
        remainder = text[len(_TARGET_PREFIX) :]
        root = remainder.split(".", 1)[0].split("[", 1)[0]
        if root:
            roots.add(root)
    return frozenset(roots)


def minimize_fixture(
    state: Mapping[str, JsonValue], contract: OutcomeContract
) -> tuple[dict[str, JsonValue], bool]:
    """Return the fixture to store and whether it is the complete state.

    Returns the state untouched when the contract needs all of it, or when
    pruning would leave nothing — a fixture pruned to empty would restore a
    target with no starting point, and "the contract named no target paths" is
    not the same fact as "the target starts empty".
    """
    if requires_complete_state(contract):
        return dict(state), True

    roots = referenced_roots(contract)
    if not roots:
        return dict(state), True

    kept = {key: value for key, value in state.items() if key in roots}
    if not kept:
        return dict(state), True
    return kept, kept.keys() == state.keys()


def prune_trajectory(
    steps: Sequence[tuple[int, str, Mapping[str, JsonValue]]],
    contract: OutcomeContract,
    read_only_tools: frozenset[str],
) -> list[tuple[int, str, Mapping[str, JsonValue]]]:
    """Drop only the read-only calls §24.2 step 3 allows, and renumber.

    A step survives unless it is read-only **and** absent from `expected_tools`
    **and** named by no policy **and** followed by no later call that could have
    used its output. That last clause is the conservative one: this cannot know
    what an argument was derived from, so any read-only call with a mutation
    after it is kept. The alternative — reasoning about data flow from recorded
    arguments — would be guessing, and guessing wrong deletes the setup a
    replay needs.

    Sequences are renumbered densely afterwards, because the case model requires
    1..n with no gaps and a gap would itself be evidence of a dropped step.
    """
    expected = frozenset(contract.expected_tools.calls if contract.expected_tools else ())
    policed = frozenset(
        str(getattr(policy, "tool", "")) for policy in contract.policies
    ) - frozenset({""})

    mutation_positions = [
        index for index, (_, tool, _) in enumerate(steps) if tool not in read_only_tools
    ]
    last_mutation = max(mutation_positions) if mutation_positions else -1

    kept: list[tuple[int, str, Mapping[str, JsonValue]]] = []
    for index, (_, tool, arguments) in enumerate(steps):
        droppable = (
            tool in read_only_tools
            and tool not in expected
            and tool not in policed
            # Nothing after it could have consumed its output.
            and index > last_mutation
        )
        if droppable:
            continue
        kept.append((len(kept) + 1, tool, dict(arguments)))
    return kept
