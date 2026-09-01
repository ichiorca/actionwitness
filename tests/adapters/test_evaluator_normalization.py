"""008-T4 — normalizing an imported report (§24.7 step 3, FR-092, FR-093).

Three properties carry this stage:

- **only allowlisted fields cross.** The `response` text and any upstream blob
  stay in the immutable source artifact; what reaches the core is the call-level
  verdict, the scenario, and a replayable trajectory. A field that never crosses
  can never be presented as evidence.
- **unsupported metadata is `null`, not absent.** FR-093 is explicit, and the
  reason is legibility: a dropped key and an unsupported key are
  indistinguishable afterwards.
- **an unaddressable trial is marked, not guessed.** ADR-0005 found `test.name`
  and `runIndex` are both optional upstream, so a report can carry trials
  nothing can name. FR-091 then permits only an explicit operator binding.
"""

from __future__ import annotations

import json

import pytest
from actionwitness_core.benchmarks.enums import (
    CallLevelResult,
    CorrelationMode,
    ExclusionReason,
    TrialEligibility,
)
from integrations.google_evals.normalize import normalize
from integrations.google_evals.pins import (
    NORMALIZER_VERSION,
    REPORTER_SCHEMA,
    REPORTER_VERSION,
)
from integrations.google_evals.reader import read_report

pytestmark = pytest.mark.adapters

REPLAY = CorrelationMode.IMPORTED_TRAJECTORY_REPLAY
BROWSER = CorrelationMode.EXECUTED_BROWSER


def _trial(
    name: str | None = "adds a mug",
    outcome: str = "pass",
    run_index: int | None = 0,
    **extra: object,
) -> dict:
    trial: dict = {"response": "the assistant's reply", "outcome": outcome, **extra}
    if name is not None:
        trial["test"] = {"name": name}
    if run_index is not None:
        trial["runIndex"] = run_index
    return trial


def _read(*trials: dict, config: dict | None = None):
    document = {
        "config": {
            "reporterSchema": REPORTER_SCHEMA,
            "evaluatorVersion": REPORTER_VERSION,
            **(config or {}),
        },
        "results": {
            "results": list(trials),
            "testCount": len(trials),
            "passCount": 0,
            "failCount": 0,
            "errorCount": 0,
        },
    }
    return read_report(json.dumps(document).encode("utf-8"))


# --- the call-level verdict --------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("pass", CallLevelResult.PASSED),
        ("fail", CallLevelResult.FAILED),
        ("error", CallLevelResult.ERROR),
    ],
)
def test_each_outcome_normalizes_to_its_call_level_result(
    outcome: str, expected: CallLevelResult
) -> None:
    """FR-092's three-valued call-level layer."""
    # Arrange
    imported = _read(_trial(outcome=outcome))

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.trials[0].call_level_result is expected


def test_an_evaluator_error_is_excluded_by_name() -> None:
    """It will never become eligible however the outcome layer turns out, so
    the reason is recorded now rather than deferred."""
    # Arrange
    imported = _read(_trial(outcome="error"))

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.trials[0].exclusion_reason is ExclusionReason.EVALUATOR_ERROR


def test_normalization_makes_no_trial_eligible() -> None:
    """Eligibility needs both layers, and the outcome layer has not run.

    A normalizer that marked a trial eligible here would let a matrix be
    computed over outcomes nobody observed.
    """
    # Arrange
    imported = _read(_trial(outcome="pass"), _trial(name="applies discount", outcome="fail"))

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert {trial.eligibility for trial in report.trials} == {TrialEligibility.EXCLUDED}
    assert {trial.exclusion_reason for trial in report.trials} == {
        ExclusionReason.OUTCOME_NOT_REACHED
    }


# --- scenarios and addressing ------------------------------------------------


def test_repeated_trials_of_one_test_are_one_scenario() -> None:
    """§24.7 step 1: the scenario is the shared intent; `runIndex` distinguishes
    the repeats."""
    # Arrange
    imported = _read(
        _trial(name="adds a mug", run_index=0),
        _trial(name="adds a mug", run_index=1),
        _trial(name="applies discount", run_index=0),
    )

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert [trial.scenario_id for trial in report.trials] == [
        "adds a mug",
        "adds a mug",
        "applies discount",
    ]
    assert [trial.external_trial_id for trial in report.trials] == [
        "adds a mug#0",
        "adds a mug#1",
        "applies discount#0",
    ]


@pytest.mark.parametrize(
    "kwargs",
    [{"name": None}, {"run_index": None}, {"name": None, "run_index": None}],
)
def test_a_trial_missing_half_its_address_is_unaddressable(kwargs: dict) -> None:
    """ADR-0005: an address is usable only when *both* parts are present."""
    # Arrange
    imported = _read(_trial(**kwargs))

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.trials[0].addressable is False
    assert report.unaddressable_trial_ids == ("#0",)


def test_a_duplicated_address_makes_both_trials_unaddressable() -> None:
    """Uniqueness is judged over the whole report, not per trial.

    Deciding one at a time would let the first of a duplicated pair look
    bindable — and binding the wrong trial's outcome to this trial's call
    evidence is precisely the error this product exists to catch.
    """
    # Arrange
    imported = _read(
        _trial(name="adds a mug", run_index=0),
        _trial(name="adds a mug", run_index=0),
        _trial(name="applies discount", run_index=0),
    )

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert [trial.addressable for trial in report.trials] == [False, False, True]
    assert report.unaddressable_trial_ids == ("#0", "#1")


def test_an_unnamed_trial_gets_its_own_scenario_rather_than_a_shared_bucket() -> None:
    """FR-093: missing metadata is never inferred. Gathering unnamed trials
    under one label would invent a scenario nobody defined."""
    # Arrange
    imported = _read(_trial(name=None), _trial(name=None))

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert [trial.scenario_id for trial in report.trials] == [
        "unnamed-scenario#0",
        "unnamed-scenario#1",
    ]


# --- only allowlisted fields cross ------------------------------------------


def test_the_response_text_does_not_cross_the_boundary() -> None:
    """§24.7 step 3 normalizes "only allowlisted call-level fields".

    The full text stays in the immutable source artifact, which is where an
    auditor reads it; nothing downstream can present it as normalized evidence.
    """
    # Arrange
    imported = _read(_trial())

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert "the assistant's reply" not in json.dumps(report.trials[0].canonical_document())


def test_an_unsupported_field_is_preserved_as_null() -> None:
    """FR-093: "missing unsupported metadata shall be `null`, never inferred".

    The key survives so a reader can see something was not understood; the value
    does not, so unvalidated upstream content never reaches a derived artifact.
    """
    # Arrange
    imported = _read(_trial(someNewUpstreamField={"depth": 2}, anotherOne=[1, 2, 3]))

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.trials[0].metadata == {"anotherOne": None, "someNewUpstreamField": None}


def test_a_trial_with_nothing_unsupported_carries_empty_metadata() -> None:
    """The counterpart: `null` means "not understood", so it must not appear
    when everything was."""
    # Arrange
    imported = _read(_trial(stepIndex=2, browserConsoleErrors=[]))

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.trials[0].metadata == {}


# --- trajectories ------------------------------------------------------------


def test_a_well_formed_trajectory_keeps_only_name_and_arguments() -> None:
    """FR-086: a replayed trajectory is data this harness executes, so anything
    else in a step must not survive into what the runner reads."""
    # Arrange
    imported = _read(
        _trial(
            trajectory=[
                {
                    "name": "update_cart",
                    "arguments": {"product_id": "mug", "quantity": 1},
                    "callbackUrl": "https://elsewhere.example/hook",
                }
            ]
        )
    )

    # Act
    report = normalize(imported, correlation_mode=REPLAY)

    # Assert
    assert report.trials[0].trajectory == (
        {"name": "update_cart", "arguments": {"product_id": "mug", "quantity": 1}},
    )
    assert "callbackUrl" not in json.dumps(report.trials[0].canonical_document())


@pytest.mark.parametrize(
    "trajectory",
    [
        [{"tool": "update_cart", "arguments": {}}],
        [{"name": "update_cart"}],
        [{"name": "", "arguments": {}}],
        ["update_cart(mug)"],
        [{"name": "update_cart", "arguments": "product_id=mug"}],
    ],
)
def test_an_unrecognised_step_shape_is_not_guessed_at(trajectory: list) -> None:
    """The whole trajectory becomes unusable rather than partially interpreted.

    Interpreting an unrecognised step would be inventing a replay nobody
    recorded — and the invented calls would then run against a real target.
    """
    # Arrange
    imported = _read(_trial(trajectory=trajectory))

    # Act
    report = normalize(imported, correlation_mode=REPLAY)

    # Assert
    assert report.trials[0].trajectory == ()
    assert report.trials[0].exclusion_reason is ExclusionReason.MISSING_TRAJECTORY


def test_a_replay_trial_without_a_trajectory_is_excluded_for_that_reason() -> None:
    """There is nothing to execute through the adapter, and the coverage count
    should say so rather than blaming the outcome layer."""
    # Arrange
    imported = _read(_trial())

    # Act
    report = normalize(imported, correlation_mode=REPLAY)

    # Assert
    assert report.trials[0].exclusion_reason is ExclusionReason.MISSING_TRAJECTORY


def test_a_browser_trial_without_a_trajectory_is_not_penalised() -> None:
    """An `executed_browser` trial binds to a run that already happened, so it
    never needed a replayable trajectory."""
    # Arrange
    imported = _read(_trial())

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.trials[0].exclusion_reason is ExclusionReason.OUTCOME_NOT_REACHED


# --- the manifest half -------------------------------------------------------


def test_the_manifest_records_the_evaluator_and_model_metadata() -> None:
    """FR-093's reproducibility fields, read from the report rather than from
    today's configuration."""
    # Arrange
    imported = _read(
        _trial(),
        config={
            "evaluatorName": "webmcp-evals",
            "modelProvider": "example",
            "modelName": "example-model-1",
            "modelParameters": {"temperature": 0},
            "targetBuildCommit": "abc1234",
        },
    )

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.manifest_fields["evaluator_name"] == "webmcp-evals"
    assert report.manifest_fields["model_name"] == "example-model-1"
    assert report.manifest_fields["model_parameters"] == {"temperature": 0}
    assert report.manifest_fields["target_build_commit"] == "abc1234"
    assert report.manifest_fields["reporter_schema"] == REPORTER_SCHEMA
    assert report.manifest_fields["normalized_adapter_version"] == NORMALIZER_VERSION


def test_absent_manifest_metadata_is_null_rather_than_invented() -> None:
    """FR-093: "missing unsupported metadata shall be `null`, never inferred".

    A manifest that filled in a model name from the environment would describe
    the wrong run the moment the environment changed.
    """
    # Arrange
    imported = _read(_trial())

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.manifest_fields["model_provider"] is None
    assert report.manifest_fields["model_name"] is None
    assert report.manifest_fields["target_fixture"] is None


def test_the_run_count_is_counted_rather_than_believed() -> None:
    """The report states its own counts; this one is derived from the trials
    actually normalized, so a report whose header disagreed with its body
    cannot misreport coverage."""
    # Arrange
    imported = _read(_trial(run_index=0), _trial(run_index=1), _trial(run_index=2))

    # Act
    report = normalize(imported, correlation_mode=BROWSER)

    # Assert
    assert report.manifest_fields["run_count"] == 3
    assert report.manifest_fields["scenario_ids"] == ["adds a mug"]
