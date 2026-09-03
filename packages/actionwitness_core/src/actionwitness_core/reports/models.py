"""The versioned layered outcome report (spec v1.9 §23.1).

Also FR-070 (the layered result), FR-073 (a stable JSON report schema), §17.2 (an
artifact hash "covers the complete top-level object except its own top-level
`content_hash` member"), §22 (`primary_failure` selection); BUILD_ORDER invariant
10 ("model-selection, observed-trajectory, execution, business-outcome, and
safety-policy layers remain distinct").

The five layers are the product's argument rendered as a data structure. A tool
can execute perfectly (`tool_execution: passed`) while the business outcome is
wrong (`business_outcome: failed`), and a model can be judged on tool selection
only by an evaluator that was actually run (`model_tool_selection:
not_evaluated`). Collapsing them into one status would produce exactly the
summary the product exists to refuse - so each layer has its own closed value set
and the model refuses a value the layer may not report.

`model_tool_selection` is `not_evaluated` in a source outcome report and stays
that way. §23.1: a Tier 2 import or Tier 3 benchmark "may display a normalized
call-level result beside it through a derived view, but neither updates this
field or the source report hash."

Scope note: §23.1's `tool_surface`, `annotations`, and `authorship` blocks are
absent here. Nothing in this milestone produces them - they belong to the
tool-surface, annotation, and proposal features - and modelling a block that only
ever serialises empty would put a shape in the hashed document that its producer
has not yet designed. They join with the milestone that fills them, under a
`schema_version` bump.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from actionwitness_core.contracts.enums import AssertionSeverity
from actionwitness_core.contracts.paths import ObservationPath, ObservationPathField
from actionwitness_core.engine.diff import MAX_CHANGE_EXCERPT_CHARS
from actionwitness_core.engine.enums import CheckStatus, FailureClassification
from actionwitness_core.engine.findings import Finding, aggregate, primary_failure
from actionwitness_core.evidence.models import RunEvent, ordered
from actionwitness_core.journeys.enums import (
    EventActor,
    GuidanceActor,
    OutcomeEventType,
    RunState,
)
from actionwitness_core.kernel import (
    ContractError,
    CoreErrorCode,
    CoreModel,
    JsonValue,
    UtcInstant,
    format_instant,
)
from actionwitness_core.reports.enums import (
    ALLOWED_LAYER_RESULTS,
    LayerResult,
    ReportLayer,
    RunMode,
)
from actionwitness_core.security.canonical import content_hash

__all__ = [
    "NO_ATTRIBUTED_CAUSE",
    "REPORT_SCHEMA_VERSION",
    "ContractReference",
    "CountsBlock",
    "ExternalCaptureReference",
    "ExternalTargetReference",
    "GuidanceReference",
    "LayeredResult",
    "OutcomeReport",
    "ScenarioReference",
    "TargetReference",
    "UndeclaredChange",
    "UndeclaredChangesBlock",
    "compose_outcome_report",
    "recorded_warnings",
]

#: Bumped when the report *shape* changes, not when a value does. §17.1 stores
#: this beside the document so a reader knows which shape a stored hash covers.
#:
#: 1.1 — `undeclared_changes.paths` carries §23.1's `{path, before, after,
#: attributed_cause}` objects instead of bare path strings, which FR-159
#: requires and which a reader cannot recover from a 1.0 document.
#:
#: 1.2 — an optional `external_target` block carries §23.9/FR-117's source
#: provenance. Existing non-external reports retain their prior fields; stored
#: 1.0 and 1.1 reports remain readable as the documents they are.
REPORT_SCHEMA_VERSION = "1.2"

type Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128)]
type ContentHash = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

#: The terminal states a verification report may carry (§16).
_VERDICT_STATES = frozenset(
    {RunState.PASSED, RunState.PASSED_WITH_WARNINGS, RunState.FAILED, RunState.ERROR}
)


class TargetReference(CoreModel):
    """Which target this run exercised (§23.1)."""

    id: Identifier
    adapter_id: Identifier

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"id": self.id, "adapter_id": self.adapter_id}


class ScenarioReference(CoreModel):
    """The immutable scenario configuration copied into the run (§23.1, §16).

    `fault_active` is derived by the adapter rather than chosen by the operator:
    §12.2 keeps the selected profile recorded in `post_fix` while disabling it,
    which is what makes a matched pre/post comparison meaningful.
    """

    mode: Identifier
    fault_profile: Identifier | None = None
    fault_active: bool = False

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "mode": self.mode,
            "fault_profile": self.fault_profile,
            "fault_active": self.fault_active,
        }


class ContractReference(CoreModel):
    """The contract this run judged, by identity rather than by copy (§23.1)."""

    id: Identifier
    schema_version: str
    content_hash: ContentHash

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "schema_version": self.schema_version,
            "content_hash": self.content_hash,
        }


class ExternalCaptureReference(CoreModel):
    """One independently observed external-state capture (§23.9, FR-117)."""

    path: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    captured_at: UtcInstant
    content_hash: ContentHash

    @model_validator(mode="after")
    def _require_safe_path(self) -> ExternalCaptureReference:
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise ContractError(
                "an external capture path must be absolute and contain no query or fragment",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "path": self.path,
            "captured_at": format_instant(self.captured_at),
            "content_hash": self.content_hash,
        }


class ExternalTargetReference(CoreModel):
    """Target-neutral source metadata for an externally observed run (§23.9)."""

    target_type: Identifier
    origin: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    pairing_id: Identifier
    bridge_version: Identifier
    theme_build_id: Identifier | None = None
    observation_provider: Identifier
    provenance: Identifier
    before: ExternalCaptureReference
    after: ExternalCaptureReference
    safe_scope_result: LayerResult

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "target_type": self.target_type,
            "origin": self.origin,
            "pairing_id": self.pairing_id,
            "bridge_version": self.bridge_version,
            "theme_build_id": self.theme_build_id,
            "observation_provider": self.observation_provider,
            "provenance": self.provenance,
            "captures": {
                "before": self.before.canonical_document(),
                "after": self.after.canonical_document(),
            },
            "safe_scope_result": self.safe_scope_result.value,
        }


class LayeredResult(CoreModel):
    """The five distinct layers (§23.1, FR-070, BUILD_ORDER invariant 10).

    Each field is validated against its own permitted value set, so a layer
    cannot report a result the specification does not allow it to - a
    `safety_policy: not_evaluated`, for instance, which would let an unevaluated
    policy hide behind a layer that never says so.
    """

    model_tool_selection: LayerResult = LayerResult.NOT_EVALUATED
    observed_trajectory: LayerResult = LayerResult.NOT_EVALUATED
    tool_execution: LayerResult = LayerResult.NOT_EVALUATED
    business_outcome: LayerResult = LayerResult.NOT_EVALUATED
    safety_policy: LayerResult = LayerResult.PASSED

    @model_validator(mode="after")
    def _check_each_layer_reports_a_permitted_value(self) -> LayeredResult:
        for layer in ReportLayer:
            value = getattr(self, layer.value)
            if value not in ALLOWED_LAYER_RESULTS[layer]:
                permitted = sorted(item.value for item in ALLOWED_LAYER_RESULTS[layer])
                raise ContractError(
                    f"layer {layer.value!r} may not report {value.value!r}; "
                    f"§23.1 permits {permitted}",
                    code=CoreErrorCode.EVALUATION_INPUT_INVALID,
                )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return {layer.value: getattr(self, layer.value).value for layer in ReportLayer}


class CountsBlock(CoreModel):
    """The §23.1 counts, each with a stated denominator.

    `tool_calls` counts actor-`agent` invocation starts only. §23.1 keeps
    actor-`eval` starts in the eval report as "replayed tool calls", so a replay
    can never inflate the number of calls an agent is credited with.

    `warnings` has two sources, because §9.5 defines two kinds of warning and
    only one of them is a failure. A warning-severity *failed* assertion is one.
    The other is a check that **held** while recording something a reader must
    still see - §9.5's `description_change`, which "should not fail a run" and
    which, counted nowhere, made a drifting tool surface report as an unqualified
    `passed`. Both are counted here so `passed_with_warnings` means what §8.5's
    label says it means, and neither turns a passing check into a failing one.
    """

    critical_failures: Annotated[int, Field(ge=0)] = 0
    warnings: Annotated[int, Field(ge=0)] = 0
    tool_calls: Annotated[int, Field(ge=0)] = 0
    human_confirmations: Annotated[int, Field(ge=0)] = 0
    guidance_handoffs: Annotated[int, Field(ge=0)] = 0

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "critical_failures": self.critical_failures,
            "warnings": self.warnings,
            "tool_calls": self.tool_calls,
            "human_confirmations": self.human_confirmations,
            "guidance_handoffs": self.guidance_handoffs,
        }


class GuidanceReference(CoreModel):
    """The guidance state at finalization (§23.1, §23.8)."""

    actor: GuidanceActor
    action: Identifier
    reason: str = ""

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"actor": self.actor.value, "action": self.action, "reason": self.reason}


#: The `attributed_cause` a path entry carries when nothing explains it. FR-159:
#: "`none` is an ordinary outcome, not an error, and is exactly what a change
#: from an unrelated background process should produce."
NO_ATTRIBUTED_CAUSE = "none"


class UndeclaredChange(CoreModel):
    """One undeclared path, with what it was, what it became, and who to ask.

    §23.1 shows these as objects rather than strings, and FR-159 says why: the
    finding must "list the paths **with redacted before and after values**, and
    attribute a likely cause". A bare path tells a reader that something moved
    and refuses to say what — which is an alert, not evidence, and leaves them
    reopening the run to learn anything.

    `before` and `after` are the diff's own bounded excerpts, carried through
    unchanged. They were redacted before either snapshot was persisted (§20.3
    redacts "before persistence, hashing, or export") and bounded when they were
    rendered, and the length cap is re-declared here so the report boundary
    enforces its own budget rather than trusting the producer. `None` is a side
    that does not exist — an added path has no `before` — and is distinct from
    the string `"null"`, which is a present JSON null.
    """

    path: ObservationPathField
    before: Annotated[str, Field(max_length=MAX_CHANGE_EXCERPT_CHARS)] | None = None
    after: Annotated[str, Field(max_length=MAX_CHANGE_EXCERPT_CHARS)] | None = None
    #: FR-159's adjacency attribution, as the short string §23.1 renders:
    #: `none`, `tool_action:<tool>@<sequence>`, or
    #: `human_confirmation@<sequence>`. The auditable object behind it is the
    #: finding's `attributed_cause` (§17.1).
    attributed_cause: str = NO_ATTRIBUTED_CAUSE

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "path": str(self.path),
            "before": self.before,
            "after": self.after,
            "attributed_cause": self.attributed_cause,
        }


class UndeclaredChangesBlock(CoreModel):
    """The §9.10 partition, as §23.1 presents it.

    `applied_exemptions` and `effect_metadata_published` are both present so a
    waiver is never invisible and a reader can tell "nothing was undeclared" from
    "the adapter published no effect metadata, so everything was".

    `paths` carries `UndeclaredChange` entries rather than path strings, which is
    what §23.1's own sample shows and what FR-159 requires. The counts beside it
    are still counts of *paths*, so `undeclared == len(paths)` and a reader who
    only wants the totals never has to walk the list.
    """

    changed_paths: Annotated[int, Field(ge=0)] = 0
    declared: Annotated[int, Field(ge=0)] = 0
    undeclared: Annotated[int, Field(ge=0)] = 0
    effect_metadata_published: bool = False
    paths: tuple[UndeclaredChange, ...] = ()
    applied_exemptions: tuple[ObservationPathField, ...] = ()

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "changed_paths": self.changed_paths,
            "declared": self.declared,
            "undeclared": self.undeclared,
            "effect_metadata_published": self.effect_metadata_published,
            "paths": [change.canonical_document() for change in self.paths],
            "applied_exemptions": [str(path) for path in self.applied_exemptions],
        }


class OutcomeReport(CoreModel):
    """One immutable layered outcome report (§23.1, FR-073).

    The report is a *projection* of findings and events, not a second source of
    truth: `compose_outcome_report` derives every layer and count from evidence
    already evaluated, so a report can never disagree with the findings it
    summarises.
    """

    schema_version: str = REPORT_SCHEMA_VERSION
    run_id: Identifier
    status: RunState
    mode: RunMode = RunMode.VERIFICATION
    target: TargetReference
    scenario: ScenarioReference
    contract: ContractReference
    external_target: ExternalTargetReference | None = None
    layers: LayeredResult
    counts: CountsBlock = CountsBlock()
    guidance_at_finalization: GuidanceReference | None = None
    undeclared_changes: UndeclaredChangesBlock | None = None
    primary_failure: FailureClassification | None = None

    @model_validator(mode="after")
    def _check_report_shape(self) -> OutcomeReport:
        if self.mode is RunMode.PROPOSAL:
            # §23.1: "a proposal run reports business_outcome: not_evaluated, a
            # null overall_result, and its candidate list rather than a verdict."
            if self.status is not RunState.PROPOSED:
                raise ContractError(
                    "a proposal report is terminal in `proposed` and carries no verdict",
                    code=CoreErrorCode.EVALUATION_INPUT_INVALID,
                )
            if self.layers.business_outcome is not LayerResult.NOT_EVALUATED:
                raise ContractError(
                    "a proposal run judges no contract, so business_outcome must be not_evaluated",
                    code=CoreErrorCode.EVALUATION_INPUT_INVALID,
                )
            if self.primary_failure is not None:
                raise ContractError(
                    "a proposal run carries no verdict and therefore no primary failure",
                    code=CoreErrorCode.EVALUATION_INPUT_INVALID,
                )
        elif self.status not in _VERDICT_STATES:
            raise ContractError(
                f"a verification report is finalized in a terminal verdict state, not "
                f"{self.status.value!r}",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        """The exact §23.1 document that is stored, exported, and hashed.

        Built explicitly rather than by `model_dump` so a block a milestone does
        not yet produce is absent rather than serialised as null, and so the
        hashed bytes are reviewable against §23.1 field by field.
        """
        document: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status.value,
            "target": self.target.canonical_document(),
            "scenario": self.scenario.canonical_document(),
            "contract": self.contract.canonical_document(),
            "layers": self.layers.canonical_document(),
            "mode": self.mode.value,
            "counts": self.counts.canonical_document(),
            "primary_failure": (
                self.primary_failure.value if self.primary_failure is not None else None
            ),
        }
        if self.external_target is not None:
            document["external_target"] = self.external_target.canonical_document()
        if self.undeclared_changes is not None:
            document["undeclared_changes"] = self.undeclared_changes.canonical_document()
        if self.guidance_at_finalization is not None:
            document["guidance_at_finalization"] = (
                self.guidance_at_finalization.canonical_document()
            )
        return document

    def content_hash(self) -> str:
        """The report's `sha256:...` identity (§17.2)."""
        return content_hash(self.canonical_document())

    def as_stored_document(self) -> dict[str, JsonValue]:
        """The document plus its own hash, as persisted and exported.

        §17.2 excludes the top-level `content_hash` member from the hash input,
        so a reader can recompute the hash from the stored document alone.
        """
        return {**self.canonical_document(), "content_hash": self.content_hash()}


def recorded_warnings(finding: Finding) -> tuple[str, ...]:
    """Non-failing warnings a check recorded about itself (§9.5).

    A check that held may still have seen something a reader has to be told
    about. §9.5's `description_change` is the specified case: it "should not fail
    a run", so the check passes and its classification is untouched, but a target
    that rewrote a tool's description between an agent's discovery of it and its
    invocation of it is exactly §9.11's undeclared delta, and a report that
    counted it nowhere was reporting a quiet run.

    Read out of `Finding.evidence` rather than from a dedicated field, because
    the evidence mapping is where a check already publishes what it saw and this
    is the same kind of fact. Read defensively for the same reason every other
    persisted mapping is: a finding rebuilt from a stored row is untrusted input
    (constitution §5), and a malformed value counts as no warning rather than
    raising in the middle of composing a report.

    Deliberately **not** part of `Finding.failed` or `aggregate`. §23.1's closed
    value set for the `safety_policy` layer has no `passed_with_warnings`, so a
    warning may move the run's summary and must never move a layer's verdict.
    """
    raw = finding.evidence.get("warnings")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return ()
    return tuple(str(item) for item in raw)


def _layer_from(finding: Finding) -> LayerResult:
    match finding.status:
        case CheckStatus.PASSED:
            return LayerResult.PASSED
        case CheckStatus.NOT_EVALUATED:
            return LayerResult.NOT_EVALUATED
        case _:
            return LayerResult.FAILED


def compose_outcome_report(
    *,
    run_id: str,
    target: TargetReference,
    scenario: ScenarioReference,
    contract: ContractReference,
    external_target: ExternalTargetReference | None = None,
    assertion_findings: Sequence[Finding] = (),
    policy_findings: Sequence[Finding] = (),
    trajectory_finding: Finding | None = None,
    tool_execution: LayerResult = LayerResult.NOT_EVALUATED,
    events: Sequence[RunEvent] = (),
    guidance_at_finalization: GuidanceReference | None = None,
    undeclared_changes: UndeclaredChangesBlock | None = None,
    mode: RunMode = RunMode.VERIFICATION,
) -> OutcomeReport:
    """Derive a report from evidence that has already been evaluated.

    Every layer, count, and the primary failure come from the findings and events
    passed in. Nothing is re-evaluated here and nothing is inferred: a report is a
    view of the verdict, not a second place where the verdict is decided.

    `model_tool_selection` is not a parameter. §23.1 finalizes it as
    `not_evaluated` in a source outcome report, and a Tier 2 import "does not
    update this field or the source report hash" - so there is no way to set it
    from here, which is the point.
    """
    everything = [*assertion_findings, *policy_findings]
    if trajectory_finding is not None:
        everything.append(trajectory_finding)

    business_outcome = (
        LayerResult.NOT_EVALUATED if mode is RunMode.PROPOSAL else aggregate(assertion_findings)
    )
    layers = LayeredResult(
        model_tool_selection=LayerResult.NOT_EVALUATED,
        observed_trajectory=(
            LayerResult.NOT_EVALUATED
            if trajectory_finding is None
            else _layer_from(trajectory_finding)
        ),
        tool_execution=tool_execution,
        business_outcome=business_outcome,
        safety_policy=aggregate(policy_findings),
    )

    timeline = ordered(events)
    counts = CountsBlock(
        critical_failures=sum(
            1
            for finding in everything
            if finding.failed and finding.severity is AssertionSeverity.CRITICAL
        ),
        warnings=sum(
            1
            for finding in everything
            if finding.failed and finding.severity is AssertionSeverity.WARNING
        )
        + sum(len(recorded_warnings(finding)) for finding in everything),
        tool_calls=sum(
            1 for event in timeline if event.is_invocation_start and event.actor is EventActor.AGENT
        ),
        human_confirmations=sum(
            1
            for event in timeline
            if event.event_type
            in {OutcomeEventType.CONFIRMATION_APPROVED, OutcomeEventType.CONFIRMATION_DENIED}
        ),
        guidance_handoffs=sum(
            1 for event in timeline if event.event_type is OutcomeEventType.GUIDANCE_TRANSITIONED
        ),
    )

    if mode is RunMode.PROPOSAL:
        status = RunState.PROPOSED
        failure = None
    else:
        failure_finding = primary_failure(everything)
        failure = failure_finding.classification if failure_finding is not None else None
        if counts.critical_failures:
            status = RunState.FAILED
        elif counts.warnings:
            status = RunState.PASSED_WITH_WARNINGS
        else:
            status = RunState.PASSED

    return OutcomeReport(
        run_id=run_id,
        status=status,
        mode=mode,
        target=target,
        scenario=scenario,
        contract=contract,
        external_target=external_target,
        layers=layers,
        counts=counts,
        guidance_at_finalization=guidance_at_finalization,
        undeclared_changes=undeclared_changes,
        primary_failure=failure,
    )


def undeclared_changes_from(finding: Finding, changed_paths: int) -> UndeclaredChangesBlock:
    """Project a `no_undeclared_changes` finding into its §23.1 block.

    A projection, never a second derivation: the excerpts and the attribution are
    read from the finding the policy engine produced, so the block and the
    finding can never tell a reader two different stories about one run.
    """
    evidence: Mapping[str, JsonValue] = finding.evidence
    published = bool(evidence.get("effect_metadata_published", False))
    return UndeclaredChangesBlock(
        changed_paths=changed_paths,
        declared=changed_paths - len(finding.paths),
        undeclared=len(finding.paths),
        effect_metadata_published=published,
        paths=_undeclared_entries(finding),
        applied_exemptions=finding.applied_exemptions,
    )


def _undeclared_entries(finding: Finding) -> tuple[UndeclaredChange, ...]:
    """FR-159's per-path entries, from the finding's evidence when it has them.

    `Finding.paths` stays the authority on *which* paths were undeclared - §17.1
    defines that column and §23.1's `undeclared` count is derived from it - so
    the richer evidence list is used only when it describes exactly those paths,
    in that order. Anything else is a finding whose two halves disagree, and the
    honest response is to publish the paths and say nothing about their values
    rather than to publish a pairing nobody produced.

    That fallback is also the compatibility path: a finding restored from a row
    written before FR-159 carried excerpts has no evidence list, and reports its
    paths with no values and `none` as the cause it never attributed.
    """
    entries = _parse_entries(finding.evidence.get("undeclared_changes"))
    if entries is not None and tuple(entry.path for entry in entries) == tuple(finding.paths):
        return entries
    return tuple(UndeclaredChange(path=path) for path in finding.paths)


def _parse_entries(raw: JsonValue) -> tuple[UndeclaredChange, ...] | None:
    """Validate an evidence list into models, or `None` if it is not one.

    Explicitly validated rather than trusted: a finding can arrive rebuilt from a
    stored row, and constitution §5 treats a persisted record as untrusted input.
    A single unusable entry discards the whole list, because a partial list would
    silently drop a path the finding says changed.

    `ValueError` covers every refusal that can reach here: `PathError` for a
    segment that is not a path, and Pydantic's own `ValidationError` for an
    excerpt longer than §11.4's budget - both subclass it, and the second is what
    stops a stored row from widening what a report carries.
    """
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return None
    parsed: list[UndeclaredChange] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return None
        try:
            parsed.append(
                UndeclaredChange(
                    path=ObservationPath.parse(str(item.get("path", ""))),
                    before=_excerpt_of(item.get("before")),
                    after=_excerpt_of(item.get("after")),
                    attributed_cause=str(item.get("attributed_cause", NO_ATTRIBUTED_CAUSE)),
                )
            )
        except ValueError:
            return None
    return tuple(parsed)


def _excerpt_of(value: JsonValue) -> str | None:
    """One recorded side of a change. `None` stays `None`; nothing else is coerced."""
    return None if value is None else str(value)
