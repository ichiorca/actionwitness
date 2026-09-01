"""Read an untrusted `webmcp-evals` report: limits, schema, redaction, hash.

Spec v1.9 FR-090 (versioned report import), §25.3, §26.5; ADR-0005 (the pin).

**The order of operations is the control, not a style choice.** FR-090 caps the
artifact at 1 MiB and the suite at 100 trials, and BUILD_ORDER §7/M7 says
"before parsing"; it then requires redaction "before persistence and hashing".
Each of those is a sequence requirement with a specific failure behind it:

1. **Size before parsing.** A 900 MB file that is rejected after `json.loads`
   has already been decoded into memory. The cap has to be a check on bytes.
2. **Trial count before validating trials.** Once the report is parsed, the
   trial cap is applied to the array length before any element is inspected, so
   a 10,000-trial report costs one length check rather than 10,000 validations.
3. **Redaction before hashing.** A hash computed over the raw document commits
   the unredacted bytes to the evidence chain permanently — the hash *is* the
   thing that gets stored and compared, so redacting afterwards is too late.

**An unpinned schema is refused, not guessed at.** ADR-0005 and `pins.py` allow
exactly one reporter schema. Reading an unknown shape means guessing which field
means what, and the guesses end up in an artifact that claims to be evidence.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from actionwitness_core.kernel import (
    CoreError,
    CoreErrorCode,
    ErrorDetail,
    JsonValue,
)
from actionwitness_core.security.canonical import content_hash
from actionwitness_core.security.redaction import (
    RedactionPattern,
    RedactionPolicy,
    redact,
)
from integrations.google_evals.pins import (
    NORMALIZER_VERSION,
    REPORTER_PACKAGE,
    REPORTER_SCHEMA,
    REPORTER_VERSION,
    is_supported_schema,
)

__all__ = [
    "DEFAULT_LIMITS",
    "REPORT_REDACTION_PATHS",
    "ImportLimits",
    "ImportedReport",
    "ReportRejected",
    "read_report",
]

#: FR-090's caps. Defaults matching the requirement, overridable by the service
#: so configuration stays the service's job rather than this module's.
MAX_REPORT_BYTES: Final = 1_048_576
MAX_TRIALS: Final = 100

#: Secret-bearing fields this reporter's `config` block can carry, redacted in
#: addition to the core's defaults (§20.3: contract paths "are applied in
#: addition to defaults").
#:
#: They need naming here because the core's word matching is deliberately
#: conservative: it matches `api_key` but not `apiKey`, and widening it to match
#: a bare `key` would redact `idempotency_key` and `cart_key` — values 004 and
#: 005 store as evidence on purpose. The adapter knows its own format, so the
#: camelCase names belong with the format rather than in shared security code.
#:
#: Pinned to ADR-0005's report shape. Moving the pin means re-reading upstream's
#: config type for fields added since.
REPORT_REDACTION_PATHS: Final[tuple[str, ...]] = (
    "config.apiKey",
    "config.apiToken",
    "config.authToken",
    "config.accessKey",
    "config.credential",
    "config.credentials",
    "**.apiKey",
    "**.apiToken",
)

#: The three outcome values the pinned reporter emits.
_OUTCOMES: Final[frozenset[str]] = frozenset({"pass", "fail", "error"})

#: The `TestResults` envelope's own keys.
_RESULTS_KEYS: Final[frozenset[str]] = frozenset(
    {"results", "testCount", "passCount", "errorCount", "failCount"}
)


class ReportRejected(CoreError):
    """An imported report could not be trusted enough to read.

    Its own type because every rejection here is about the *file*, never about
    the target under test: a caller must be able to tell "this report is
    unusable" from "this benchmark found a problem", and a shared exception type
    would leave that to string matching.
    """


@dataclass(frozen=True, slots=True)
class ImportLimits:
    """FR-090's two caps, passed in rather than read from the environment.

    This package must stay importable with no service configuration present —
    §26.5 requires the whole path to run "without Node, an LLM API key, Shopify,
    or the Buggy Store package", and reading settings here would add exactly the
    dependency that requirement forbids.
    """

    max_bytes: int = MAX_REPORT_BYTES
    max_trials: int = MAX_TRIALS


DEFAULT_LIMITS: Final = ImportLimits()


@dataclass(frozen=True, slots=True)
class ImportedReport:
    """A report that passed every check, redacted and hashed in that order."""

    #: The redacted document. The only form that is ever persisted or hashed.
    document: Mapping[str, JsonValue]
    #: `sha256:…` over the redacted document, so the stored bytes are the hashed
    #: bytes (FR-090's "separately hashed immutable source artifact").
    content_hash: str
    reporter_schema: str
    normalizer_version: str
    trial_count: int
    #: Whether anything was actually removed. Recorded so a fixture claiming to
    #: be redacted can be checked rather than believed.
    redacted: bool


def _reject(message: str, *, location: str, code: CoreErrorCode) -> ReportRejected:
    return ReportRejected(
        message, code=code, details=(ErrorDetail(location=location, message=message),)
    )


def read_report(
    raw: bytes,
    *,
    limits: ImportLimits = DEFAULT_LIMITS,
    policy: RedactionPolicy | None = None,
) -> ImportedReport:
    """Validate, redact, and hash one evaluator report.

    Takes bytes rather than a parsed object on purpose: the size cap is a
    statement about the artifact that arrived, and a caller who had already
    parsed it would have spent the cost the cap exists to prevent.
    """
    _check_size(raw, limits)
    document = _parse(raw)
    _check_schema(document)
    envelope = _results_envelope(document)
    trials = _check_trial_count(envelope, limits)
    _check_trials(trials)

    # Redaction precedes hashing. The redacted document is the artifact.
    #
    # The caller's policy *widens* this adapter's own; it never replaces it. A
    # caller who passed a narrow policy must not be able to switch the
    # reporter's known secret fields back on (§20.3).
    redacted_document = redact(document, _policy_for(policy))
    if not isinstance(redacted_document, Mapping):  # pragma: no cover - redact preserves shape
        raise _reject(
            "redaction did not return an object",
            location="$",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )

    return ImportedReport(
        document=redacted_document,
        content_hash=content_hash(dict(redacted_document)),
        reporter_schema=REPORTER_SCHEMA,
        normalizer_version=NORMALIZER_VERSION,
        trial_count=len(trials),
        redacted=redacted_document != document,
    )


def _policy_for(caller: RedactionPolicy | None) -> RedactionPolicy:
    """This adapter's known secret fields, plus whatever the caller added."""
    mine = tuple(RedactionPattern.parse(path) for path in REPORT_REDACTION_PATHS)
    if caller is None:
        return RedactionPolicy(patterns=mine)
    return RedactionPolicy(
        patterns=(*mine, *caller.patterns),
        # Never narrowed: `apply_defaults` stays on unless the caller had
        # already turned it off for a document that was redacted once already.
        apply_defaults=caller.apply_defaults,
    )


def _check_size(raw: bytes, limits: ImportLimits) -> None:
    """FR-090's 1 MiB cap, on bytes, before anything is decoded."""
    if len(raw) > limits.max_bytes:
        raise _reject(
            f"the report is {len(raw)} bytes; the limit is {limits.max_bytes} (FR-090)",
            location="$",
            code=CoreErrorCode.RESOURCE_LIMIT_EXCEEDED,
        )


def _parse(raw: bytes) -> Mapping[str, JsonValue]:
    """UTF-8 JSON, or a refusal naming which of the two failed."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as undecodable:
        raise _reject(
            f"the report is not valid UTF-8: {undecodable.reason}",
            location="$",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        ) from undecodable

    try:
        document: Any = json.loads(text)
    except json.JSONDecodeError as malformed:
        raise _reject(
            f"the report is not valid JSON: {malformed.msg} at line {malformed.lineno}",
            location="$",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        ) from malformed

    if not isinstance(document, Mapping):
        raise _reject(
            f"the report must be a JSON object, not {type(document).__name__}",
            location="$",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )
    return document


def _check_schema(document: Mapping[str, JsonValue]) -> None:
    """ADR-0005 decision 2: the exact `{config, results}` document, and nothing
    else at the top level.

    Unknown top-level keys refuse rather than normalize to `null`. FR-093's
    preserve-as-null rule is about *trial metadata* — an unknown key beside
    `config` and `results` means this is a different document than the one the
    pin describes, and reading it would be guessing.
    """
    keys = set(document)
    if keys != {"config", "results"}:
        raise _reject(
            f"a pinned {REPORTER_PACKAGE} report is a {{config, results}} document; "
            f"this one has {sorted(keys) or 'no keys'}",
            location="$",
            code=CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION,
        )

    config = document["config"]
    if not isinstance(config, Mapping):
        raise _reject(
            f"`config` must be an object, not {type(config).__name__}",
            location="$.config",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )

    # A report may announce its schema; when it does, it must be the pinned one.
    # When it does not, the pin still applies — this importer only ever claims to
    # have read `REPORTER_SCHEMA`, which is what lands in the artifact.
    announced = config.get("reporterSchema")
    if announced is not None and not (
        isinstance(announced, str) and is_supported_schema(announced)
    ):
        raise _reject(
            f"this build supports {REPORTER_SCHEMA} only; the report announces "
            f"{announced!r} (ADR-0005)",
            location="$.config.reporterSchema",
            code=CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION,
        )

    version = config.get("evaluatorVersion")
    if version is not None and version != REPORTER_VERSION:
        raise _reject(
            f"this build is pinned to {REPORTER_PACKAGE} {REPORTER_VERSION}; the report "
            f"announces {version!r} (ADR-0005)",
            location="$.config.evaluatorVersion",
            code=CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION,
        )


def _results_envelope(document: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """ADR-0005's `TestResults` envelope."""
    envelope = document["results"]
    if not isinstance(envelope, Mapping):
        raise _reject(
            f"`results` must be a TestResults object, not {type(envelope).__name__}",
            location="$.results",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )
    unknown = set(envelope) - _RESULTS_KEYS
    if unknown:
        raise _reject(
            f"`results` carries unknown keys {sorted(unknown)}; the pinned TestResults "
            f"shape is {sorted(_RESULTS_KEYS)}",
            location="$.results",
            code=CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION,
        )
    return envelope


def _check_trial_count(
    envelope: Mapping[str, JsonValue], limits: ImportLimits
) -> Sequence[JsonValue]:
    """FR-090's 100-trial cap, applied to the array length before any element is
    inspected."""
    trials = envelope.get("results")
    if not isinstance(trials, Sequence) or isinstance(trials, str | bytes):
        raise _reject(
            f"`results.results` must be an array, not {type(trials).__name__}",
            location="$.results.results",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )
    if len(trials) > limits.max_trials:
        raise _reject(
            f"the report carries {len(trials)} trials; the limit is {limits.max_trials} (FR-090)",
            location="$.results.results",
            code=CoreErrorCode.RESOURCE_LIMIT_EXCEEDED,
        )
    return trials


def _check_trials(trials: Sequence[JsonValue]) -> None:
    """Each trial against ADR-0005's `TestResult` shape.

    Unknown *trial* keys are permitted here and become `null` metadata during
    normalization (FR-093). That is the opposite of the top-level rule, and
    deliberately so: upstream adds per-trial diagnostic fields between patch
    releases, and refusing a whole report over one would make the pin useless
    without making it safer. What is not permitted is a missing or unrecognised
    `outcome`, because that field is the call-level verdict itself.
    """
    for index, trial in enumerate(trials):
        location = f"$.results.results[{index}]"
        if not isinstance(trial, Mapping):
            raise _reject(
                f"trial {index} must be an object, not {type(trial).__name__}",
                location=location,
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
        outcome = trial.get("outcome")
        if outcome not in _OUTCOMES:
            raise _reject(
                f"trial {index} has outcome {outcome!r}; the pinned reporter emits "
                f"{sorted(_OUTCOMES)}",
                location=f"{location}.outcome",
                code=CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION,
            )
        trajectory = trial.get("trajectory")
        if trajectory is not None and (
            not isinstance(trajectory, Sequence) or isinstance(trajectory, str | bytes)
        ):
            raise _reject(
                f"trial {index} has a trajectory that is not an array",
                location=f"{location}.trajectory",
                code=CoreErrorCode.EVALUATION_INPUT_INVALID,
            )
