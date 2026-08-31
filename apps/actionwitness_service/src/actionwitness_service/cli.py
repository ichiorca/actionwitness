"""`actionwitness` CLI (spec v1.8 §24.6, exit-code contract FR-088).

Exit codes: 0 = expectation met; 1 = valid run, expectation not met;
2 = invalid eval definition or harness execution.

Scaffolding: argument surface only. Until the Tier 2 runner lands, every command
reports itself as not-yet-implemented and exits 2 (an invalid harness execution),
so nothing can masquerade as a passing eval.
"""

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="actionwitness")
    sub = parser.add_subparsers(dest="command", required=True)

    ev = sub.add_parser("eval", help="Validate or run built-in regression eval cases")
    evsub = ev.add_subparsers(dest="eval_command", required=True)

    validate = evsub.add_parser("validate", help="Schema-validate an eval case (§24.6)")
    validate.add_argument("case", help="Path to case.json")

    run = evsub.add_parser("run", help="Deterministically replay an eval case (§24.3)")
    run.add_argument("case", help="Path to case.json")
    run.add_argument("--environment", choices=["current", "reproduce_source"], default="current")
    run.add_argument("--report-dir", default=".evals")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(f"actionwitness: '{args.command}' is scaffolded but not implemented yet (Tier 2).")
    return 2  # invalid harness execution until the runner exists (FR-088)


if __name__ == "__main__":
    sys.exit(main())
