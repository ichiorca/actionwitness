"""Direct WebMCP access stays in the adapter (constitution §1, §25.1; 012-T6).

The constitution: "All direct WebMCP access remains isolated in the browser
adapter." The project rule says where: `src/webmcp/adapter.ts`.

**Why this gate exists now.** T6 is the `getTools()` / `toolchange`
reconciliation task, and the risk BUILD_ORDER names for it is a reconciliation
that "silently accepted a changed surface". Two independent readers of
`getTools()` is exactly how that happens — the registration view a person reads
and the capture the server judges could look at different reads and disagree,
and the page would show "all registered" while the evidence recorded something
else. They now share one call, and this keeps a third from appearing.

It is also the ordinary reason for the rule: one place to audit when the browser
API changes, and a UI that stays testable without a browser that has WebMCP.

**The exceptions are named rather than tacit**, which is the point of writing
this down. Each is a file where going through the adapter would be wrong, not
merely inconvenient.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FRONTEND_SRC = REPO_ROOT / "apps" / "actionwitness_service" / "frontend" / "src"

#: The browser API, named the way the source names it.
_WEBMCP = "modelContext"

#: The adapter itself, plus the files where bypassing it is the correct call.
#:
#: - `webmcp/adapter.ts` — the home the constitution names.
#: - `integrations/buggyStore/poisoned.ts` — §13.3's injected surface fault. It
#:   impersonates a *hostile* registrar, and a hostile page does not politely
#:   use this app's adapter. Routing it through the adapter would make the
#:   injected attack weaker than the real one it stands in for, which would
#:   quietly weaken every test that depends on it.
#: - `spike/**` — ADR-0002's decision harness. A separate Vite entry point
#:   (`spike.html`), deliberately outside the product surface so a decision tool
#:   can never leak into it.
#: - `webmcp/compat.d.ts` — types, not access. It exists to declare
#:   `navigator.modelContext`, which the pinned `webmcp-types` package types on
#:   `Document` alone, and a declaration file cannot declare a property without
#:   naming it. Nothing here runs: the augmentation gives the adapter a typed
#:   second location to feature-detect, and detection is still what proves
#:   support (§25.12). It sits beside the adapter, in the one directory this rule
#:   is about.
#: - `webmcp/auditCollector.ts` — a string, not a call. It emits JavaScript for
#:   a *different document*: §12.17 puts the audit's enumeration and its
#:   `cart.js` read in the operator's own session on the audited storefront, and
#:   a document can reach neither across an origin. So the browser-API tokens in
#:   that file are the text of a script this page hands to a person, and nothing
#:   in it executes here. It is its own module for exactly this reason — the
#:   component that displays it stays covered by this rule.
_ALLOWED = frozenset(
    {
        "webmcp/adapter.ts",
        "webmcp/compat.d.ts",
        "webmcp/auditCollector.ts",
        "integrations/buggyStore/poisoned.ts",
    }
)
_ALLOWED_TREES = ("spike/",)


def _code(source: str) -> str:
    """`source` with comments removed.

    The gate is about *access*, not mentions. These modules discuss the browser
    API constantly — `surface.ts` explains why `getTools()` is the authority,
    `poisoned.ts` explains what the impersonator will show up in — and a scan
    over raw text would flag the explanation as the violation, which teaches
    people to stop writing the explanation.

    String literals are tracked so a `//` inside one is not mistaken for the
    start of a comment. Nothing here needs to be a real parser: it needs to not
    lie in the direction of hiding a call.
    """
    out: list[str] = []
    quote: str | None = None
    block = False
    index = 0
    while index < len(source):
        char = source[index]
        pair = source[index : index + 2]
        if block:
            if pair == "*/":
                block = False
                index += 2
                continue
        elif quote is not None:
            out.append(char)
            if char == "\\":
                out.append(source[index + 1 : index + 2])
                index += 2
                continue
            if char == quote:
                quote = None
        elif pair == "/*":
            block = True
            index += 2
            continue
        elif pair == "//":
            newline = source.find("\n", index)
            index = len(source) if newline == -1 else newline
            continue
        elif char in "\"'`":
            quote = char
            out.append(char)
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _shipped_sources() -> list[Path]:
    """Product sources only.

    Tests and the vitest setup are excluded: they mention the browser API
    precisely in order to install a double or to assert its absence, and
    scanning them would flag the explanation as the violation.
    """
    return [
        path
        for path in sorted(FRONTEND_SRC.rglob("*.ts*"))
        if ".test." not in path.name and "test" not in path.relative_to(FRONTEND_SRC).parts
    ]


def _is_allowed(relative: str) -> bool:
    return relative in _ALLOWED or relative.startswith(_ALLOWED_TREES)


@pytest.mark.architecture
def test_the_scan_finds_the_frontend() -> None:
    """The guard on the check below: an empty scan would prove nothing."""
    sources = _shipped_sources()
    assert sources, "no frontend sources were scanned, so the check is vacuous"
    assert (FRONTEND_SRC / "webmcp" / "adapter.ts").is_file(), "the adapter moved"


@pytest.mark.architecture
def test_only_the_adapter_touches_the_browser_tool_api() -> None:
    """One reader, and the exceptions are the ones written down above."""
    offenders = [
        path.relative_to(FRONTEND_SRC).as_posix()
        for path in _shipped_sources()
        if _WEBMCP in _code(path.read_text(encoding="utf-8"))
        and not _is_allowed(path.relative_to(FRONTEND_SRC).as_posix())
    ]
    assert offenders == [], (
        "direct WebMCP access outside the adapter: "
        f"{offenders}. Add a hook to `webmcp/adapter.ts` and call that, or — if "
        "bypassing the adapter is genuinely correct — add the file to the "
        "allowlist in this test with the reason."
    )


@pytest.mark.architecture
def test_every_allowlisted_exception_still_exists() -> None:
    """An allowlist nobody prunes becomes permission for files that moved.

    A stale entry is worse than a missing one: it silently re-permits whatever
    later takes that path.
    """
    for relative in _ALLOWED:
        assert (FRONTEND_SRC / relative).is_file(), (
            f"{relative} is allowlisted for direct WebMCP access but does not exist"
        )


@pytest.mark.architecture
def test_the_adapter_is_the_only_caller_of_get_tools() -> None:
    """The specific duplication T6 removed, kept removed.

    `getTools()` feeds two things that must agree: what the registration panel
    shows a person, and what the surface capture sends the server to judge. A
    second call site lets those diverge — and the divergence would look like the
    page telling the truth while the evidence said otherwise.

    `webmcp/auditCollector.ts` is excluded, and the reason is the rule rather
    than an exception to it. That module reads no surface: it emits the text of
    a script that runs on an **audited storefront**, enumerating *that*
    document's tools in the operator's own session. It cannot disagree with this
    page's registration view because it never reads this page. Excluding it by
    name keeps the property this test is really about — one reader of *our*
    surface — instead of relaxing the count to two and losing it.
    """
    callers = [
        path.relative_to(FRONTEND_SRC).as_posix()
        for path in _shipped_sources()
        if "getTools()" in _code(path.read_text(encoding="utf-8"))
        and path.relative_to(FRONTEND_SRC).as_posix() != "webmcp/auditCollector.ts"
        and not path.relative_to(FRONTEND_SRC).as_posix().startswith(_ALLOWED_TREES)
    ]
    assert callers == ["webmcp/adapter.ts"], (
        f"`getTools()` is called from {callers}; it belongs to the adapter alone "
        "so the registration view and the recorded surface come from one read"
    )
