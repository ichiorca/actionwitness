"""Normalize a pinned `webmcp-evals` report into the core's trial model.

Spec v1.9 §24.7 step 3 ("normalize only allowlisted call-level fields"), FR-092
(what makes a trial a call-level pass), FR-093 (unsupported metadata is `null`),
ADR-0005 (trial addressing).

**Only allowlisted fields cross this boundary.** The core's `NormalizedTrial`
has no place to put a `response` string or an upstream diagnostic blob, and that
is the point: everything downstream — the matrix, the artifact, the UI — reads
the normalized shape, so a field that never crosses can never be presented as
evidence. What is not understood is recorded as `null` in `metadata` rather than
dropped, because a dropped key and an unsupported key look identical afterwards.

**A trial may have no usable address.** ADR-0005 found that `test.name` and
`runIndex` are *both* optional upstream, so a report can contain trials nothing
can name. Such a trial still gets a positional id — it has to be storable and
displayable — but it is marked `addressable: False`, and FR-091 then allows
binding it only by explicit operator choice. The id's shape is never evidence
that it identifies anything.

**Normalization makes no trial eligible.** Every trial leaves here excluded:
either the evaluator errored, or the outcome layer has not run yet. Eligibility
arrives when a binding or a replay supplies the second layer, which is the only
moment both halves exist.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from actionwitness_core.benchmarks.enums import (
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    TrialEligibility,
)
from actionwitness_core.benchmarks.models import NormalizedTrial
from actionwitness_core.kernel import CoreErrorCode, JsonValue
from integrations.google_evals.reader import ImportedReport, ReportRejected

__all__ = ["NormalizedReport", "normalize"]

#: ADR-0005's `TestResult` keys. Everything else in a trial is unsupported
#: metadata and is preserved as `null` (FR-093).
_SUPPORTED_TRIAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "test",
        "response",
        "outcome",
        "trajectory",
        "browserConsoleErrors",
        "runIndex",
        "stepIndex",
    }
)

#: `outcome` → the core's call-level vocabulary. The reader has already refused
#: anything outside this mapping, so a `KeyError` here would be a bug rather
#: than untrusted input.
_OUTCOMES: Final[Mapping[str, CallLevelResult]] = {
    "pass": CallLevelResult.PASSED,
    "fail": CallLevelResult.FAILED,
    "error": CallLevelResult.ERROR,
}

#: The `config` keys FR-093's manifest is built from. Anything else in `config`
#: is left where it is: the immutable source artifact keeps the whole redacted
#: document, so nothing is lost by not copying it into the manifest.
_MANIFEST_FIELDS: Final[Mapping[str, str]] = {
    "evaluatorName": "evaluator_name",
    "evaluatorPackage": "evaluator_package",
    "evaluatorVersion": "evaluator_version",
    "commandMode": "evaluator_command_mode",
    "modelProvider": "model_provider",
    "modelName": "model_name",
    "targetBuildCommit": "target_build_commit",
    "targetFixture": "target_fixture",
}


@dataclass(frozen=True, slots=True)
class NormalizedReport:
    """What one imported report becomes: trials plus the manifest's evaluator
    half.

    The manifest is returned as a mapping rather than a `BenchmarkManifest`
    because the other half — benchmark id, source kind, correlation mode,
    source artifact hashes — belongs to the suite, and this module has no
    business inventing any of it.
    """

    trials: tuple[NormalizedTrial, ...]
    manifest_fields: Mapping[str, JsonValue]

    @property
    def unaddressable_trial_ids(self) -> tuple[str, ...]:
        """Trials that need an explicit operator binding (ADR-0005, FR-091)."""
        return tuple(trial.external_trial_id for trial in self.trials if not trial.addressable)


def normalize(
    imported: ImportedReport,
    *,
    correlation_mode: CorrelationMode,
) -> NormalizedReport:
    """Turn a validated, redacted report into core trials.

    Takes an `ImportedReport` rather than raw bytes so the type system records
    that normalization happens *after* validation and redaction: there is no way
    to reach this function with a document that skipped either.
    """
    # Narrowed explicitly rather than asserted. `assert` disappears under `-O`,
    # which would turn a shape the reader is supposed to guarantee into an
    # attribute error in production and nothing at all in tests.
    envelope = imported.document["results"]
    config = imported.document["config"]
    if not isinstance(envelope, Mapping) or not isinstance(config, Mapping):
        raise _unreachable("results and config are objects by the time they reach here")
    raw_trials = envelope["results"]
    if not isinstance(raw_trials, Sequence) or isinstance(raw_trials, str | bytes):
        raise _unreachable("results.results is an array by the time it reaches here")

    addresses = _addresses(raw_trials)
    trials = tuple(
        _trial(raw, index, addresses[index], correlation_mode)
        for index, raw in enumerate(raw_trials)
        if isinstance(raw, Mapping)
    )

    return NormalizedReport(trials=trials, manifest_fields=_manifest(config, imported, trials))


def _unreachable(expectation: str) -> ReportRejected:
    """A shape `read_report` should already have refused.

    Reaching this means the reader and the normalizer disagree about what a
    validated report looks like, which is a defect here — not untrusted input.
    Raised as a rejection anyway so a caller never receives a half-normalized
    report, and named so the traceback says which invariant broke.
    """
    return ReportRejected(
        f"normalizer reached a report the reader should have refused: {expectation}",
        code=CoreErrorCode.EVALUATION_INPUT_INVALID,
    )


def _addresses(raw_trials: Sequence[JsonValue]) -> list[tuple[str, bool]]:
    """One `(id, addressable)` pair per trial, decided over the whole report.

    Uniqueness cannot be judged one trial at a time: ADR-0005 makes an address
    usable only when `(test.name, runIndex)` are both present **and the pair is
    unique within the report**, so two trials sharing an address make *both*
    unaddressable. Deciding per trial would let the first one look bindable.
    """
    proposed: list[str | None] = []
    for raw in raw_trials:
        if not isinstance(raw, Mapping):
            proposed.append(None)
            continue
        test = raw.get("test")
        name = test.get("name") if isinstance(test, Mapping) else None
        run_index = raw.get("runIndex")
        if isinstance(name, str) and name and isinstance(run_index, int):
            proposed.append(f"{name}#{run_index}")
        else:
            proposed.append(None)

    seen: dict[str, int] = {}
    for candidate in proposed:
        if candidate is not None:
            seen[candidate] = seen.get(candidate, 0) + 1

    addresses: list[tuple[str, bool]] = []
    for index, candidate in enumerate(proposed):
        if candidate is not None and seen[candidate] == 1:
            addresses.append((candidate, True))
        else:
            # Positional, and marked unaddressable. The position lets the trial
            # be stored and shown; `addressable=False` is what stops FR-091's
            # binding step from treating the position as an identity.
            addresses.append((f"#{index}", False))
    return addresses


def _trial(
    raw: Mapping[str, JsonValue],
    index: int,
    address: tuple[str, bool],
    correlation_mode: CorrelationMode,
) -> NormalizedTrial:
    """One trial, allowlisted fields only."""
    trial_id, addressable = address
    outcome = _OUTCOMES[str(raw["outcome"])]

    test = raw.get("test")
    name = test.get("name") if isinstance(test, Mapping) else None
    # The scenario is the shared intent, and repeated trials of one test are
    # repeats of one scenario (§24.7 step 1). A trial with no test name has no
    # scenario either, and gets its own positional label rather than being
    # gathered under an invented one.
    scenario_id = name if isinstance(name, str) and name else f"unnamed-scenario#{index}"

    trajectory = _trajectory(raw.get("trajectory"))

    # Normalization never produces an eligible trial: the outcome layer has not
    # run. An evaluator error is named as such, because that trial will never
    # become eligible however the outcome layer turns out.
    if outcome is CallLevelResult.ERROR:
        reason = ExclusionReason.EVALUATOR_ERROR
    elif correlation_mode is CorrelationMode.IMPORTED_TRAJECTORY_REPLAY and trajectory is None:
        reason = ExclusionReason.MISSING_TRAJECTORY
    else:
        reason = ExclusionReason.OUTCOME_NOT_REACHED

    return NormalizedTrial(
        external_trial_id=trial_id,
        scenario_id=scenario_id,
        correlation_mode=correlation_mode,
        call_level_result=outcome,
        eligibility=TrialEligibility.EXCLUDED,
        exclusion_reason=reason,
        trajectory=trajectory or (),
        metadata=_metadata(raw),
        addressable=addressable,
    )


def _trajectory(value: JsonValue) -> tuple[Mapping[str, JsonValue], ...] | None:
    """The replayable calls, or `None` when there is nothing replayable.

    Requires each step to be `{"name": str, "arguments": object}` and keeps only
    those two fields. FR-086 makes that a safety property rather than a
    convenience: a replayed trajectory is data this harness executes, so
    anything else in a step — a URL, a script, a shell string — must not survive
    into something the runner reads.

    A step that does not match is not guessed at. The whole trajectory becomes
    `None`, the trial is excluded as `missing_trajectory`, and its call-level
    result still counts toward coverage. Interpreting an unrecognised step shape
    would be inventing a replay nobody recorded.
    """
    if not isinstance(value, Sequence) or isinstance(value, str | bytes) or not value:
        return None

    steps: list[Mapping[str, JsonValue]] = []
    for step in value:
        if not isinstance(step, Mapping):
            return None
        name = step.get("name")
        arguments = step.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
            return None
        steps.append({"name": name, "arguments": dict(arguments)})
    return tuple(steps)


def _metadata(raw: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    """Unsupported upstream fields, explicitly `null` (FR-093).

    The *keys* are kept and the values are not. Keeping the value would carry
    unvalidated upstream content into a derived artifact; dropping the key would
    make "we did not understand this" indistinguishable from "this was never
    there".
    """
    return dict.fromkeys(sorted(set(raw) - _SUPPORTED_TRIAL_KEYS))


def _manifest(
    config: Mapping[str, JsonValue],
    imported: ImportedReport,
    trials: Sequence[NormalizedTrial],
) -> dict[str, JsonValue]:
    """FR-093's evaluator half of the manifest.

    Absent fields are `null`, never inferred — a manifest that filled in a model
    name from today's configuration would describe the wrong run the moment the
    configuration changed.
    """
    fields: dict[str, JsonValue] = {
        target: config.get(source) if isinstance(config.get(source), str) else None
        for source, target in _MANIFEST_FIELDS.items()
    }
    # Exactly what the evaluator exported, or `None` when it exported nothing.
    # AC-17 asks for "actual exported evaluator/model parameters without
    # inventing missing values", and an empty dict here would invent the claim
    # that the evaluator reported none.
    parameters = config.get("modelParameters")
    fields["model_parameters"] = dict(parameters) if isinstance(parameters, Mapping) else None
    fields["reporter_schema"] = imported.reporter_schema
    fields["normalized_adapter_version"] = imported.normalizer_version
    # The number of repeated trials this report carries, counted rather than
    # read from a `runCount` the report could state incorrectly.
    fields["run_count"] = len(trials)
    fields["scenario_ids"] = sorted({trial.scenario_id for trial in trials})
    return fields
