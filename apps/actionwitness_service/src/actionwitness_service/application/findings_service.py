"""The bounded findings an agent can actually read (§11.4, §23.3, AC-22).

Every other tool result is capped at 1,500 characters. This one gets 4,000, and
the exception is normative rather than generous: "a finding an agent cannot read
is equivalent to a finding that was never produced." An agent that can run a
verification but cannot learn its outcome cannot close the loop AC-22 describes.

Three rules make the budget survivable without lying about what was found:

- a default `limit` of 3 rather than 10, because the first few findings are the
  ones a reader acts on;
- each `expected` and `actual` truncated to 120 characters with an explicit
  marker, so a reader can see that a value was shortened rather than guessing;
- **the untruncated total and the report endpoint always reported.** This is the
  part that keeps the projection honest: a bounded list that did not say how
  much it left out would let an agent conclude it had seen everything.

The bounding happens here rather than in TypeScript. §23.3 limits tool *output
size*, which is a rule about the contract between the harness and an agent — and
the constitution keeps business rules out of the browser layer, where a client
could simply not apply them.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from actionwitness_core.evidence.effects import bounded
from actionwitness_core.security.limits import MAX_FINDING_VALUE_CHARS

from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.persistence.database import UnitOfWork

__all__ = ["DEFAULT_FINDING_LIMIT", "MAX_FINDING_LIMIT", "FindingsProjection"]

#: §11.4: "its default `limit` is 3 rather than 10".
DEFAULT_FINDING_LIMIT: Final = 3
#: The ceiling a caller may ask for. Beyond this the 4,000-character budget
#: cannot be met without truncating so hard the findings stop being readable.
MAX_FINDING_LIMIT: Final = 10

#: Failures first, then warnings, then the rest. A bounded list that led with
#: passing checks would spend its budget on the findings nobody needs.
_ORDER: Final = (
    "CASE status WHEN 'failed' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
    "CASE severity WHEN 'critical' THEN 0 WHEN 'major' THEN 1 ELSE 2 END, "
    "check_id"
)


class FindingsProjection:
    """One run's findings, bounded for a tool result."""

    def __init__(self, work: UnitOfWork, workspace_id: str) -> None:
        self._work = work
        self._workspace_id = workspace_id

    async def read(
        self, run_id: str, *, limit: int = DEFAULT_FINDING_LIMIT, report_path: str = ""
    ) -> dict[str, Any]:
        """§11.4's projection for `get_run_findings`."""
        if not 1 <= limit <= MAX_FINDING_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_FINDING_LIMIT}")

        run = await WorkspaceScope(self._work, self._workspace_id).run(run_id)

        rows = await self._work.fetch_all(
            f"SELECT * FROM findings WHERE run_id = ? ORDER BY {_ORDER} LIMIT ?",
            (run_id, limit),
        )
        totals = await self._work.fetch_one(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN status = 'warning' THEN 1 ELSE 0 END) AS warnings
              FROM findings WHERE run_id = ?
            """,
            (run_id,),
        )
        total = int(totals["total"]) if totals else 0

        return {
            "run_id": str(run["id"]),
            "status": str(run["status"]),
            "overall_result": run["overall_result"],
            "findings": [_projected(row) for row in rows],
            # Always present, even when nothing was elided. A field that
            # appeared only on truncation would train a reader to ignore it.
            "returned": len(rows),
            "total": total,
            "failed": int(totals["failed"] or 0) if totals else 0,
            "warnings": int(totals["warnings"] or 0) if totals else 0,
            "elided": max(0, total - len(rows)),
            #: Where the untruncated evidence lives. §23.3 keeps full report
            #: content out of tool output and in the workspace-scoped endpoint.
            "report": report_path or f"/api/v1/runs/{run_id}/report",
        }


def _projected(row: Mapping[str, Any]) -> dict[str, Any]:
    """One finding, with its values shortened and its shape kept.

    Structured, never narrated: AC-22 requires "check ID, classification, path,
    and redacted expected and actual values" and forbids returning a finding as
    prose. A sentence would be shorter and would make the finding unusable by
    the agent it was written for.
    """
    return {
        "check_id": str(row["check_id"]),
        "check_type": str(row["check_type"]),
        "status": str(row["status"]),
        "severity": str(row["severity"]),
        "classification": row["classification"],
        "path": row["path"] or _first_path(row["paths_json"]),
        "expected": _value(row["expected_json"]),
        "actual": _value(row["actual_json"]),
    }


def _first_path(paths_json: Any) -> str | None:
    """§17.1 stores either one `path` or many; a tool result shows the first."""
    if not paths_json:
        return None
    paths = json.loads(paths_json)
    return str(paths[0]) if isinstance(paths, Sequence) and paths else None


def _value(stored: Any) -> Any:
    """A stored JSON value, truncated to §11.4's 120 characters.

    `bounded` shortens strings and leaves numbers, booleans, and nulls alone —
    rewriting `1` as `"1"` would put a difference in the evidence that never
    happened.
    """
    if stored is None:
        return None
    return bounded(json.loads(stored), limit=MAX_FINDING_VALUE_CHARS)
