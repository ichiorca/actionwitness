"""010-T1 — the configured live backend (FR-099, FR-096, AC-17, §25.3).

Two properties carry this stage, and they pull in opposite directions:

- **the live path is explicit.** FR-099 admits "one explicitly configured LLM
  backend". An allowlist is what makes "explicitly configured" checkable rather
  than a hope about the operator's typing, and the manifest records the provider
  as reproducibility metadata — so a value nothing validated would be a claim
  about a backend this build never saw.
- **its absence changes nothing else.** FR-096 requires the import and
  correlation module to keep working with no live backend, no credential, and
  no network. The Tier 2 gate runs in exactly that state, so a live path that
  made itself a prerequisite would take AC-16 down with it.

The label is the third thing worth guarding: §25.3 says the checked-in fallback
"must never be presented as a live execution", and `source_kind_for` reads the
deployment's own configuration rather than a caller's preference.
"""

from __future__ import annotations

import pytest
from actionwitness_core.benchmarks.enums import SourceKind
from actionwitness_service.config import ModuleStatus, ServiceSettings
from integrations.google_evals.live import (
    SUPPORTED_COMMAND_MODES,
    SUPPORTED_PROVIDERS,
    LiveRunUnavailable,
    describe_live_run,
    redacted_summary,
    source_kind_for,
)
from integrations.google_evals.pins import REPORTER_SCHEMA, REPORTER_VERSION

pytestmark = pytest.mark.adapters

CREDENTIAL = "EXAMPLE_MODEL_KEY"


def _configured(**overrides: str):
    """A resolved live backend, as `ServiceSettings` would produce one."""
    environ = {
        "HARNESS_ENV": "local",
        "LIVE_EVALUATOR_ENABLED": "true",
        "LIVE_EVALUATOR_PROVIDER": "google",
        "LIVE_EVALUATOR_MODEL": "example-model-1",
        "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
        CREDENTIAL: "not-a-real-key",
        **overrides,
    }
    return ServiceSettings.from_env(environ)


# --- absence is an ordinary state -------------------------------------------


def test_an_unconfigured_deployment_leaves_the_import_path_working() -> None:
    """FR-096: the Tier 2 module "remains available" without a live backend.

    This is the state CI runs in, so a live path that made itself a
    prerequisite would take AC-16 down with it.
    """
    # Arrange
    settings = ServiceSettings.from_env({"HARNESS_ENV": "local"})

    # Assert
    assert settings.module("live_evaluator").status is ModuleStatus.DISABLED
    assert settings.is_enabled("evaluator_import")
    assert settings.evaluator_import is not None


def test_describing_a_run_without_a_backend_is_refused_by_name() -> None:
    """`LiveRunUnavailable`, not a generic error: "no live backend here" is an
    ordinary deployment state and must be distinguishable from a bad report."""
    # Arrange / Act / Assert
    with pytest.raises(LiveRunUnavailable):
        describe_live_run(None)


def test_an_unconfigured_deployment_reports_a_recorded_fixture() -> None:
    """§25.3: the fallback is "never presented as a live execution"."""
    # Arrange / Act / Assert
    assert source_kind_for(None) is SourceKind.RECORDED_FIXTURE


# --- the configured backend --------------------------------------------------


def test_a_configured_backend_describes_a_pinned_run() -> None:
    """FR-099: a pinned `webmcp-evals` configuration against one backend."""
    # Arrange
    settings = _configured()

    # Act
    configuration = describe_live_run(settings.live_evaluator)

    # Assert
    assert configuration.provider == "google"
    assert configuration.model == "example-model-1"
    assert configuration.reporter_version == REPORTER_VERSION
    assert configuration.reporter_schema == REPORTER_SCHEMA


def test_a_configured_backend_reports_a_live_model_run() -> None:
    """AC-17: the application labels the suite `live_model_run`."""
    # Arrange
    settings = _configured()

    # Act / Assert
    assert source_kind_for(settings.live_evaluator) is SourceKind.LIVE_MODEL_RUN


def test_the_manifest_half_records_the_pin_and_the_backend() -> None:
    """FR-093's evaluator and model fields for a live suite."""
    # Arrange
    configuration = describe_live_run(_configured().live_evaluator)

    # Act
    fields = configuration.manifest_fields()

    # Assert
    assert fields["model_provider"] == "google"
    assert fields["model_name"] == "example-model-1"
    assert fields["evaluator_version"] == REPORTER_VERSION
    assert fields["reporter_schema"] == REPORTER_SCHEMA


def test_the_manifest_half_invents_no_model_parameters() -> None:
    """FR-093: missing metadata is `null`, never inferred.

    The parameters that matter are the ones the evaluator actually exported,
    and they arrive with the report. Filling them in from configuration would
    describe the run somebody intended rather than the one that happened.
    """
    # Arrange
    configuration = describe_live_run(_configured().live_evaluator)

    # Act
    fields = configuration.manifest_fields()

    # Assert
    assert "model_parameters" not in fields


# --- explicit means validated ------------------------------------------------


@pytest.mark.parametrize("provider", ["", "not-a-backend", "GOOGLE-ish", "local"])
def test_an_unsupported_provider_is_refused(provider: str) -> None:
    """FR-099's "explicitly configured", made checkable.

    The provider is written into the manifest as reproducibility metadata, so a
    value nothing validated would be a claim about a backend this build never
    saw.
    """
    # Arrange
    settings = _configured(LIVE_EVALUATOR_PROVIDER=provider)

    # Act / Assert — an empty provider is refused by config resolution, and an
    # unknown one by this module; either way no run is described.
    with pytest.raises(LiveRunUnavailable):
        describe_live_run(settings.live_evaluator)


def test_the_provider_allowlist_is_stated_rather_than_pattern_matched() -> None:
    """A second supported backend must be a decision, never a regex that
    happened to admit one."""
    # Arrange / Act / Assert
    assert frozenset({"google", "openai", "anthropic"}) == SUPPORTED_PROVIDERS


def test_smoke_mode_cannot_feed_a_benchmark() -> None:
    """ADR-0005 decision 6 and §25.3: `smoke` is a diagnostic, and is never
    presented as the probabilistic side of the benchmark."""
    # Arrange
    settings = _configured()

    # Act / Assert
    assert "smoke" not in SUPPORTED_COMMAND_MODES
    with pytest.raises(LiveRunUnavailable):
        describe_live_run(settings.live_evaluator, command_mode="smoke")


# --- the credential ----------------------------------------------------------


def test_the_configuration_carries_the_credential_name_not_its_value() -> None:
    """FR-099: the value stays in the environment.

    The variable *name* is useful — it tells an operator where to look. The
    value is not in this process to leak.
    """
    # Arrange
    configuration = describe_live_run(_configured().live_evaluator)

    # Act
    summary = redacted_summary(configuration)

    # Assert
    assert configuration.credential_var == CREDENTIAL
    assert "not-a-real-key" not in repr(configuration)
    assert "not-a-real-key" not in str(summary)
    assert summary["credential_source"] == f"environment variable {CREDENTIAL}"


def test_a_named_but_unset_credential_is_reported_before_the_demo() -> None:
    """004's resolver already refuses this, and it matters most here: a
    credential discovered missing during a recording is the worst moment."""
    # Arrange
    settings = ServiceSettings.from_env(
        {
            "HARNESS_ENV": "local",
            "LIVE_EVALUATOR_ENABLED": "true",
            "LIVE_EVALUATOR_PROVIDER": "google",
            "LIVE_EVALUATOR_MODEL": "example-model-1",
            "LIVE_EVALUATOR_CREDENTIAL_VAR": CREDENTIAL,
        }
    )

    # Act / Assert
    assert settings.module("live_evaluator").status is ModuleStatus.MISCONFIGURED
    assert source_kind_for(settings.live_evaluator) is SourceKind.RECORDED_FIXTURE


def test_a_misconfigured_live_backend_does_not_disable_the_import_path() -> None:
    """Tier 3 absence must never take the Tier 2 gate down with it."""
    # Arrange
    settings = ServiceSettings.from_env({"HARNESS_ENV": "local", "LIVE_EVALUATOR_ENABLED": "true"})

    # Act / Assert
    assert settings.module("live_evaluator").status is ModuleStatus.MISCONFIGURED
    assert settings.is_enabled("evaluator_import")


# --- no execution ------------------------------------------------------------


def test_this_module_runs_no_process() -> None:
    """FR-098 forbids arbitrary command execution, and AC-17 puts execution in
    the *developer's* hands: "when the developer executes the pinned Google
    evaluator ... and imports the resulting report".

    Asserted against the source, because a `subprocess` import added later
    would pass every behavioural test in this file.
    """
    # Arrange
    from pathlib import Path

    import integrations.google_evals.live as module

    # Act
    source = Path(module.__file__).read_text(encoding="utf-8")

    # Assert
    for forbidden in ("import subprocess", "os.system", "popen", "shell=True"):
        assert forbidden not in source, f"live.py reaches for {forbidden!r}"
