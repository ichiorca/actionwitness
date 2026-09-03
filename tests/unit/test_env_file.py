"""Loading `.env` (spec §29.1's configuration surface; constitution §5).

The bug this covers is a gap between documentation and deployment. `.env.example`
lists every variable, `.gitignore` protects the real file, and
`scripts/scan_for_secrets.py` refuses to let it be committed — but nothing read
it, so an operator who put a credential there and started the service saw the
module report `disabled` for something they had just configured.

Three properties carry the weight here.

**The process environment wins.** A file that could override an explicit
`FOO=bar uv run ...` would make the override silently do nothing.

**Nothing is fatal.** A stray line, a missing file, a binary file, a file too
large: each disables nothing and takes the service down never. This mirrors
`config.py`'s own rule that construction never raises.

**No value is ever logged.** This file exists to hold secrets, so the tests
assert on what reaches the log records rather than trusting the implementation
to have been careful — including for the unparseable lines, which are exactly
as likely as any other to contain a credential.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from actionwitness_service.env_file import (
    ENV_FILE_VARIABLE,
    compose_environment,
    parse_env_file,
    read_env_file,
)

pytestmark = pytest.mark.unit


# --- parsing ----------------------------------------------------------------


def test_a_plain_assignment_is_read() -> None:
    assert parse_env_file("LIVE_EVALUATOR_ENABLED=true") == {"LIVE_EVALUATOR_ENABLED": "true"}


def test_comments_and_blank_lines_are_ignored() -> None:
    # `.env.example` ships every variable commented out, so a copied file is
    # mostly comments. Reading one as an assignment would define a variable
    # named `#LIVE_EVALUATOR_ENABLED`.
    text = "\n".join(["# a comment", "", "   ", "#LIVE_EVALUATOR_ENABLED=false", "A=1"])

    assert parse_env_file(text) == {"A": "1"}


def test_an_export_prefix_is_tolerated() -> None:
    """People paste these files out of shell scripts."""
    assert parse_env_file("export GOOGLE_AI=abc") == {"GOOGLE_AI": "abc"}


def test_one_layer_of_matching_quotes_is_stripped() -> None:
    parsed = parse_env_file("A=\"two words\"\nB='single'\nC=\"mismatched'")

    assert parsed["A"] == "two words"
    assert parsed["B"] == "single"
    # Not a matching pair, so nothing is stripped rather than a guess being made.
    assert parsed["C"] == "\"mismatched'"


def test_escape_sequences_are_left_alone() -> None:
    r"""`\n` stays two characters.

    Interpreting it would corrupt any credential that legitimately contains a
    backslash, and every other tool that reads these files leaves it alone.
    """
    assert parse_env_file(r'KEY="a\nb"') == {"KEY": r"a\nb"}


def test_a_value_may_contain_equals_signs() -> None:
    """Base64 and JWT-shaped values end in `=` padding all the time."""
    assert parse_env_file("TOKEN=abc=def==") == {"TOKEN": "abc=def=="}


def test_an_empty_value_is_kept_and_is_not_an_absence() -> None:
    # `FOO=` is a deliberate empty string. Dropping it would silently fall back
    # to a default the operator was trying to clear.
    assert parse_env_file("FOO=") == {"FOO": ""}


@pytest.mark.parametrize(
    "line", ["no equals sign here", "1STARTS_WITH_DIGIT=x", "has spaces=x", "has-a-dash=x", "=x"]
)
def test_an_unparseable_line_is_skipped_rather_than_fatal(line: str) -> None:
    """One stray line must not take down a service that would otherwise start."""
    assert parse_env_file(f"{line}\nGOOD=1") == {"GOOD": "1"}


# --- precedence -------------------------------------------------------------


def test_the_process_environment_wins_over_the_file(tmp_path: Path) -> None:
    """An explicit override now beats a default written earlier."""
    (tmp_path / ".env").write_text("SHARED=from_file\nONLY_FILE=file\n", encoding="utf-8")

    composed = compose_environment({"SHARED": "from_process"}, root=tmp_path)

    assert composed["SHARED"] == "from_process"
    assert composed["ONLY_FILE"] == "file"


def test_the_result_does_not_change_when_the_source_mapping_does(tmp_path: Path) -> None:
    """Settings a deployment resolved must not move underneath it."""
    (tmp_path / ".env").write_text("A=1\n", encoding="utf-8")
    process: dict[str, str] = {}

    composed = compose_environment(process, root=tmp_path)
    process["B"] = "added later"

    assert "B" not in composed


def test_the_file_location_can_be_redirected(tmp_path: Path) -> None:
    elsewhere = tmp_path / "custom.env"
    elsewhere.write_text("A=1\n", encoding="utf-8")

    composed = compose_environment({ENV_FILE_VARIABLE: str(elsewhere)}, root=tmp_path)

    assert composed["A"] == "1"


def test_a_missing_file_is_the_ordinary_case(tmp_path: Path) -> None:
    """Most deployments configure the process directly; that is not an error."""
    composed = compose_environment({"A": "1"}, root=tmp_path)

    assert composed["A"] == "1"


def test_a_file_too_large_to_be_configuration_is_ignored(tmp_path: Path) -> None:
    # Somebody pointed the loader at the wrong file. Reading it whole to find
    # that out would be the wrong order of operations.
    (tmp_path / ".env").write_text("A=" + "x" * (256 * 1024), encoding="utf-8")

    assert "A" not in compose_environment({}, root=tmp_path)


def test_a_file_that_is_not_text_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    (tmp_path / ".env").write_bytes(b"\xff\xfe\x00binary")

    assert compose_environment({}, root=tmp_path) == {}


def test_a_directory_where_the_file_should_be_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".env").mkdir()

    assert compose_environment({}, root=tmp_path) == {}


# --- secrets never reach the log --------------------------------------------


def test_no_value_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The names are the diagnostic; the values are the secret.

    Asserted against the emitted records rather than trusted to the
    implementation, because this is the one property whose failure is an
    incident rather than a bug.
    """
    secret = "sk-do-not-log-this-value"
    (tmp_path / ".env").write_text(f"GOOGLE_AI={secret}\nnot a valid line\n", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger="actionwitness.config"):
        composed = compose_environment({}, root=tmp_path)

    assert composed["GOOGLE_AI"] == secret
    emitted = "\n".join(record.getMessage() for record in caplog.records)
    assert secret not in emitted
    # The name is present, because that is what makes a missing variable
    # diagnosable from a log an operator can safely paste into a bug report.
    assert "GOOGLE_AI" in emitted
    # And the unparseable line is counted, not quoted.
    assert "not a valid line" not in emitted


def test_reading_returns_nothing_for_an_absent_path(tmp_path: Path) -> None:
    assert read_env_file(tmp_path / "nope.env") == {}
