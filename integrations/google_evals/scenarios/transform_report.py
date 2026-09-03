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
  operator does not supply is refused, not defaulted.
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
    "commandMode": "browser",
    "modelProvider": "google",
}


def transform(raw: dict, *, model: str, commit: str, fixture: str) -> dict:
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
    parser.add_argument(
        "--fixture",
        default="buggy-store-canonical-empty-cart",
        help="the fixture identifier the scenarios restore",
    )
    arguments = parser.parse_args()

    raw = json.loads(arguments.raw.read_text(encoding="utf-8"))
    document = transform(
        raw, model=arguments.model, commit=arguments.commit, fixture=arguments.fixture
    )
    arguments.out.write_text(json.dumps(document, indent=1), encoding="utf-8")
    print(f"wrote {arguments.out} ({len(document['results']['results'])} trials)")


if __name__ == "__main__":
    main()
