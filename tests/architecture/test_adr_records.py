"""ADR corpus gates (BUILD_ORDER §6; 001-preflight-baseline AC-1).

BUILD_ORDER §6 fixes the shape of a decision record: context, decision, positive
and negative consequences, rejected alternatives, status, date, and the
implementing change. A record missing the negative consequences or the rejected
alternatives is the failure mode worth catching — it reads as a justification
written after the fact rather than a decision.

The docket table in `docs/adr/README.md` is the index the rest of the project
reads. These tests keep it honest against the record files themselves.
"""

import re
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ADR_DIR = REPO_ROOT / "docs" / "adr"
TEMPLATE = ADR_DIR / "0000-template.md"
INDEX = ADR_DIR / "README.md"

VALID_STATUSES = ("Not started", "Proposed", "Accepted", "Superseded")
FILE_STATUSES = ("Proposed", "Accepted", "Superseded")

REQUIRED_SECTIONS = (
    "## Context",
    "## Decision",
    "## Consequences",
    "### Positive",
    "### Negative",
    "## Rejected alternatives",
)

RECORD_NAME = re.compile(r"^(\d{4})-[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
TITLE = re.compile(r"^# ADR-(\d{4}) — .+$", re.MULTILINE)
STATUS_FIELD = re.compile(r"^- \*\*Status:\*\* (.+)$", re.MULTILINE)
DATE_FIELD = re.compile(r"^- \*\*Date:\*\* (\d{4}-\d{2}-\d{2})$", re.MULTILINE)
IMPLEMENTING_FIELD = re.compile(r"^- \*\*Implementing change:\*\* (.+)$", re.MULTILINE)

INDEX_ROW = re.compile(
    r"^\| (?:\[)?ADR-(\d{4})(?:\]\([^)]+\))? \| [^|]+ \| ([^|]+?) \|", re.MULTILINE
)


def _records() -> list[Path]:
    """Every ADR file except the template."""
    return sorted(p for p in ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md") if p != TEMPLATE)


def _index_statuses() -> dict[str, str]:
    return {
        adr_id: status.strip()
        for adr_id, status in INDEX_ROW.findall(INDEX.read_text(encoding="utf-8"))
    }


@pytest.mark.architecture
def test_the_adr_directory_has_a_template_and_an_index() -> None:
    assert ADR_DIR.is_dir(), "expected docs/adr/ to exist"
    assert TEMPLATE.is_file(), "expected docs/adr/0000-template.md"
    assert INDEX.is_file(), "expected docs/adr/README.md as the docket index"


@pytest.mark.architecture
def test_at_least_one_decision_is_recorded() -> None:
    assert _records(), "expected at least one ADR beyond the template"


@pytest.mark.architecture
@pytest.mark.parametrize("record", _records(), ids=lambda p: p.name)
def test_record_is_structurally_complete(record: Path) -> None:
    text = record.read_text(encoding="utf-8")

    assert RECORD_NAME.match(record.name), (
        f"{record.name}: expected NNNN-kebab-title.md"
    )

    title = TITLE.search(text)
    assert title, f"{record.name}: first heading must be '# ADR-NNNN — <title>'"
    assert title.group(1) == record.name[:4], (
        f"{record.name}: heading ADR-{title.group(1)} disagrees with the filename"
    )

    missing = [section for section in REQUIRED_SECTIONS if section not in text]
    assert missing == [], f"{record.name}: missing required sections {missing}"

    status = STATUS_FIELD.search(text)
    assert status, f"{record.name}: missing '- **Status:** ...'"
    assert status.group(1).strip() in FILE_STATUSES, (
        f"{record.name}: status {status.group(1)!r} not in {FILE_STATUSES}"
    )

    stamped = DATE_FIELD.search(text)
    assert stamped, f"{record.name}: missing an ISO '- **Date:** YYYY-MM-DD'"
    assert date.fromisoformat(stamped.group(1)) <= date.today(), (
        f"{record.name}: date {stamped.group(1)} is in the future"
    )

    implementing = IMPLEMENTING_FIELD.search(text)
    assert implementing, f"{record.name}: missing '- **Implementing change:** ...'"
    assert implementing.group(1).strip(), f"{record.name}: implementing change is empty"


@pytest.mark.architecture
@pytest.mark.parametrize("record", _records(), ids=lambda p: p.name)
def test_record_states_a_rejected_alternative(record: Path) -> None:
    """The 'why not' outlives the 'why'; an empty section defeats the record."""
    text = record.read_text(encoding="utf-8")
    _, _, tail = text.partition("## Rejected alternatives")
    alternatives, _, _ = tail.partition("\n## ")
    assert re.search(r"^### .+$", alternatives, re.MULTILINE), (
        f"{record.name}: 'Rejected alternatives' must name at least one '### <Option>'"
    )


@pytest.mark.architecture
def test_index_lists_every_record_and_agrees_on_status() -> None:
    index_statuses = _index_statuses()
    assert index_statuses, "expected a docket table in docs/adr/README.md"

    unknown = {adr: s for adr, s in index_statuses.items() if s not in VALID_STATUSES}
    assert unknown == {}, f"docket rows carry unknown statuses: {unknown}"

    for record in _records():
        adr_id = record.name[:4]
        assert adr_id in index_statuses, (
            f"{record.name} is not listed in the docket table"
        )
        file_status = STATUS_FIELD.search(record.read_text(encoding="utf-8"))
        assert file_status is not None
        assert index_statuses[adr_id] == file_status.group(1).strip(), (
            f"ADR-{adr_id}: docket says {index_statuses[adr_id]!r}, "
            f"record says {file_status.group(1).strip()!r}"
        )


@pytest.mark.architecture
def test_docket_rows_without_a_record_are_marked_not_started() -> None:
    recorded = {p.name[:4] for p in _records()}
    for adr_id, status in _index_statuses().items():
        if adr_id not in recorded:
            assert status == "Not started", (
                f"ADR-{adr_id} has no record file but the docket says {status!r}"
            )
