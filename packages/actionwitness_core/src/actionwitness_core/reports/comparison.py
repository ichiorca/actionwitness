"""Matched pre/post scenario comparison (FR-019, §23.7, §17.1).

FR-019: a pair is linked "only when target adapter, contract, fixture,
normalized intent, scalar parameters, and actual tool trajectory ... match
**while scenario modes differ**. ... A mismatched pair remains an ordinary rerun
and is labeled `not_comparable` with differing fields."

Two ideas do all the work here.

**A comparison is a controlled experiment, so exactly one variable may move.**
The scenario mode is that variable. Everything else — the adapter, the contract,
the fixture, the intent, the profile, the implementation — is held fixed, and if
any of it moved the two runs are not evidence about the scenario; they are two
different experiments. §17.1 says the same thing in storage terms:
`comparison_key_hash` is a "hash of every controlled input except scenario mode
and derived fault activation".

**A mismatch is not an error.** §23.7 and FR-019 both say the rerun "remains an
ordinary rerun" — it is a perfectly good run that simply cannot be read as the
other one's counterpart. Reporting that as a failure would push somebody to make
the pair match by weakening what they meant to test; reporting it as a labelled
result with the differing fields lets them decide.

**The trajectory is compared, not hashed into the key.** §17.1 scopes the key to
controlled *inputs*, and the trajectory is an outcome — the agent chose it. So
it is compared separately and named in `differing_fields` when it moved, which
also lets a reader tell "you configured it differently" apart from "the agent
did something different this time".
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final

from pydantic import Field, StringConstraints

from actionwitness_core.engine.enums import FailureClassification
from actionwitness_core.journeys.enums import RunState
from actionwitness_core.journeys.transitions import is_terminal
from actionwitness_core.kernel import CoreModel, JsonValue
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.security.canonical import content_hash

__all__ = [
    "COMPARISON_KEY_FIELDS",
    "ComparableRun",
    "ComparisonResult",
    "compare_runs",
]

type Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]

#: The controlled inputs §17.1 hashes into `comparison_key_hash`. Named here so
#: the set is one list rather than a shape repeated at each call site, and so a
#: reader can see at a glance what a comparison holds fixed.
#:
#: `scenario_mode` and `fault_active` are deliberately absent: the first is the
#: variable under study and the second is derived from it.
COMPARISON_KEY_FIELDS: Final[tuple[str, ...]] = (
    "target_id",
    "adapter_id",
    "contract_content_hash",
    "fixture_content_hash",
    "intent_content_hash",
    "failure_profile",
    "implementation_version",
    "build_commit",
)


class ComparableRun(CoreModel):
    """Everything a comparison needs about one run.

    Assembled by the caller from stored evidence rather than fetched here, so
    the comparison stays pure and a replay reaches the same verdict from the
    same rows.
    """

    run_id: Identifier
    status: RunState
    scenario_mode: Identifier
    #: Derived by the adapter, not by the harness (§9.1, §12.2). Displayed in
    #: the pair and excluded from the key.
    fault_active: bool = False

    target_id: Identifier
    adapter_id: Identifier
    contract_content_hash: str | None = None
    fixture_content_hash: str | None = None
    intent_content_hash: str | None = None
    failure_profile: str | None = None
    implementation_version: str
    build_commit: str | None = None

    #: The ordered agent tool calls actually observed (§10.3, FR-019's "actual
    #: tool trajectory"). An outcome, not an input.
    trajectory: tuple[str, ...] = ()
    overall_result: LayerResult | None = None
    critical_classifications: tuple[FailureClassification, ...] = ()

    def comparison_key(self) -> str:
        """§17.1's `comparison_key_hash`.

        Hashed over the canonical document of the controlled inputs, so two runs
        configured identically produce the same key on any machine and a reader
        can recompute it from the stored columns.
        """
        return content_hash(self.controlled_inputs())

    def controlled_inputs(self) -> dict[str, JsonValue]:
        return {field: getattr(self, field) for field in COMPARISON_KEY_FIELDS}

    def summary(self) -> dict[str, JsonValue]:
        """What §23.7 shows for each side of the pair."""
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "scenario_mode": self.scenario_mode,
            "fault_active": self.fault_active,
            "implementation_version": self.implementation_version,
            "build_commit": self.build_commit,
            "overall_result": None if self.overall_result is None else self.overall_result.value,
            "critical_classifications": [item.value for item in self.critical_classifications],
            "comparison_key_hash": self.comparison_key(),
        }


class ComparisonResult(CoreModel):
    """A matched pair, or a labelled explanation of why it is not one."""

    comparable: bool
    #: Populated only when `comparable` is false. Named fields rather than a
    #: sentence, because a reader has to be able to act on it (§23.7: "lists the
    #: differing fields").
    differing_fields: tuple[str, ...] = ()
    reason: str = ""
    source: dict[str, JsonValue] = Field(default_factory=dict)
    candidate: dict[str, JsonValue] = Field(default_factory=dict)
    #: §23.7: "whether the original critical classification disappeared". `None`
    #: when the pair is not comparable, because the question is only meaningful
    #: of a matched pair.
    resolved_classifications: tuple[str, ...] | None = None
    introduced_classifications: tuple[str, ...] | None = None

    def as_document(self) -> dict[str, JsonValue]:
        document: dict[str, JsonValue] = {
            "comparable": self.comparable,
            "source": self.source,
            "candidate": self.candidate,
        }
        if self.comparable:
            document["resolved_classifications"] = list(self.resolved_classifications or ())
            document["introduced_classifications"] = list(self.introduced_classifications or ())
        else:
            document["differing_fields"] = list(self.differing_fields)
            document["reason"] = self.reason
        return document


def compare_runs(source: ComparableRun, candidate: ComparableRun) -> ComparisonResult:
    """Decide whether two runs form a matched pre/post pair (FR-019).

    Returns a result either way. A pair that does not match is `not_comparable`
    with its differing fields, never an error — the rerun is still an ordinary
    run, and treating the mismatch as a failure would push somebody to make the
    pair match by weakening what they meant to test.
    """
    differing: list[str] = []
    reasons: list[str] = []

    # "After both runs terminate" — a run still in flight has no outcome to
    # compare, and comparing against a partial one would read as a result.
    for label, run in (("source", source), ("candidate", candidate)):
        if not is_terminal(run.status):
            differing.append(f"{label}.status")
            reasons.append(f"the {label} run has not terminated")

    # The variable under study must actually have moved. Two runs in the same
    # mode are a repetition, not a comparison, and labelling them a pre/post
    # pair would claim an experiment nobody ran.
    if source.scenario_mode == candidate.scenario_mode:
        differing.append("scenario_mode")
        reasons.append(
            f"both runs used scenario mode {source.scenario_mode!r}, so nothing was varied"
        )

    for field in COMPARISON_KEY_FIELDS:
        if getattr(source, field) != getattr(candidate, field):
            differing.append(field)
    if any(field in differing for field in COMPARISON_KEY_FIELDS):
        reasons.append("a controlled input differs, so the two runs are different experiments")

    if source.trajectory != candidate.trajectory:
        differing.append("trajectory")
        reasons.append(
            "the agent's observed tool calls differ, so the runs exercised different paths"
        )

    if differing:
        return ComparisonResult(
            comparable=False,
            differing_fields=tuple(dict.fromkeys(differing)),
            reason="; ".join(dict.fromkeys(reasons)),
            source=source.summary(),
            candidate=candidate.summary(),
        )

    resolved = tuple(
        item.value
        for item in source.critical_classifications
        if item not in candidate.critical_classifications
    )
    introduced = tuple(
        item.value
        for item in candidate.critical_classifications
        if item not in source.critical_classifications
    )
    return ComparisonResult(
        comparable=True,
        source=source.summary(),
        candidate=candidate.summary(),
        resolved_classifications=resolved,
        introduced_classifications=introduced,
    )


def trajectory_of(calls: Sequence[str]) -> tuple[str, ...]:
    """The observed call sequence as a comparable value.

    A tuple rather than a hash, so a mismatch can show *what* differed rather
    than only that something did.
    """
    return tuple(calls)
