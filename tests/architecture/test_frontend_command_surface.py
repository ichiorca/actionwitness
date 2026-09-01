"""Frontend command-surface gates (BUILD_ORDER §7/M0; 001-preflight-baseline AC-3).

The M0 exit gate names four frontend commands, and one of them is easy to lose:
`npm run build` is bundling, not type-checking. Vite strips types without
checking them, so a project that declares only `build` has no type coverage at
all while looking like it does. The stack-typescript rules say this explicitly —
"treat `npm run build` as bundling only; NEVER claim type-check or lint coverage
because the package declares neither."

These gates are Python because they must run in the Python lane, with no Node
toolchain and no `node_modules` present. They assert the surface is *declared*;
that it *passes* is verified by running it (see the exit gate, T12).
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FRONTEND = REPO_ROOT / "apps" / "actionwitness_service" / "frontend"
PACKAGE_JSON = FRONTEND / "package.json"
TSCONFIG = FRONTEND / "tsconfig.json"
VITEST_CONFIG = FRONTEND / "vitest.config.ts"
ESLINT_CONFIG = FRONTEND / "eslint.config.js"

REQUIRED_SCRIPTS = ("typecheck", "lint", "test", "build")

#: The store frontend does not yet declare `lint`. It is a separate application
#: with its own gates (§29.1), and adding an ESLint configuration to it is not
#: this milestone's work — but the quality bars apply to it too, so the gap is
#: named here rather than hidden by a shared constant that quietly excused it.
#: Recorded in the 006 deviations ledger for the repository-hardening milestone.
STORE_REQUIRED_SCRIPTS = ("typecheck", "test", "build")

# Mandated by the project's TypeScript rules, not by taste: the harness narrows
# untrusted HTTP and WebMCP payloads, where an absent property and an explicit
# `undefined` differ and every indexed lookup can miss.
REQUIRED_COMPILER_OPTIONS = {
    "strict": True,
    "exactOptionalPropertyTypes": True,
    "noUncheckedIndexedAccess": True,
    "noEmit": True,
}


def _package_json() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


def _tsconfig() -> dict:
    return json.loads(TSCONFIG.read_text(encoding="utf-8"))


@pytest.mark.architecture
def test_harness_frontend_declares_every_required_script() -> None:
    scripts = _package_json().get("scripts", {})
    missing = [name for name in REQUIRED_SCRIPTS if name not in scripts]
    assert missing == [], f"harness frontend package.json omits scripts: {missing}"


@pytest.mark.architecture
def test_typecheck_is_a_separate_command_from_build() -> None:
    """A build script that merely bundles must never be presented as type coverage."""
    scripts = _package_json()["scripts"]
    assert "tsc" in scripts["typecheck"], "typecheck must invoke tsc"
    assert "--noEmit" in scripts["typecheck"], "typecheck must run tsc --noEmit"
    assert "tsc" not in scripts["build"], (
        "build is bundling only; type-checking belongs to the typecheck script"
    )


@pytest.mark.architecture
def test_tests_run_without_a_watcher() -> None:
    """`npm test` is a gate, so it must exit rather than watch."""
    assert _package_json()["scripts"]["test"] == "vitest run"


@pytest.mark.architecture
@pytest.mark.parametrize("option,expected", sorted(REQUIRED_COMPILER_OPTIONS.items()))
def test_tsconfig_enables_required_strictness(option: str, expected: bool) -> None:
    compiler_options = _tsconfig()["compilerOptions"]
    assert compiler_options.get(option) == expected, (
        f"tsconfig compilerOptions.{option} must be {expected}"
    )


@pytest.mark.architecture
def test_vitest_config_exists_and_uses_jsdom() -> None:
    """jsdom supplies no WebMCP (spec §26.3); the environment must still be a DOM."""
    assert VITEST_CONFIG.is_file(), "expected a dedicated vitest.config.ts"
    text = VITEST_CONFIG.read_text(encoding="utf-8")
    assert 'environment: "jsdom"' in text


@pytest.mark.architecture
def test_exactly_one_webmcp_hook_is_pinned_per_adr_0002() -> None:
    """ADR-0002 is Accepted: use-webmcp-tool@0.2.0, exact (spec §32 LD-4).

    Exactly one candidate, pinned to the tested version with no range operator —
    a float would silently move the tree away from the build the spike measured.
    """
    manifest = _package_json()
    declared = {
        **manifest.get("dependencies", {}),
        **manifest.get("devDependencies", {}),
    }
    candidates = sorted({"use-webmcp-tool", "usewebmcp"} & declared.keys())
    assert candidates == ["use-webmcp-tool"], (
        f"ADR-0002 pins use-webmcp-tool alone; manifest declares {candidates}"
    )
    assert declared["use-webmcp-tool"] == "0.2.0", (
        f"pin drifted from the tested 0.2.0: {declared['use-webmcp-tool']!r}"
    )
    assert declared.get("webmcp-types") == "0.1.5", (
        f"webmcp-types must stay at the tested 0.1.5: {declared.get('webmcp-types')!r}"
    )


@pytest.mark.architecture
def test_frontend_lockfile_is_committed_with_the_pin() -> None:
    """The lockfile records the tested tree; ADR-0002 landed, so it must exist."""
    lockfile = FRONTEND / "package-lock.json"
    assert lockfile.exists(), (
        "package-lock.json missing: the ADR-0002 pin is only reproducible with "
        "the lockfile that recorded the tested tree"
    )
    assert '"use-webmcp-tool"' in lockfile.read_text(encoding="utf-8")


# --- the standalone storefront (003-T7) -------------------------------------

STORE_FRONTEND = REPO_ROOT / "examples" / "buggy_store" / "frontend"
STORE_PACKAGE_JSON = STORE_FRONTEND / "package.json"
STORE_TSCONFIG = STORE_FRONTEND / "tsconfig.json"


def _store_package_json() -> dict:
    return json.loads(STORE_PACKAGE_JSON.read_text(encoding="utf-8"))


@pytest.mark.architecture
def test_store_frontend_declares_every_required_script() -> None:
    """§29.1 builds the two frontends independently, so each carries its own gates."""
    scripts = _store_package_json().get("scripts", {})
    missing = [name for name in STORE_REQUIRED_SCRIPTS if name not in scripts]
    assert missing == [], f"store frontend package.json omits scripts: {missing}"
    assert scripts["test"] == "vitest run"
    assert "--noEmit" in scripts["typecheck"]
    assert "tsc" not in scripts["build"], "build is bundling only"


@pytest.mark.architecture
@pytest.mark.parametrize("option,expected", sorted(REQUIRED_COMPILER_OPTIONS.items()))
def test_store_tsconfig_enables_required_strictness(option: str, expected: bool) -> None:
    compiler_options = json.loads(STORE_TSCONFIG.read_text(encoding="utf-8"))["compilerOptions"]
    assert compiler_options.get(option) == expected, (
        f"store tsconfig compilerOptions.{option} must be {expected}"
    )


@pytest.mark.architecture
def test_the_storefront_declares_no_webmcp_dependency() -> None:
    """AC-09 and §26.7: the human path must not need the browser-tool surface.

    The storefront is what a person uses when WebMCP is absent, so a WebMCP
    package here would be evidence that the fallback had quietly acquired a
    dependency on the thing it is the fallback for.
    """
    manifest = _store_package_json()
    declared = {**manifest.get("dependencies", {}), **manifest.get("devDependencies", {})}
    webmcp = sorted(name for name in declared if "webmcp" in name.lower())
    assert webmcp == [], f"the standalone storefront declares WebMCP packages: {webmcp}"


def _shipped_sources() -> list:
    """Production sources only.

    Test files and the vitest setup are not in the bundle, and both mention the
    browser-tool API precisely to say it is absent - scanning them would flag the
    explanation as the violation.
    """
    return [
        path
        for path in (STORE_FRONTEND / "src").rglob("*.ts*")
        if "test" not in path.relative_to(STORE_FRONTEND / "src").parts
        and ".test." not in path.name
    ]


@pytest.mark.architecture
def test_the_storefront_never_references_the_browser_tool_api() -> None:
    """Checked in the shipped source as well as the manifest."""
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in _shipped_sources()
        if "modelContext" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"the storefront references WebMCP in: {offenders}"
    assert _shipped_sources(), "the source scan found no files, so it proves nothing"


@pytest.mark.architecture
def test_the_storefront_calls_only_the_stores_own_surface() -> None:
    """§15.5 reserves `/demo/api/v1` for this UI; it must call nothing else.

    Request paths are composed from one base constant rather than written out
    per call, so the check is that the base is the store's surface and that no
    literal reaches the harness's `/api/v1` instead.
    """
    source = (STORE_FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")
    assert 'const API = "/demo/api/v1/store"' in source, (
        "the storefront's base path is not the store's versioned surface"
    )
    for path in _shipped_sources():
        text = path.read_text(encoding="utf-8")
        assert '"/api/' not in text, f"{path.name} reaches the harness API"
        assert "actionwitness" not in text.lower(), f"{path.name} names the harness"


@pytest.mark.architecture
def test_the_store_frontend_lockfile_is_committed() -> None:
    """A frontend gate is only reproducible with the tree it was run against."""
    assert (STORE_FRONTEND / "package-lock.json").is_file()


@pytest.mark.architecture
def test_the_frontend_has_a_lint_configuration() -> None:
    """The quality bars require configured lint checks to pass.

    Until 006 this package declared no lint script and carried no ESLint
    configuration at all — cheap to overlook while the frontend was a few
    hundred lines, and not once it carries the Tier 1 UI. A `lint` script with
    nothing behind it would be worse than none, so the config file is asserted
    too.
    """
    assert ESLINT_CONFIG.is_file(), "the harness frontend has no ESLint configuration"
    scripts = _package_json()["scripts"]
    assert "eslint" in scripts["lint"], "the lint script must actually invoke eslint"


@pytest.mark.architecture
def test_lint_is_not_folded_into_another_command() -> None:
    """Separate gates stay separately reportable.

    A `test` script that also linted would make a lint failure look like a test
    failure, and a green `test` run would no longer mean what it says.
    """
    scripts = _package_json()["scripts"]
    for name in ("test", "build", "typecheck"):
        assert "eslint" not in scripts[name], f"{name} must not run eslint"
