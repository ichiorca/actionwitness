"""The AC-17 report transform states its provenance instead of asserting it.

`integrations/google_evals/scenarios/transform_report.py` turns the pinned
evaluator's raw JSON into the importable shape. Its contract (its own
docstring) is that nothing is guessed: a field the operator does not supply is
refused, not defaulted, and the pinned header describes the run that actually
happened. These tests hold it to that.

The mode question is the sharp one. The pinned `webmcp-evals@0.0.4` JSON
report carries no `commandMode` field at all — the subcommand that ran decides
which keys its `config` block holds (`browser` writes `url`; `local`/`analyze`
write `toolSchemaFile`; `smoke` writes no JSON report). Browser and local mode
verify fundamentally different things, so a transform that stamped `"browser"`
onto whatever it was handed would let a local run impersonate the browser
proof AC-17 rests on. The transform must therefore *establish* the mode from
the raw config and refuse anything it cannot establish as a browser run.

The script is stdlib-only and loaded here by path: `tests/` is not a package,
and the scenarios kit is an operator tool rather than an installed
distribution, so its public entry points are the `transform` function and the
CLI itself.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "integrations" / "google_evals" / "scenarios" / "transform_report.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("transform_report", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def transform_report() -> Any:
    return _load_module()


def _raw_report(config: dict[str, Any]) -> dict[str, Any]:
    """A minimal raw report in the pinned evaluator's own shape."""
    return {
        "config": config,
        "results": {
            "results": [
                {
                    "test": {"name": "SAVE20 on one mug against the faulty build"},
                    "outcome": "pass",
                    "runIndex": 0,
                    "response": "done",
                    "trajectory": [
                        {"name": "update_cart", "arguments": {"product_id": "mug-ceramic-001"}},
                        {"name": "arm_outcome_contract", "arguments": {}},
                    ],
                }
            ],
            "testCount": 1,
            "passCount": 1,
            "failCount": 0,
            "errorCount": 0,
        },
    }


#: What the pinned evaluator's browser subcommand actually writes (verified
#: against webmcp-evals@0.0.4 `dist/commands/index.js`): the page URL, never a
#: tool-schema file.
BROWSER_CONFIG: dict[str, Any] = {
    "url": "http://localhost:5173",
    "evalsFile": "save20_suite.json",
    "backend": "gemini",
    "model": "gemini-2.5-flash",
    "runs": 3,
}

#: …and what its local/analyze subcommands write instead.
LOCAL_CONFIG: dict[str, Any] = {
    "toolSchemaFile": "tools.json",
    "evalsFile": "save20_suite.json",
    "backend": "gemini",
    "model": "gemini-2.5-flash",
    "runs": 3,
}


class TestCommandModeIsReadNotAsserted:
    def test_a_browser_mode_report_is_labelled_browser(self, transform_report: Any) -> None:
        # Arrange
        raw = _raw_report(BROWSER_CONFIG)

        # Act
        document = transform_report.transform(
            raw, model="gemini-2.5-flash", commit="abc123", fixture="fx"
        )

        # Assert — the label states what the raw report established.
        assert document["config"]["commandMode"] == "browser"

    def test_a_local_mode_report_is_refused_rather_than_relabelled(
        self, transform_report: Any
    ) -> None:
        """A local run transformed by this kit must not silently claim browser."""
        # Arrange
        raw = _raw_report(LOCAL_CONFIG)

        # Act / Assert
        with pytest.raises(ValueError, match="browser"):
            transform_report.transform(raw, model="gemini-2.5-flash", commit="abc123", fixture="fx")

    def test_a_report_whose_mode_cannot_be_established_is_refused(
        self, transform_report: Any
    ) -> None:
        """No `url`, no `toolSchemaFile`: nothing is guessed, so nothing passes."""
        # Arrange — a config carrying neither discriminating key.
        raw = _raw_report({"backend": "gemini", "model": "gemini-2.5-flash"})

        # Act / Assert
        with pytest.raises(ValueError, match="browser"):
            transform_report.transform(raw, model="gemini-2.5-flash", commit="abc123", fixture="fx")

    def test_the_pinned_header_no_longer_carries_a_mode_constant(
        self, transform_report: Any
    ) -> None:
        """The mode is per-report evidence; a constant would reassert the bug."""
        assert "commandMode" not in transform_report.PINNED_CONFIG


class TestNothingIsDefaulted:
    def test_the_fixture_argument_is_required(
        self,
        transform_report: Any,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """`targetFixture` is provenance; a default would invent it (AC-17)."""
        # Arrange — a real raw report on disk, and every argument except --fixture.
        raw_path = tmp_path / "raw.json"
        raw_path.write_text(json.dumps(_raw_report(BROWSER_CONFIG)), encoding="utf-8")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "transform_report.py",
                str(raw_path),
                str(tmp_path / "out.json"),
                "--model",
                "gemini-2.5-flash",
                "--commit",
                "abc123",
            ],
        )

        # Act
        with pytest.raises(SystemExit) as refusal:
            transform_report.main()

        # Assert — argparse's usage refusal, naming the missing argument.
        assert refusal.value.code == 2
        assert "--fixture" in capsys.readouterr().err
        assert not (tmp_path / "out.json").exists()

    def test_a_fully_specified_invocation_writes_the_supplied_provenance(
        self, transform_report: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Arrange
        raw_path = tmp_path / "raw.json"
        raw_path.write_text(json.dumps(_raw_report(BROWSER_CONFIG)), encoding="utf-8")
        out_path = tmp_path / "out.json"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "transform_report.py",
                str(raw_path),
                str(out_path),
                "--model",
                "gemini-2.5-flash",
                "--commit",
                "abc123",
                "--fixture",
                "buggy-store-canonical-empty-cart",
            ],
        )

        # Act
        transform_report.main()

        # Assert
        document = json.loads(out_path.read_text(encoding="utf-8"))
        config = document["config"]
        assert config["targetFixture"] == "buggy-store-canonical-empty-cart"
        assert config["targetBuildCommit"] == "abc123"
        assert config["modelName"] == "gemini-2.5-flash"
        assert config["commandMode"] == "browser"
        # Absent raw parameters stay absent rather than becoming `{}` (AC-17).
        assert config["modelParameters"] is None
        # The trajectory kept the target call and dropped the harness call.
        trajectory = document["results"]["results"][0]["trajectory"]
        assert [step["name"] for step in trajectory] == ["update_cart"]
