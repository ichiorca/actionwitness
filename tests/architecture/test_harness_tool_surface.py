"""The harness tool names must not fork (§9.11, FR-168; 014-T1).

Two copies of the same list exist, and they exist for a reason: the frontend
registers the tools and the server decides which namespace each captured tool
belongs to. §9.11 applies stability policy to the *target* partition, so a name
the server does not recognise as a harness tool lands in the watched partition —
where the tool's ordinary lifecycle appearance and disappearance (§11.5) becomes
an `added`/`removed` delta and fails the run.

That failure would be confusing in the worst way: the harness accusing the
target of mutating its surface, because somebody renamed one of the harness's
own tools. So the two lists are held in agreement here rather than by memory.

Parsed with a regex rather than a TS toolchain, because the Python lane has no
Node — the same tradeoff `test_exit_gate_traceability` makes for vitest titles.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from actionwitness_service.application.surface_service import HARNESS_TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS_TOOLS_TS = (
    REPO_ROOT / "apps" / "actionwitness_service" / "frontend" / "src" / "tools" / "harnessTools.ts"
)

#: `name: "list_contract_templates",` — the shape every entry in that file uses.
_NAME = re.compile(r'^\s*name:\s*"([a-z_]+)"', re.MULTILINE)


def _declared_in_frontend() -> set[str]:
    return set(_NAME.findall(HARNESS_TOOLS_TS.read_text(encoding="utf-8")))


@pytest.mark.architecture
def test_the_frontend_tool_module_is_still_where_the_server_expects() -> None:
    """The guard on the comparison below: an empty scan would prove nothing."""
    assert HARNESS_TOOLS_TS.is_file(), "the harness tool definitions moved"
    assert _declared_in_frontend(), "no tool names were parsed, so the check is vacuous"


@pytest.mark.architecture
def test_the_server_and_the_frontend_agree_on_the_harness_tool_names() -> None:
    """A disagreement in either direction is a defect, so both are named.

    A name the frontend registers and the server does not know puts a harness
    tool in the target partition and fails runs for using the product. A name
    the server knows and the frontend never registers quietly *excuses* a tool
    from the stability policy — which is the direction an attacker would want.
    """
    declared = _declared_in_frontend()

    assert declared - HARNESS_TOOL_NAMES == set(), (
        "the frontend registers harness tools the server does not know; they "
        "would be judged as target tools and fail stable_tool_surface"
    )
    assert HARNESS_TOOL_NAMES - declared == set(), (
        "the server excuses tool names the frontend never registers; a tool "
        "matching one of these would escape the target partition"
    )
