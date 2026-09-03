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

Three frontend files are scanned, because the harness registers by three
mechanisms: the hook module, §25.2's declarative form whose `toolname`
attribute *is* its registration (012-T5), and the native registration in
`tools/workspaceStatus.ts` (ADR-0002 rule 3). All reach `getTools()`
identically, so all have to be partitioned identically — the native one was
the miss that put `get_workspace_status` in the target partition unnoticed.

Parsed with a regex rather than a TS toolchain, because the Python lane has no
Node — the same tradeoff `test_exit_gate_traceability` makes for vitest titles.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from actionwitness_service.application.surface_service import HARNESS_TOOL_NAMES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FRONTEND_SRC = REPO_ROOT / "apps" / "actionwitness_service" / "frontend" / "src"
HARNESS_TOOLS_TS = FRONTEND_SRC / "tools" / "harnessTools.ts"

#: §25.2's declarative tool lives in the form that *is* its registration, not in
#: the hook module — the browser reads `toolname` off the markup and nothing
#: calls `registerTool`. It still reaches `getTools()` like any other tool, so
#: it still has to be partitioned as one (012-T5).
CONTRACT_FORM_TSX = FRONTEND_SRC / "components" / "ContractForm.tsx"

#: The native registration (ADR-0002 rule 3): `get_workspace_status` never
#: passes through the hook module, which is exactly how it went missing from
#: the server's partition once.
WORKSPACE_STATUS_TS = FRONTEND_SRC / "tools" / "workspaceStatus.ts"

#: `name: "list_contract_templates",` — the shape every entry in that file uses.
_NAME = re.compile(r'^\s*name:\s*"([a-z_]+)"', re.MULTILINE)

#: `export const CREATE_CONTRACT_TOOL = "create_outcome_contract";` and
#: `export const GET_WORKSPACE_STATUS = "get_workspace_status";` — an exported
#: string constant is each file's single source for its tool name.
_CONSTANT_NAME = re.compile(r'^export const [A-Z_]+ = "([a-z_]+)";', re.MULTILINE)


def _declared_in_frontend() -> set[str]:
    return (
        set(_NAME.findall(HARNESS_TOOLS_TS.read_text(encoding="utf-8")))
        | set(_CONSTANT_NAME.findall(CONTRACT_FORM_TSX.read_text(encoding="utf-8")))
        | set(_CONSTANT_NAME.findall(WORKSPACE_STATUS_TS.read_text(encoding="utf-8")))
    )


@pytest.mark.architecture
def test_the_frontend_tool_modules_are_still_where_the_server_expects() -> None:
    """The guard on the comparison below: an empty scan would prove nothing."""
    assert HARNESS_TOOLS_TS.is_file(), "the harness tool definitions moved"
    assert CONTRACT_FORM_TSX.is_file(), "the declarative contract form moved"
    assert WORKSPACE_STATUS_TS.is_file(), "the native status tool moved"
    assert _declared_in_frontend(), "no tool names were parsed, so the check is vacuous"


@pytest.mark.architecture
def test_the_declarative_tool_is_scanned_too() -> None:
    """A second guard, because the two files are parsed by different patterns.

    `harnessTools.ts` writes `name: "…"` inside an object; the form writes an
    exported constant. If the declarative pattern silently stopped matching, the
    union above would still be non-empty and the check below would still pass —
    while `create_outcome_contract` quietly became a name the server excuses and
    nothing declares, which is the direction an attacker would want. The native
    status tool is guarded for the same reason — its pattern is the constant
    export, not the hook module's `name:` shape.
    """
    assert "create_outcome_contract" in _declared_in_frontend()
    assert "get_workspace_status" in _declared_in_frontend()


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
