"""The pinned configuration a live evaluator run is executed against (FR-099).

Spec v1.9 §12.11 (FR-099–FR-101), §25.3, AC-17; ADR-0005 (the pin).

**This module describes a run; it does not perform one.** AC-17 says "the
*developer* executes the pinned Google evaluator ... and imports the resulting
report" — the harness's part is to state the configuration reproducibly, accept
the report, and label the suite. FR-098 forbids arbitrary command execution
outright and FR-097 leaves any server-side CLI adapter as a stretch behind an
allowlisted argument vector, so there is deliberately no `subprocess` here and
no code path that could grow one by accident.

**The credential never appears.** `LiveEvaluatorSettings` carries the *name* of
the environment variable, never its value, and this module carries the name no
further than a description a developer reads. FR-099 names four places a
credential must never reach — the browser, a WebMCP argument, a committed file,
an uploaded manifest — and the way to satisfy all four is for the value never to
enter the harness process at all.

**A live run and a recorded fixture are never interchangeable.** §25.3 and
FR-101 both insist the checked-in fallback stays labeled `recorded_fixture`.
`source_kind_for` is the one place that decision is made, and it reads the
resolved configuration rather than a caller's preference — a caller who wanted
to claim a live run could otherwise simply ask for one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from actionwitness_core.benchmarks.enums import SourceKind
from integrations.google_evals.pins import (
    NORMALIZER_VERSION,
    REPORTER_PACKAGE,
    REPORTER_SCHEMA,
    REPORTER_VERSION,
)

__all__ = [
    "SUPPORTED_COMMAND_MODES",
    "SUPPORTED_PROVIDERS",
    "LiveRunConfiguration",
    "describe_live_run",
    "source_kind_for",
]

#: Backends this build knows how to describe a run against. FR-099 admits "one
#: explicitly configured LLM backend"; an allowlist is what makes "explicitly
#: configured" checkable rather than a hope about the operator's typing.
#:
#: An unknown provider is refused rather than passed through: the manifest
#: records the provider as reproducibility metadata (FR-093), and a value
#: nothing validated would be a claim about a backend this build never saw.
SUPPORTED_PROVIDERS: Final[frozenset[str]] = frozenset({"google", "openai", "anthropic"})

#: §25.3's modes. `smoke` is a diagnostic and is deliberately absent: ADR-0005
#: decision 6 keeps it out of the probabilistic side of the benchmark, so a
#: smoke run must never be described as one that could feed a matrix.
SUPPORTED_COMMAND_MODES: Final[frozenset[str]] = frozenset({"browser", "local"})


@dataclass(frozen=True, slots=True)
class LiveRunConfiguration:
    """Everything a developer needs to reproduce one live evaluator run.

    Every field is either pinned by ADR-0005 or explicitly configured. There is
    no field a caller could use to smuggle a credential, and no field this
    module fills in from a default the operator did not choose — FR-093 makes
    missing metadata `null`, never inferred.
    """

    provider: str
    model: str
    #: The *name* of the environment variable holding the credential. Never the
    #: value: this string is written into a manifest a human reads.
    credential_var: str
    command_mode: str
    reporter_package: str = REPORTER_PACKAGE
    reporter_version: str = REPORTER_VERSION
    reporter_schema: str = REPORTER_SCHEMA
    normalizer_version: str = NORMALIZER_VERSION

    def manifest_fields(self) -> dict[str, object]:
        """FR-093's evaluator and model half, for a live suite.

        `model_parameters` is deliberately empty here: FR-100 freezes the
        parameters into the manifest at approval time, and whatever the
        evaluator actually exported arrives with the report. Guessing them from
        configuration would describe the run somebody intended rather than the
        one that happened.
        """
        return {
            "evaluator_name": self.reporter_package,
            "evaluator_package": self.reporter_package,
            "evaluator_version": self.reporter_version,
            "evaluator_command_mode": self.command_mode,
            "model_provider": self.provider,
            "model_name": self.model,
            "reporter_schema": self.reporter_schema,
            "normalized_adapter_version": self.normalizer_version,
        }


class LiveRunUnavailable(RuntimeError):
    """The live path is not configured, or is configured in a way this build
    cannot describe.

    Its own type so a caller can tell "no live backend here" from "the report
    you imported was bad" — the first is an ordinary deployment state that
    FR-096 requires the Tier 2 path to survive, and the second is a finding.
    """


def describe_live_run(
    settings: object,
    *,
    command_mode: str = "browser",
) -> LiveRunConfiguration:
    """The pinned configuration for the resolved live backend.

    Raises `LiveRunUnavailable` when the live evaluator module is disabled or
    misconfigured. That is not an error state for the product: FR-096 requires
    the import and correlation module to keep working with no live backend at
    all, and the caller's job is to fall back to the recorded fixture rather
    than to fail.
    """
    if settings is None:
        raise LiveRunUnavailable(
            "no live evaluator backend is configured; the recorded fixture path "
            "remains available (FR-096)"
        )

    provider = str(getattr(settings, "provider", "")).strip().lower()
    model = str(getattr(settings, "model", "")).strip()
    credential_var = str(getattr(settings, "credential_var", "")).strip()

    if provider not in SUPPORTED_PROVIDERS:
        raise LiveRunUnavailable(
            f"provider {provider!r} is not one this build can describe a run "
            f"against; supported: {sorted(SUPPORTED_PROVIDERS)}"
        )
    if not model:
        raise LiveRunUnavailable("no model is configured for the live backend")
    if not credential_var:
        raise LiveRunUnavailable("no credential variable name is configured")
    if command_mode not in SUPPORTED_COMMAND_MODES:
        raise LiveRunUnavailable(
            f"command mode {command_mode!r} cannot feed a benchmark; supported: "
            f"{sorted(SUPPORTED_COMMAND_MODES)}"
        )

    return LiveRunConfiguration(
        provider=provider,
        model=model,
        credential_var=credential_var,
        command_mode=command_mode,
    )


def source_kind_for(settings: object) -> SourceKind:
    """`live_model_run` only when a live backend is actually configured.

    Read from the resolved configuration rather than taken from a caller.
    AC-17 requires the application to label a live suite `live_model_run`, and
    §25.3 requires the checked-in fallback to be labeled `recorded_fixture` and
    "never presented as a live execution" — so the label has to follow the
    deployment's own state, not a request field somebody could set.
    """
    try:
        describe_live_run(settings)
    except LiveRunUnavailable:
        return SourceKind.RECORDED_FIXTURE
    return SourceKind.LIVE_MODEL_RUN


class CredentialMaterialRejected(ValueError):
    """An uploaded document carried something that looks like a credential.

    Distinct from `ReportRejected` because the remedy is different: a malformed
    report needs regenerating, and this one needs a human to find out how a
    secret reached a file somebody was about to upload — and to rotate it.
    """


def screen_for_credential_material(document: Mapping[str, object], settings: object) -> None:
    """Refuse an uploaded document that carries credential material (FR-099).

    FR-099 forbids a credential arriving "through ... an uploaded benchmark
    manifest", and this is the one prohibited channel the harness can actually
    police: it knows the *name* of the variable the credential lives in, so a
    document using that name as a key is a credential being carried in.

    Deliberately narrow. It screens for the configured variable name and for a
    small set of unambiguous credential key spellings, and it does **not** try
    to recognise secrets by shape — a heuristic that guessed would reject
    ordinary data and, worse, would teach a reader that anything it passed was
    safe. The general defence is redaction before persistence (FR-090), which
    runs regardless; this is the specific one that can name what went wrong.

    Raises rather than redacting, because a credential in a file destined for a
    repository is an incident: silently removing it would hide the fact that
    the value existed and needs rotating (constitution §7).
    """
    names = {"apikey", "api_key", "secret", "password", "credential", "access_token"}
    configured = str(getattr(settings, "credential_var", "") or "").strip().lower()
    if configured:
        names.add(configured)

    found = _keys_matching(document, names)
    if found:
        raise CredentialMaterialRejected(
            "the uploaded document carries credential material under "
            f"{sorted(found)}; a credential must reach this process only through "
            "the evaluator's environment (FR-099). Treat the value as exposed "
            "and rotate it."
        )


def _keys_matching(value: object, names: set[str]) -> set[str]:
    """Every key in a nested document whose name is credential-like."""
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).strip().lower() in names:
                found.add(str(key))
            found |= _keys_matching(nested, names)
    elif isinstance(value, list | tuple):
        for item in value:
            found |= _keys_matching(item, names)
    return found


def redacted_summary(configuration: LiveRunConfiguration) -> Mapping[str, str]:
    """What may be shown about a live run, for a UI or a log line.

    The credential *variable name* is included because it tells an operator
    where to look; its value is not present in this process to include. Nothing
    here is derived from the environment at call time, so a summary cannot
    accidentally start carrying a secret that was added later.
    """
    return {
        "provider": configuration.provider,
        "model": configuration.model,
        "credential_source": f"environment variable {configuration.credential_var}",
        "evaluator": f"{configuration.reporter_package}@{configuration.reporter_version}",
        "command_mode": configuration.command_mode,
    }
