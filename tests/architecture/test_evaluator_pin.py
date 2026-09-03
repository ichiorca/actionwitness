"""008-T1 — the evaluator pin agrees with ADR-0005 (BUILD_ORDER §7/M7).

ADR-0005 is the reasoning and `integrations.google_evals.pins` is the executable
half. The failure this gate exists for is the two drifting apart: somebody bumps
the constant to make a regenerated fixture parse, the record still says 0.0.4,
and the benchmark's provenance claim quietly stops being true.

Read out of the record rather than duplicated here, so this file cannot become a
third place the pin is written down.

**The record is operator-held and untracked** — the ADR corpus left version
control deliberately (commit 8b59084; the README says the records "are kept
outside this repository at the operator's direction"). So the pin-vs-record
cross-check runs exactly where it can: in a working tree that carries the
corpus, which is also the only place the drift it guards against would be
introduced. In a clone or CI checkout the record does not exist, and those
assertions skip *with this explanation* rather than failing on a file no
clone carries — the same accommodation `test_documentation_references` made
when it replaced the ADR-corpus gates. Everything provable from tracked
sources alone stays unconditional.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from integrations.google_evals.pins import (
    NORMALIZER_VERSION,
    REPORTER_COMMIT,
    REPORTER_PACKAGE,
    REPORTER_SCHEMA,
    REPORTER_VERSION,
    SUPPORTED_REPORTER_SCHEMAS,
    is_supported_schema,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RECORD = REPO_ROOT / "docs" / "adr" / "0005-external-evaluator-binding.md"


def _record() -> str:
    """The decision record, or an explained skip where the corpus is absent."""
    if not RECORD.is_file():
        pytest.skip(
            "ADR-0005 is operator-held and untracked (commit 8b59084); the "
            "pin-vs-record cross-check runs only in a tree carrying the corpus"
        )
    return RECORD.read_text(encoding="utf-8")


@pytest.mark.architecture
def test_the_tracked_pins_are_internally_consistent() -> None:
    """The half of the record that lives in tracked code, asserted everywhere.

    The three record-reading tests below skip in a checkout without the
    operator-held corpus; these two facts must not skip with them, because a
    clone can and should still prove them.
    """
    # Arrange / Act / Assert
    assert f"{REPORTER_PACKAGE}/{REPORTER_VERSION}" == REPORTER_SCHEMA
    assert NORMALIZER_VERSION == "1"


@pytest.mark.architecture
def test_the_pinned_version_and_commit_are_the_ones_the_record_decided() -> None:
    """ADR-0005 decision 1, verbatim: v0.0.4, commit `fe33c1b`."""
    # Arrange
    text = _record()

    # Act
    decision = re.search(r"\*\*Pin `webmcp-evals` v([\d.]+), commit `([0-9a-f]+)`\*\*", text)

    # Assert
    assert decision is not None, "ADR-0005 no longer states its pin in the decided form"
    assert decision.groups() == (REPORTER_VERSION, REPORTER_COMMIT), (
        f"pins.py says {REPORTER_VERSION}/{REPORTER_COMMIT}, ADR-0005 says {decision.groups()}"
    )


@pytest.mark.architecture
def test_the_schema_label_is_the_one_written_into_normalized_artifacts() -> None:
    """ADR-0005 decision 2 names the exact label; artifacts are read by people
    who were not here when it was chosen."""
    # Arrange / Act / Assert
    assert f"{REPORTER_PACKAGE}/{REPORTER_VERSION}" == REPORTER_SCHEMA
    assert REPORTER_SCHEMA in _record(), "the schema label in pins.py appears nowhere in ADR-0005"


@pytest.mark.architecture
def test_the_normalizer_version_is_recorded() -> None:
    """ADR-0005 decision 3: normalizer version 1, recorded in every normalized
    artifact beside the reporter schema."""
    # Arrange / Act / Assert
    assert NORMALIZER_VERSION == "1"
    assert "Normalizer version 1" in _record()


@pytest.mark.architecture
def test_the_allowlist_holds_exactly_the_pinned_schema() -> None:
    """FR-090 admits an allowlist, and today it has one member.

    Asserted as equality: a second entry is a decision that belongs in a
    superseding record, not a quiet addition.
    """
    # Arrange / Act / Assert
    assert frozenset({REPORTER_SCHEMA}) == SUPPORTED_REPORTER_SCHEMAS


@pytest.mark.architecture
@pytest.mark.parametrize(
    "announced",
    [
        "webmcp-evals/0.0.3",
        "webmcp-evals/0.0.5",
        "webmcp-evals/0.0.40",
        "webmcp-evals",
        "",
        "WEBMCP-EVALS/0.0.4",
    ],
)
def test_an_unpinned_schema_is_refused(announced: str) -> None:
    """Exact membership, never a prefix or a version comparison.

    `webmcp-evals/0.0.40` is the case that makes this worth a test: a
    `startswith` check admits it, and it is a different report shape.
    """
    # Arrange / Act / Assert
    assert is_supported_schema(announced) is False


@pytest.mark.architecture
def test_the_pinned_schema_is_accepted() -> None:
    """The counterpart — a gate that refused everything would also pass above."""
    # Arrange / Act / Assert
    assert is_supported_schema(REPORTER_SCHEMA) is True
