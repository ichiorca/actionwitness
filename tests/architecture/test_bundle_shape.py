"""The bundle shape the Content-Security-Policy assumes.

009 declined to ship a CSP, and the recorded objection was precise: a policy is
"easy to write and easy to get subtly wrong against a build whose output shape is
not asserted anywhere". That is an argument for a gate, not against a policy, and
this file is the gate.

`CONTENT_SECURITY_POLICY` starts at `default-src 'none'` and carries no
`'unsafe-inline'` and no `'unsafe-eval'`. That is only safe while the frontends
stay the shape they are today: module scripts from this origin, no inline script,
no inline style, no stylesheet, no dynamic code, no off-origin asset. Each of
those is asserted below, so the failure mode the objection worried about — a
policy quietly diverging from the bundle until a page breaks in production —
shows up here, in an ordinary `uv run pytest -q`, on the commit that causes it.

Read from **source** rather than from `dist/`. A built bundle is not present in a
clean checkout, and a gate that only runs where somebody has run `npm run build`
is a gate that stops running. The build is a deterministic transform of these
files: Vite hashes and bundles, and cannot introduce an inline handler or a
remote script that the source did not ask for. The CI `image` job greps the built
filesystem, which is where the transform itself is checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

HARNESS_FRONTEND = REPO_ROOT / "apps/actionwitness_service/frontend"
STORE_FRONTEND = REPO_ROOT / "examples/buggy_store/frontend"

#: Both bundles the deployment serves from its one origin (§29.1): the harness at
#: `/` and the storefront at `/demo`. One policy covers both, so both are held to
#: it — a storefront that needed `'unsafe-inline'` would force the harness to
#: carry it too.
FRONTENDS: tuple[Path, ...] = (HARNESS_FRONTEND, STORE_FRONTEND)

ENTRY_HTML: tuple[Path, ...] = tuple(frontend / "index.html" for frontend in FRONTENDS)


def _sources(frontend: Path) -> list[Path]:
    """Every TypeScript source Vite will bundle, tests included.

    Tests are included deliberately. A test that renders a component with an
    inline style is evidence that the component takes one.
    """
    return sorted(path for suffix in ("*.ts", "*.tsx") for path in (frontend / "src").rglob(suffix))


@pytest.mark.architecture
def test_both_frontends_are_present_to_be_checked() -> None:
    """The guard on everything below.

    A moved entry point or a renamed `src/` would make every test in this file
    pass over nothing, which is the way a gate stops being one without anybody
    noticing. Both halves are asserted: the page exists, and there is source
    behind it to scan.
    """
    missing = [str(path.relative_to(REPO_ROOT)) for path in ENTRY_HTML if not path.is_file()]
    assert missing == [], f"a frontend entry point moved or was deleted: {missing}"

    empty = [frontend.parent.name for frontend in FRONTENDS if not _sources(frontend)]
    assert empty == [], f"no TypeScript source found for {empty}; these tests would scan nothing"


@pytest.mark.architecture
@pytest.mark.parametrize("entry", ENTRY_HTML, ids=lambda p: p.parent.parent.name)
def test_no_entry_page_carries_an_inline_script(entry: Path) -> None:
    """`script-src 'self'` with no hash and no nonce.

    Vite rewrites the module `src` to a hashed asset and leaves the element a
    `src` element. An inline `<script>` in the source survives the build as an
    inline `<script>` in the output, and would be refused by the browser — as a
    blank page, not as an error anybody sees in a test.
    """
    html = entry.read_text(encoding="utf-8")

    inline = [
        block
        for block in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.DOTALL | re.IGNORECASE)
        if block.strip()
    ]

    assert inline == [], f"{entry.parent.parent.name} has an inline script the CSP will refuse"


@pytest.mark.architecture
@pytest.mark.parametrize("entry", ENTRY_HTML, ids=lambda p: p.parent.parent.name)
def test_no_entry_page_carries_an_inline_style_or_a_remote_asset(entry: Path) -> None:
    """`style-src 'self'` and `default-src 'none'`.

    A `<style>` block, a `style=` attribute, and an off-origin `href` or `src`
    each fail differently and all fail silently: unstyled text, or a font that
    never arrives.
    """
    html = entry.read_text(encoding="utf-8")

    problems: list[str] = []
    if re.search(r"<style\b", html, re.IGNORECASE):
        problems.append("a <style> block")
    if re.search(r"\bstyle\s*=", html, re.IGNORECASE):
        problems.append("a style= attribute")
    remote = re.findall(r'(?:src|href)\s*=\s*["\'](https?:|//)', html, re.IGNORECASE)
    if remote:
        problems.append(f"an off-origin asset ({remote})")

    assert problems == [], f"{entry.parent.parent.name}: {', '.join(problems)}"


@pytest.mark.architecture
@pytest.mark.parametrize("frontend", FRONTENDS, ids=lambda p: p.parent.name)
def test_no_component_sets_an_inline_style(frontend: Path) -> None:
    """`style-src 'self'` without `'unsafe-inline'` refuses `style=` attributes.

    React's `style={{...}}` compiles to exactly that. This is the single most
    likely way the policy and the bundle drift apart, because it is a normal
    thing to write and nothing else in the toolchain objects to it.

    The fix, if this fails, is a stylesheet — not `'unsafe-inline'`. Adding that
    keyword back would re-admit the injected-style class of attack the policy is
    for, to save writing a CSS rule.
    """
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _sources(frontend)
        if re.search(r"\bstyle=\{", path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], (
        f"inline styles would be refused by style-src 'self': {offenders}; "
        "add a stylesheet rather than 'unsafe-inline'"
    )


@pytest.mark.architecture
@pytest.mark.parametrize("frontend", FRONTENDS, ids=lambda p: p.parent.name)
def test_nothing_injects_a_stylesheet_at_runtime(frontend: Path) -> None:
    """CSS-in-JS builds a `<style>` element, which is an inline style.

    Named libraries rather than a general check, because the general check is the
    test above and this one is about the toolchain choice that would make that
    test pass while still breaking the page.
    """
    forbidden = ("styled-components", "@emotion", 'createElement("style"', "createElement('style'")

    offenders = [
        f"{path.relative_to(REPO_ROOT)} ({marker})"
        for path in _sources(frontend)
        for marker in forbidden
        if marker in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"runtime style injection is refused by the CSP: {offenders}"


@pytest.mark.architecture
@pytest.mark.parametrize("frontend", FRONTENDS, ids=lambda p: p.parent.name)
def test_nothing_compiles_a_string_into_code(frontend: Path) -> None:
    """`'unsafe-eval'` is absent, so `eval` and `new Function` are refused.

    Worth checking separately from the styles: this one fails at the moment the
    code runs rather than at render, so it can sit unnoticed behind a branch
    nobody takes in development.
    """
    pattern = re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(")

    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _sources(frontend)
        if pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == [], f"the CSP carries no 'unsafe-eval': {offenders}"


@pytest.mark.architecture
@pytest.mark.parametrize("frontend", FRONTENDS, ids=lambda p: p.parent.name)
def test_no_source_fetches_from_another_origin(frontend: Path) -> None:
    """`connect-src 'self'`, `font-src 'self'`, `img-src 'self' data:`.

    The harness talks to `/api/v1`, the storefront to `/demo/api/v1`, and both
    are same-origin by construction (§29.1). A CDN font or an analytics beacon
    would be refused — which is the correct outcome, and this says so at the
    commit that adds one rather than at the deploy.

    Loopback URLs are allowed through: they appear in development configuration
    and Vite proxy settings, never in a shipped request path.
    """
    absolute = re.compile(r"""["'`](https?://[^"'`\s]+)""")
    local = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?", re.IGNORECASE)

    offenders = [
        f"{path.relative_to(REPO_ROOT)} -> {url}"
        for path in _sources(frontend)
        for url in absolute.findall(path.read_text(encoding="utf-8"))
        if not local.match(url) and "example" not in url and "w3.org" not in url
    ]

    assert offenders == [], f"an off-origin fetch the CSP will refuse: {offenders}"
