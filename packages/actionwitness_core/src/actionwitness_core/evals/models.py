"""The regression eval case and its report (§24.1, §9.7–9.8, FR-081, FR-088).

A case is the whole point of M6: a self-contained, versioned document that
replays a recorded failure "without depending on the original browser session"
(§9.7). Everything here serves that portability, and two properties carry it.

**The document is the contract, and its hash covers everything but itself.**
§24.2 step 11 calculates the content hash *last*, after every other field is
final, and §17.2 excludes a document's own `content_hash` member from its hash
input. A field written after hashing would make the hash a claim about a
document nobody kept — the one defect a portable artifact cannot survive, since
its whole job is to be recognisable a year later.

**The expectation is compared by set equality.** §24.1: "`required_classifications`
is compared by exact set equality against actual critical failure
classifications; ordering is ignored and duplicates are collapsed." Containment
would let an unrelated new failure ride along inside a passing eval, which is
precisely the regression a regression suite exists to catch.

Nothing here knows what `pre_fix` means. §24.4's environment mapping is target
knowledge and lives in the integration layer; this module names the profiles and
compares outcomes, which is all a target-neutral core can honestly do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from actionwitness_core.contracts.enums import AssertionSeverity
from actionwitness_core.contracts.models import OutcomeContract
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.evals.enums import (
    ConfirmationStrategy,
    EvalEnvironment,
    EvalStatus,
    SourceProtocol,
)
from actionwitness_core.kernel import ContractError, CoreErrorCode, CoreModel, JsonValue
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.security.canonical import canonical_text, document_content_hash

__all__ = [
    "CASE_SCHEMA_VERSION",
    "EmbeddedContract",
    "EnvironmentExpectation",
    "EvalExpectations",
    "EvalFixture",
    "EvalReport",
    "EvalSource",
    "EvalTarget",
    "RegressionEvalCase",
    "ReplayConfiguration",
    "SourceFinding",
    "SurfaceDelta",
    "SurfaceEvidence",
    "TrajectoryStep",
    "expectation_matches",
]

type Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
type ContentHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
type SchemaVersion = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+$")]

#: The generator's own version, part of FR-080's idempotence key. Bumping it is
#: how a change to case *contents* becomes a new case rather than silently
#: altering what an existing hash refers to.
CASE_SCHEMA_VERSION: Literal["1.0"] = "1.0"


class EvalSource(CoreModel):
    """Where the case came from (§24.1 `source`, FR-081).

    `failure_profile` is provenance, not configuration. §24.2 step 9 records it
    "without automatically activating it in ordinary CI runs" — a case that
    forced its own fault profile would make `current` untestable, which is the
    profile CI actually runs.
    """

    protocol: SourceProtocol = SourceProtocol.WEBMCP
    run_id: Identifier
    implementation_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    build_commit: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    scenario_mode: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    failure_profile: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    overall_result: LayerResult
    critical_classifications: tuple[FailureClassification, ...] = ()

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "protocol": self.protocol.value,
            "run_id": self.run_id,
            "implementation_version": self.implementation_version,
            "build_commit": self.build_commit,
            "scenario_mode": self.scenario_mode,
            "failure_profile": self.failure_profile,
            "overall_result": self.overall_result.value,
            # Sorted and de-duplicated: §17.2 normalizes unordered collections
            # used in hashes, and a set written in encounter order would make
            # two identical cases hash differently.
            "critical_classifications": sorted(
                {item.value for item in self.critical_classifications}
            ),
        }


class EvalTarget(CoreModel):
    """Which adapter replays this case (§24.1 `target`, FR-084)."""

    type: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    id: Identifier
    adapter: Annotated[str, StringConstraints(min_length=1, max_length=128)]

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"type": self.type, "id": self.id, "adapter": self.adapter}


class EvalFixture(CoreModel):
    """The initial state a replay restores (§24.1 `fixture`, §24.2 step 2).

    `complete` records whether the fixture was minimized or retained whole. A
    contract carrying `no_undeclared_changes` is defined over paths it does not
    name, so a minimized fixture would make that policy unevaluable — and the
    runner has to be able to tell "small because nothing else mattered" from
    "small because somebody trimmed it wrongly".
    """

    state_version: Annotated[int, Field(ge=0)] = 0
    content_hash: ContentHash
    target_state: Mapping[str, JsonValue]
    complete: bool = False

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "state_version": self.state_version,
            "content_hash": self.content_hash,
            "target_state": dict(self.target_state),
            "complete": self.complete,
        }


class TrajectoryStep(CoreModel):
    """One replayed call (§24.1 `trajectory`, FR-086).

    Arguments only — no code, no URL, no shell. FR-086 makes that a safety
    property: a case is data a CI job executes, so anything executable in it
    would be arbitrary code execution wearing a fixture's clothes.
    """

    sequence: Annotated[int, Field(ge=1)]
    tool: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    arguments: Mapping[str, JsonValue] = Field(default_factory=dict)

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "sequence": self.sequence,
            "tool": self.tool,
            "arguments": dict(self.arguments),
        }


class EmbeddedContract(CoreModel):
    """The source contract, verbatim, with the hash it was verified against.

    §24.2 step 6 embeds the document and verifies its stored hash *before* case
    creation. Embedding rather than referencing is what makes the case
    self-contained (FR-082): a case that pointed at a contract row could not be
    handed to anyone.
    """

    content_hash: ContentHash
    document: OutcomeContract

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "content_hash": self.content_hash,
            "document": self.document.canonical_document(),
        }


class SourceFinding(CoreModel):
    """One finding the source run produced (§24.1 `source_findings`)."""

    check_id: Identifier
    classification: FailureClassification | None = None
    severity: AssertionSeverity
    status: CheckStatus
    path: Annotated[str, StringConstraints(min_length=1, max_length=256)] | None = None
    expected: JsonValue = None
    actual: JsonValue = None

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "check_id": self.check_id,
            "classification": None if self.classification is None else self.classification.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "path": self.path,
            "expected": self.expected,
            "actual": self.actual,
        }


class SurfaceDelta(CoreModel):
    """One recorded change to the published tool surface (§24.3a)."""

    sequence: Annotated[int, Field(ge=1)]
    kind: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    partition: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    tool: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "sequence": self.sequence,
            "kind": self.kind,
            "partition": self.partition,
            "tool": self.tool,
        }


class SurfaceEvidence(CoreModel):
    """§24.3a's tool-surface baseline and deltas.

    Recorded because a headless replay cannot regenerate it: without this a
    `tool_surface_poisoned` case "could never reproduce its own classification
    and would fail permanently".
    """

    baseline: tuple[str, ...] = ()
    deltas: tuple[SurfaceDelta, ...] = ()

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "baseline": list(self.baseline),
            "deltas": [delta.canonical_document() for delta in self.deltas],
        }


class ReplayConfiguration(CoreModel):
    """How the case replays (§24.1 `replay`, §24.5).

    `default_environment` is `current` and §24.4 forbids a generated case from
    silently forcing the other — a case that defaulted to `reproduce_source`
    would report a reproduced failure as routine CI success.
    """

    default_environment: EvalEnvironment = EvalEnvironment.CURRENT
    confirmation_strategy: ConfirmationStrategy = ConfirmationStrategy.NO_CONFIRMATION

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "default_environment": self.default_environment.value,
            "confirmation_strategy": self.confirmation_strategy.value,
        }


class EnvironmentExpectation(CoreModel):
    """What one profile must produce for the eval to pass (§24.1 `expected`)."""

    overall_result: LayerResult
    required_classifications: tuple[FailureClassification, ...] = ()

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "overall_result": self.overall_result.value,
            "required_classifications": sorted(
                {item.value for item in self.required_classifications}
            ),
        }

    def classification_set(self) -> frozenset[FailureClassification]:
        """Ordering ignored, duplicates collapsed (§24.1)."""
        return frozenset(self.required_classifications)


class EvalExpectations(CoreModel):
    """Both profiles' expectations. Every case carries both (§24.2 steps 7–8)."""

    current: EnvironmentExpectation
    reproduce_source: EnvironmentExpectation

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "current": self.current.canonical_document(),
            "reproduce_source": self.reproduce_source.canonical_document(),
        }

    def for_environment(self, environment: EvalEnvironment) -> EnvironmentExpectation:
        return self.current if environment is EvalEnvironment.CURRENT else self.reproduce_source


class RegressionEvalCase(CoreModel):
    """§24.1's case document.

    Self-contained by construction (FR-082): the contract is embedded, the
    fixture is data, the trajectory is arguments, and nothing here references a
    row, a credential, or a private package.
    """

    schema_version: SchemaVersion = CASE_SCHEMA_VERSION
    id: Identifier
    name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    source: EvalSource
    target: EvalTarget
    fixture: EvalFixture
    trajectory: tuple[TrajectoryStep, ...]
    contract: EmbeddedContract
    source_findings: tuple[SourceFinding, ...] = ()
    surface: SurfaceEvidence | None = None
    #: §24.3a: policies that cannot be evaluated in replay. Excluded from both
    #: classification sets *and* named in the report, so a passing eval can
    #: never quietly mean "not checked".
    non_replayable_policies: tuple[str, ...] = ()
    replay: ReplayConfiguration = Field(default_factory=ReplayConfiguration)
    expected: EvalExpectations

    @model_validator(mode="after")
    def _trajectory_is_dense_and_ordered(self) -> RegressionEvalCase:
        """Sequences run 1..n with no gaps.

        A gap would mean a step was dropped after numbering, and a replay would
        silently perform a different journey than the one recorded — the exact
        way a case comes to pass for a reason other than the one it was cut for.
        """
        sequences = [step.sequence for step in self.trajectory]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ContractError(
                f"trajectory sequences must be 1..{len(sequences)} with no gaps, got {sequences}",
                code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        """Every field except the hash itself, in a stable order."""
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "name": self.name,
            "source": self.source.canonical_document(),
            "target": self.target.canonical_document(),
            "fixture": self.fixture.canonical_document(),
            "trajectory": [step.canonical_document() for step in self.trajectory],
            "contract": self.contract.canonical_document(),
            "source_findings": [finding.canonical_document() for finding in self.source_findings],
            "surface": None if self.surface is None else self.surface.canonical_document(),
            "non_replayable_policies": sorted(set(self.non_replayable_policies)),
            "replay": self.replay.canonical_document(),
            "expected": self.expected.canonical_document(),
        }

    def content_hash(self) -> str:
        """§24.2 step 11: computed over everything else, last.

        `document_content_hash` drops a top-level `content_hash` member, so a
        stored case and the live object hash identically — which is what lets a
        reader verify a case they were handed without trusting the sender.
        """
        return document_content_hash(self.canonical_document())

    def as_stored_document(self) -> dict[str, JsonValue]:
        """The case as it is written to disk: the document plus its own hash."""
        return {**self.canonical_document(), "content_hash": self.content_hash()}

    def canonical_bytes(self) -> bytes:
        """The exact bytes a stored case holds. Byte-identical for equal cases."""
        return canonical_text(self.as_stored_document()).encode("utf-8")


def expectation_matches(
    expectation: EnvironmentExpectation,
    *,
    actual_result: LayerResult,
    actual_classifications: Sequence[FailureClassification],
    non_replayable: Sequence[str] = (),
) -> bool:
    """Whether a replay met its expectation (§24.1, §24.3).

    **Set equality, not containment.** An extra critical classification is a
    different failure than the one the case was cut from, and a suite that
    accepted supersets would pass while a new regression rode along inside it.

    `non_replayable` is accepted so a caller cannot forget that §24.3a excludes
    unevaluable policies from *both* sides; it is unused in the comparison
    itself because those classifications were already removed from both sets
    before they arrived here, and taking the argument keeps that visible at
    every call site.
    """
    if actual_result is not expectation.overall_result:
        return False
    return frozenset(actual_classifications) == expectation.classification_set()


class EvalReport(CoreModel):
    """FR-088's canonical report.

    Every field FR-088 names is present, and three of them exist so a passing
    eval cannot hide what produced it: the selected `environment`, the
    `actual_classifications` beside the `expected_classifications`, and
    `non_replayable_policies`. §24.4: "so a passing eval cannot hide the
    environment or failure it produced."
    """

    schema_version: SchemaVersion = CASE_SCHEMA_VERSION
    eval_case_id: Identifier
    eval_case_hash: ContentHash
    implementation_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    build_commit: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    environment: EvalEnvironment
    status: EvalStatus
    #: What the target actually did. Deliberately a different field from
    #: `status`: a reproduced failure is `overall_result: failed` with
    #: `status: passed`, and collapsing them is the misreading §24.3 warns of.
    overall_result: LayerResult | None = None
    actual_classifications: tuple[FailureClassification, ...] = ()
    expected_classifications: tuple[FailureClassification, ...] = ()
    classification_match: bool = False
    replayed_trajectory: tuple[TrajectoryStep, ...] = ()
    final_state: Mapping[str, JsonValue] | None = None
    non_replayable_policies: tuple[str, ...] = ()
    detail: Annotated[str, StringConstraints(max_length=1024)] = ""

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "eval_case_id": self.eval_case_id,
            "eval_case_hash": self.eval_case_hash,
            "implementation_version": self.implementation_version,
            "build_commit": self.build_commit,
            "environment": self.environment.value,
            "status": self.status.value,
            "overall_result": None if self.overall_result is None else self.overall_result.value,
            "actual_classifications": sorted({c.value for c in self.actual_classifications}),
            "expected_classifications": sorted({c.value for c in self.expected_classifications}),
            "classification_match": self.classification_match,
            "replayed_trajectory": [step.canonical_document() for step in self.replayed_trajectory],
            "final_state": None if self.final_state is None else dict(self.final_state),
            "non_replayable_policies": sorted(set(self.non_replayable_policies)),
            "detail": self.detail,
        }

    def content_hash(self) -> str:
        return document_content_hash(self.canonical_document())

    def as_stored_document(self) -> dict[str, JsonValue]:
        return {**self.canonical_document(), "content_hash": self.content_hash()}
