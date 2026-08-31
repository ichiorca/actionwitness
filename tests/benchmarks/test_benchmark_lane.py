"""Benchmark lane (spec §26.5).

AC-16's defining constraint is that the whole import/correlation path runs with
Node unavailable and no LLM or Shopify credential. That is a configuration
property, and it is assertable now: report import must be available in an empty
environment, and the live-model path must not be.
"""

import pytest
from actionwitness_service.config import ModuleStatus


@pytest.mark.benchmarks
def test_report_import_is_available_without_any_credential(build_settings) -> None:
    """AC-16 runs from a checked-in fixture; needing a key would defeat it."""
    settings = build_settings({})
    assert settings.is_enabled("evaluator_import")
    assert settings.evaluator_import is not None


@pytest.mark.benchmarks
def test_the_live_model_path_is_off_unless_explicitly_configured(build_settings) -> None:
    """A recorded fixture and a live run must never be silently interchangeable."""
    settings = build_settings({})
    assert settings.module("live_evaluator").status is ModuleStatus.DISABLED
    assert settings.live_evaluator is None


@pytest.mark.benchmarks
def test_import_limits_are_bounded_before_parsing(build_settings) -> None:
    """FR-090 caps size and trial count *before* parsing untrusted report JSON."""
    settings = build_settings({})
    assert settings.evaluator_import is not None
    assert settings.evaluator_import.max_report_bytes == 1_048_576
    assert settings.evaluator_import.max_trials == 100


@pytest.mark.benchmarks
def test_an_unavailable_live_model_does_not_disable_report_import(build_settings) -> None:
    """Tier 3 absence must never take the Tier 2 gate down with it."""
    settings = build_settings({"LIVE_EVALUATOR_ENABLED": "true"})  # enabled but unconfigured
    assert settings.module("live_evaluator").status is ModuleStatus.MISCONFIGURED
    assert settings.is_enabled("evaluator_import")
    assert settings.is_enabled("buggy_store")
