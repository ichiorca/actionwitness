"""Transform a raw `webmcp-evals` JSON report into the importable shape.

The pinned evaluator writes `{config, results}` where `results` already has the
exact row shape the importer accepts (`{test:{name}, outcome, runIndex,
response, trajectory:[{name, arguments}]}`) — but its `config` block is the CLI
invocation, not the pinned reproducibility header `pins.is_supported_schema`
requires, and its trajectories carry every call the model made, harness tools
included.

Two transformations, both stated rather than silent:

* **The config block is replaced** with the pinned header (FR-093): schema,
  evaluator name and version from ADR-0005; provider, model, build commit and
  fixture from the arguments to this script. Nothing is guessed — a field the
  operator does not supply is refused, not defaulted, and the command mode is
  established from the raw report rather than asserted: the pinned evaluator
  writes no `commandMode` field, but its `browser` subcommand's config carries
  `url` where `local`/`analyze` carry `toolSchemaFile` (verified against
  webmcp-evals@0.0.4 `dist/commands/index.js`; `smoke` writes no JSON report
  at all). A raw report this cannot establish as a browser run is refused —
  browser and local mode verify fundamentally different things, and AC-17
  requires actual exported parameters, never invented ones.
* **Trajectories are reduced to the adapter's tool surface.** FR-091 imports
  "the imported, redacted, **allowlisted** tool calls", and the replayer
  refuses a tool the adapter does not publish — a trajectory carrying
  `arm_outcome_contract` would exclude its own trial. The evaluator's raw
  report remains the full record; this file is the replayable projection.

Usage:
    python integrations/google_evals/scenarios/transform_report.py \
        RAW_REPORT.json OUT.json \
        --model gemini-2.5-flash --commit "$(git rev-parse HEAD)" \
        --fixture buggy-store-canonical-empty-cart
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

#: The demo adapter's published tools — the only calls a trajectory may replay.
TARGET_TOOLS: frozenset[str] = frozenset(
    {"search_catalog", "get_cart", "update_cart", "apply_discount", "proceed_to_checkout"}
)

PINNED_CONFIG: dict[str, object] = {
    "reporterSchema": "webmcp-evals/0.0.4",
    "evaluatorName": "webmcp-evals",
    "evaluatorPackage": "webmcp-evals",
    "evaluatorVersion": "0.0.4",
    "modelProvider": "google",
}


def command_mode(config: object) -> str:
    """The raw run's command mode, established from its config — never assumed.

    The pinned evaluator's JSON report names no mode; the subcommand that ran
    decides which keys its config carries (webmcp-evals@0.0.4
    `dist/commands/index.js`): `browser` writes `url`, `local`/`analyze` write
    `toolSchemaFile`, and `smoke` writes no JSON report. This kit is authored
    for browser mode only (README) — local mode grades against a static tool
    schema and never drives the real page, so a local report relabelled
    "browser" would claim a verification that did not happen. Anything not
    established as a browser run is refused.
    """
    if not isinstance(config, dict):
        raise ValueError(
            "the raw report carries no config object, so its command mode cannot "
            "be established; this kit transforms browser-mode reports only"
        )
    if "toolSchemaFile" in config:
        raise ValueError(
            "the raw report is a local/analyze-mode run (its config carries "
            "toolSchemaFile, not a page url); this kit is authored for browser "
            "mode only and will not relabel a run that verified something else"
        )
    if "url" not in config:
        raise ValueError(
            "the raw report's config carries neither url nor toolSchemaFile, so "
            "it cannot be established as a browser-mode run; nothing is guessed"
        )
    return "browser"


def transform(raw: dict, *, model: str, commit: str, fixture: str) -> dict:
    mode = command_mode(raw.get("config"))
    results = raw["results"]
    rows = []
    for row in results["results"]:
        trajectory = [
            {"name": step["name"], "arguments": step["arguments"]}
            for step in row.get("trajectory") or []
            if isinstance(step, dict) and step.get("name") in TARGET_TOOLS
        ]
        rows.append(
            {
                "test": {"name": row["test"]["name"]},
                "outcome": row["outcome"],
                "runIndex": row["runIndex"],
                "response": row.get("response", ""),
                "trajectory": trajectory,
            }
        )

    # Exactly what the evaluator exported, or null when it exported nothing.
    # AC-17 forbids inventing missing values, and the normalizer's own rule
    # (normalize.py) is that an empty dict would invent the claim "the
    # evaluator reported no parameters" — absent must stay absent.
    model_parameters = raw.get("config", {}).get("modelParameters")
    return {
        "config": {
            **PINNED_CONFIG,
            "commandMode": mode,
            "modelName": model,
            "modelParameters": model_parameters,
            "targetBuildCommit": commit,
            "targetFixture": fixture,
        },
        "results": {
            "results": rows,
            "testCount": results["testCount"],
            "passCount": results["passCount"],
            "failCount": results["failCount"],
            "errorCount": results["errorCount"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw", type=Path, help="the evaluator's report-<ts>.json")
    parser.add_argument("out", type=Path, help="where to write the importable report")
    parser.add_argument("--model", required=True, help="model identifier the run used")
    parser.add_argument("--commit", required=True, help="target build commit (git rev-parse HEAD)")
    # Required like --model and --commit: targetFixture is provenance, and a
    # default would invent the very metadata this script promises not to guess.
    parser.add_argument(
        "--fixture",
        required=True,
        help="the fixture identifier the scenarios restore",
    )
    arguments = parser.parse_args()

    raw = json.loads(arguments.raw.read_text(encoding="utf-8"))
    try:
        document = transform(
            raw, model=arguments.model, commit=arguments.commit, fixture=arguments.fixture
        )
    except ValueError as refusal:
        # A refusal is the tool doing its job; the operator needs the reason,
        # not a traceback.
        raise SystemExit(f"refused: {refusal}") from refusal
    arguments.out.write_text(json.dumps(document, indent=1), encoding="utf-8")
    print(f"wrote {arguments.out} ({len(document['results']['results'])} trials)")


if __name__ == "__main__":
    main()
