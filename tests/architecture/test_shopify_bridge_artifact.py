"""The Shopify theme bridge, as an artifact (FR-111, FR-112, FR-115, AC-18).

`shopify_bridge/` is not like the rest of this repository. It is a file an
operator pastes into **somebody else's Shopify theme**, where it runs at their
origin, in their shopper's session, beside their apps. Nothing in the harness's
own lanes reaches it: it is not in the Python workspace, it is not in the
frontend bundle (`npm run build` never imports it), and the browser lane never
loads a storefront.

So the properties that make it safe to install have to be asserted about the
*text of the file*, which is what this module does. Its behaviour — fragment
stripping, redirect and size refusals, tool registration and unregistration — is
covered separately and behaviourally by
`apps/actionwitness_service/frontend/src/shopifyBridge.test.ts`. The two are
complementary: that suite proves the paths it walks are correct, and this one
proves there is no path it did not walk.

The rule this file is written against, from `memory/rules/shopify-rules.md`:
"NEVER place credentials, cart tokens, or raw payloads in query strings, browser
storage, logs, telemetry, reports, or tool results", and cart-only behaviour with
no checkout, order, customer, or payment path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

BRIDGE_DIR = REPO_ROOT / "shopify_bridge"
BRIDGE = BRIDGE_DIR / "actionwitness-bridge.js"
BRIDGE_TYPES = BRIDGE_DIR / "actionwitness-bridge.d.ts"
SNIPPET = BRIDGE_DIR / "snippets" / "actionwitness-bridge.liquid"
BRIDGE_README = BRIDGE_DIR / "README.md"

DOCKERIGNORE = REPO_ROOT / ".dockerignore"
DOCKERFILE = REPO_ROOT / "Dockerfile"

BRIDGE_TESTS = REPO_ROOT / "apps/actionwitness_service/frontend/src/shopifyBridge.test.ts"


def _source() -> str:
    return BRIDGE.read_text(encoding="utf-8")


def _code() -> str:
    """The bridge with its comments removed.

    The file explains at length what it refuses to do, and a scan over raw text
    would read the explanation as the violation — which teaches the next author
    to delete the explanation. Block and line comments only; string literals are
    left in place, because a forbidden call hidden in a string is still worth
    seeing.
    """
    without_block = re.sub(r"/\*.*?\*/", "", _source(), flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", without_block, flags=re.MULTILINE)


@pytest.mark.architecture
def test_the_bridge_is_present_to_be_checked() -> None:
    """The guard on everything below.

    A renamed or deleted bridge would make every assertion in this file pass
    over nothing, which is how a gate stops being one without anybody noticing.
    """
    for path in (BRIDGE, BRIDGE_TYPES, SNIPPET, BRIDGE_README):
        assert path.is_file(), f"{path.relative_to(REPO_ROOT)} is missing"
    assert BRIDGE_TESTS.is_file(), "the bridge's behavioural suite moved or was deleted"
    assert "ActionWitnessBridge" in _source(), "the bridge no longer exports its namespace"


@pytest.mark.architecture
def test_the_bridge_needs_no_build_step() -> None:
    """It is read before it is installed, so it must be readable.

    A merchant is being asked to paste this into their theme. A bundler, a
    `package.json`, or a transpiled output would put a build artifact between
    them and the thing they are agreeing to run, and "trust the minified file"
    is not a request this project gets to make of somebody else's storefront.
    """
    assert not (BRIDGE_DIR / "package.json").exists(), (
        "the bridge is checked in unbuilt on purpose; a manifest here means a build step"
    )
    code = _code()
    assert "import " not in code and "export " not in code, (
        "the bridge must stay a classic script: a module is deferred, so it would run "
        "after the third-party theme scripts FR-111 requires it to precede"
    )


#: Every route into persistent client-side state. FR-111: the credential is
#: "redeemed once, and thereafter represented by a bounded session-scoped
#: credential" — one that must not survive the frame it was issued to.
_PERSISTENCE = ("localStorage", "sessionStorage", "document.cookie", "indexedDB", "caches")


@pytest.mark.architecture
@pytest.mark.parametrize("api", _PERSISTENCE)
def test_the_bridge_stores_nothing(api: str) -> None:
    """No credential, no cart token, nowhere durable.

    This is what makes "a reload ends the pairing" a fact about the design rather
    than a promise, and it is only true while the bridge touches no storage *at
    all*. A single `setItem` for something innocuous would establish the habit,
    and the next value stored would be the one that matters.
    """
    assert api not in _code(), f"the bridge must not touch {api}"


#: Cart-only, and it is a safety boundary rather than a scope preference: an
#: accidental order on somebody's development store is not recoverable by a code
#: change. FR-114 forbids each of these for this contract.
_FORBIDDEN_PATHS = (
    "/cart/add",
    "/cart/change",
    "/cart/update",
    "/cart/clear",
    "/checkout",
    "/account/login",
    "/orders",
)


@pytest.mark.architecture
@pytest.mark.parametrize("path", _FORBIDDEN_PATHS)
def test_the_bridge_names_no_mutation_or_checkout_endpoint(path: str) -> None:
    """`cart.js` is a read. Everything above it is a write or a purchase."""
    assert path not in _code(), (
        f"the bridge references {path}; it observes the cart and never changes it, "
        "navigates to checkout, logs a customer in, or creates an order"
    )


@pytest.mark.architecture
def test_the_bridge_never_navigates_the_storefront() -> None:
    """A navigation is how a cart-only bridge reaches checkout by accident.

    Assigning `location.href`, calling `assign`/`replace`, or submitting a form
    would each move the shopper's tab, and AC-18 requires the run to record *no*
    checkout navigation. `location.hash` is excluded deliberately: it is the
    `replaceState` fallback that removes the credential from the visible URL, and
    it changes no document.
    """
    offenders = [
        marker
        for marker in ("location.href =", "location.assign", "location.replace", ".submit()")
        if marker in _code()
    ]
    assert offenders == [], f"the bridge navigates the storefront: {offenders}"


@pytest.mark.architecture
def test_the_credential_leaves_the_url_and_never_enters_a_query_string() -> None:
    """FR-111's two halves, as properties of the file.

    The strip itself is asserted behaviourally next door; what is asserted here
    is that the mechanism is *present* — a bridge that read the fragment and
    never called `replaceState` would pass every other test in this file.
    """
    code = _code()
    assert "history.replaceState" in code, "the bridge does not remove the fragment from the URL"
    assert "location.hash" in code, "the bridge does not read the credential from the fragment"
    # The credential travels in a header. A URL built with it would put it in
    # every proxy log between the storefront and the harness.
    assert "Authorization" in code and "Bearer " in code, (
        "bridge requests must authorize with a bearer credential, never a query parameter"
    )
    assert not re.search(r"[?&](credential|token|pairing_credential)=", code), (
        "a credential appears in a query string"
    )


@pytest.mark.architecture
def test_the_cart_read_is_locale_aware_and_bounded() -> None:
    """FR-112, as the three things that make the observation independent.

    A hard-coded `/cart.js` is the quiet failure: it works on every store the
    developer tested and returns the wrong locale's cart — or a redirect — on a
    localised one, at which point the "independent observation" is of something
    else.
    """
    code = _code()
    assert "Shopify.routes" in code or "routes.root" in code, (
        "the cart URL must be built from the locale-aware storefront root"
    )
    assert "redirect" in code, "a redirected cart read must be refused"
    assert "256 * 1024" in code, "FR-112's 256 KiB cap is not stated in the source"


@pytest.mark.architecture
def test_the_tool_matches_appendix_d3_and_can_be_unregistered() -> None:
    """§11.3: the pairing is session state and is never a tool argument.

    An `inputSchema` with a property on it would be the failure — a pairing id in
    the schema hands the session to whatever reads the tool surface, and an agent
    could then pair itself.
    """
    code = _code()
    assert "verify_shopify_outcome" in code
    assert "additionalProperties: false" in code
    assert re.search(r"properties:\s*\{\s*\}", code), (
        "Appendix D.3's schema takes no arguments; the pairing is never one"
    )
    # Registration is undone by aborting the signal it was given, so the
    # unregistration cannot be forgotten separately from the registration.
    assert "registerTool" in code and ".abort()" in code, (
        "the bridge must be able to unregister the tool on every terminal state (§16.5)"
    )


@pytest.mark.architecture
def test_the_bridge_requires_an_https_harness_and_store_origin() -> None:
    """FR-110: one exact configured origin, and no way to widen it from a link.

    The origins come from the theme's own `<script>` attributes rather than from
    the launch URL, which is the difference between "the operator installed this
    pointing at their harness" and "whoever composed the link chose where these
    observations go".
    """
    code = _code()
    assert 'protocol !== "https:"' in code, "the bridge accepts a non-HTTPS origin"
    assert "data-harness-origin" in code and "data-store-origin" in code, (
        "the origins must come from the installed script element, not from the URL"
    )
    # The `{% comment %}` block is stripped first: it *explains* why `defer`,
    # `async` and `type="module"` are wrong here, and a scan over the raw file
    # would read that explanation as the violation — which is how the next
    # author learns to delete the explanation.
    snippet = re.sub(
        r"\{%-?\s*comment\s*-?%\}.*?\{%-?\s*endcomment\s*-?%\}",
        "",
        SNIPPET.read_text(encoding="utf-8"),
        flags=re.DOTALL,
    )
    assert "<script" in snippet, "the snippet no longer loads the bridge"
    for forbidden in ("defer", "async", 'type="module"'):
        assert forbidden not in snippet, (
            f"the theme snippet uses {forbidden}, which runs the bridge after the "
            "third-party scripts FR-111 requires it to precede"
        )


@pytest.mark.architecture
def test_the_bridge_is_not_shipped_in_the_release_image() -> None:
    """It is installed on a storefront, not served by the harness.

    Two locks, matching `test_release_artifact_hygiene.py`'s reasoning. The
    Dockerfile copies narrowly, so the bridge is absent by default;
    `.dockerignore` says so explicitly as well, because "absent because nobody
    added it" stops being true the first time somebody adds a broad `COPY`.

    It stays checked into git deliberately — an operator has to read and upload
    it — so this is about the *image*, not about the repository.
    """
    entries = {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "shopify_bridge/" in entries, (
        ".dockerignore no longer excludes shopify_bridge/; the theme bridge is installed "
        "on a storefront and has no place in the harness image"
    )
    assert "shopify_bridge" not in DOCKERFILE.read_text(encoding="utf-8"), (
        "the Dockerfile copies the theme bridge into the image"
    )
