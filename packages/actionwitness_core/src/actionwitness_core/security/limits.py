"""Resource limits and the bounded summaries that keep evidence out of tool output.

Spec v1.9 §11.4 (tool-context budgets and the one normative `get_run_findings`
exception), §23.3 (evidence minimization: "WebMCP tool outputs should return only
a compact summary and IDs"), §20.3 (annotation text is bounded at 500
characters), FR-032 (a completion records a *bounded* output summary).

Two different concerns share this module because they share a failure mode. An
unbounded tool result exhausts an agent's context, and an unbounded stored
payload exhausts the workspace's byte budget; in both cases the damage is done by
the time anyone notices, and in both cases the fix is the same: truncate at a
declared boundary and say so.

Saying so is the part that is easy to get wrong. A silently truncated value reads
as a complete one, so `bounded_summary` always leaves a marker and
`TruncatedText` always reports the original length.
"""

from __future__ import annotations

from typing import Final

from actionwitness_core.kernel import CoreModel

__all__ = [
    "MAX_ANNOTATION_CHARS",
    "MAX_FINDINGS_RESULT_CHARS",
    "MAX_FINDING_VALUE_CHARS",
    "MAX_TOOL_DESCRIPTION_CHARS",
    "MAX_TOOL_NAME_CHARS",
    "MAX_TOOL_PARAMETER_DESCRIPTION_CHARS",
    "MAX_TOOL_RESULT_CHARS",
    "TRUNCATION_MARKER",
    "ResourceLimits",
    "TruncatedText",
    "bounded_summary",
]

#: Tool and parameter names, in characters (§11.4).
MAX_TOOL_NAME_CHARS: Final = 30

#: Tool descriptions, in characters (§11.4).
MAX_TOOL_DESCRIPTION_CHARS: Final = 500

#: Parameter descriptions, in characters (§11.4).
MAX_TOOL_PARAMETER_DESCRIPTION_CHARS: Final = 150

#: Any individual tool result, in characters (§11.4).
MAX_TOOL_RESULT_CHARS: Final = 1_500

#: The one normative exception (§11.4): `get_run_findings`, "because a finding an
#: agent cannot read is equivalent to a finding that was never produced".
MAX_FINDINGS_RESULT_CHARS: Final = 4_000

#: Each `expected` and `actual` value inside a findings result (§11.4).
MAX_FINDING_VALUE_CHARS: Final = 120

#: Human annotation text (§20.3).
MAX_ANNOTATION_CHARS: Final = 500

#: Appended to a truncated summary. Counted inside the budget, never added on top
#: of it - a marker that pushed the result over its limit would defeat the limit.
TRUNCATION_MARKER: Final = "…[truncated]"


class TruncatedText(CoreModel):
    """A bounded summary that still reports what it left out.

    `original_length` is carried so a reader - or a `get_run_findings` result,
    which §11.4 requires to "always report the untruncated total" - can tell the
    difference between a short value and a shortened one without holding the
    original.
    """

    text: str
    truncated: bool
    original_length: int


def bounded_summary(value: str, limit: int) -> TruncatedText:
    """Truncate `value` to `limit` characters, marking it when anything was lost.

    The marker is included *within* the limit, so the returned text is never
    longer than the budget the caller declared. A limit too small to hold the
    marker yields a hard cut rather than an over-budget result.
    """
    if limit < 0:
        raise ValueError("a character budget cannot be negative")
    original_length = len(value)
    if original_length <= limit:
        return TruncatedText(text=value, truncated=False, original_length=original_length)
    if limit <= len(TRUNCATION_MARKER):
        return TruncatedText(text=value[:limit], truncated=True, original_length=original_length)
    keep = limit - len(TRUNCATION_MARKER)
    return TruncatedText(
        text=value[:keep] + TRUNCATION_MARKER,
        truncated=True,
        original_length=original_length,
    )


class ResourceLimits(CoreModel):
    """The caps a caller enforces on one workspace or run.

    Target-neutral and injected rather than read from the environment: the core
    never reads configuration (constitution §1), so the application supplies the
    deployment's numbers and the core enforces whatever it is given. The defaults
    below are the specified tool-context budgets; the counting caps have no
    specified value and default to `None`, meaning "the caller has not set one"
    rather than "unlimited by design".
    """

    max_tool_result_chars: int = MAX_TOOL_RESULT_CHARS
    max_findings_result_chars: int = MAX_FINDINGS_RESULT_CHARS
    max_finding_value_chars: int = MAX_FINDING_VALUE_CHARS
    max_annotation_chars: int = MAX_ANNOTATION_CHARS
    max_events_per_run: int | None = None
    max_payload_bytes: int | None = None

    def exceeds_events(self, count: int) -> bool:
        """True when `count` has reached a configured event ceiling."""
        return self.max_events_per_run is not None and count > self.max_events_per_run

    def exceeds_payload(self, size_bytes: int) -> bool:
        """True when `size_bytes` has reached a configured payload ceiling."""
        return self.max_payload_bytes is not None and size_bytes > self.max_payload_bytes
