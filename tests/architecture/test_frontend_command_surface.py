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

REQUIRED_SCRIPTS = ("typecheck", "test", "build")

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
def test_no_webmcp_hook_package_is_pinned_before_adr_0002() -> None:
    """Guards the ADR-0002 operator gate from being closed by a stray install.

    The hook package is selected by a human running the spike against the target
    browser build (spec §25.1, §33 q2). Until that record is Accepted, neither
    candidate may appear in the manifest. When ADR-0002 lands, this test is
    replaced by one asserting that exactly one of them is pinned.
    """
    manifest = _package_json()
    declared = {
        **manifest.get("dependencies", {}),
        **manifest.get("devDependencies", {}),
    }
    candidates = sorted({"use-webmcp-tool", "usewebmcp"} & declared.keys())
    assert candidates == [], (
        f"ADR-0002 is not Accepted yet, but the manifest already pins {candidates}"
    )


@pytest.mark.architecture
def test_frontend_lockfile_is_not_committed_before_adr_0002() -> None:
    """The lockfile is committed *after* the pin, so it records the tested tree."""
    lockfile = FRONTEND / "package-lock.json"
    assert not lockfile.exists(), (
        "package-lock.json exists before the ADR-0002 pin; it would lock in a "
        "hook choice the spike has not made"
    )
