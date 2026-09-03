"""Release-artifact hygiene (constitution §8; 009-T2/T7).

Constitution §8: "Release artifacts contain no secrets, local paths, private
fixtures, generated build debris, or undeclared dependencies."

These gates are static — they read `Dockerfile` and `.dockerignore` rather than
inspecting a built image, because the Python lane has no Docker daemon and a gate
that only runs where Docker exists is a gate that stops running. The CI workflow
builds the image and greps its filesystem for the same exclusions
(`.github/workflows/ci.yml`, the `image` job); this file is what fails in an
ordinary `uv run pytest -q` when someone deletes a line from `.dockerignore`.

The paths below are not hypothetical. `docs/BUILD_ORDER.md` and the functional
specification are present in an operator's working tree and absent from git, so
`.gitignore` does not protect the image from them — a build context is assembled
from the working directory, not from the index. The same is true of the whole
local agent rig.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
ENTRYPOINT = REPO_ROOT / "scripts" / "docker-entrypoint.sh"

#: Untracked-but-present planning inputs. Shipping one would put private material
#: in a public artifact, which is the §8 failure with the largest blast radius.
PLANNING_INPUTS = (
    "docs/BUILD_ORDER.md",
    "docs/actionwitness-functional-spec.md",
    "docs/actionwitness-feature-recommendations.md",
    "docs/actionwitness-spec-v2.0-review.md",
    "docs/actionwitness-top3-features-round2.md",
)

#: The machine-local development rig (`.gitignore`'s rig section). None of it is
#: part of the deliverable and some of it carries local audit state.
LOCAL_RIG = (".cavesson/", ".gateweave/", ".claude/", "agents/", "skills/", "hooks/", "evals/")

#: Secrets and local state.
SECRETS_AND_STATE = (".env", "*.sqlite3", "artifacts/")

#: Build debris.
DEBRIS = ("**/node_modules/", "**/dist/", "**/__pycache__/", ".venv/", ".git/")


def _dockerignore() -> set[str]:
    return {
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


@pytest.mark.architecture
def test_the_build_context_is_filtered_at_all() -> None:
    """A missing `.dockerignore` is the whole failure, so it is asserted first."""
    assert DOCKERIGNORE.is_file(), (
        "without .dockerignore the build context is the working tree, planning "
        "documents and local rig included"
    )


@pytest.mark.architecture
@pytest.mark.parametrize("excluded", [*PLANNING_INPUTS, *LOCAL_RIG, *SECRETS_AND_STATE, *DEBRIS])
def test_the_build_context_excludes_what_must_never_ship(excluded: str) -> None:
    assert excluded in _dockerignore(), f".dockerignore no longer excludes {excluded!r}"


@pytest.mark.architecture
def test_the_lockfiles_survive_the_wildcard_exclusion() -> None:
    """`*.lock` excludes the rig's sentinels and would take `uv.lock` with it.

    `uv sync --frozen` in the image fails closed without the lockfile, so this
    would be caught — but it would be caught as a confusing resolution error in a
    Docker build rather than as the exclusion mistake it is.
    """
    entries = _dockerignore()
    assert "!uv.lock" in entries
    assert "!**/package-lock.json" in entries


@pytest.mark.architecture
def test_the_dockerfile_copies_narrowly_rather_than_the_whole_context() -> None:
    """Defence in depth: `.dockerignore` is the first lock, not the only one.

    A `COPY . .` puts the entire filtered context in a layer, so every future
    addition to the working tree is shipped by default and excluded only by
    someone remembering to update `.dockerignore`. Naming each source directory
    inverts that: a new directory is absent until someone adds it.
    """
    body = DOCKERFILE.read_text(encoding="utf-8")
    offenders = re.findall(r"^\s*COPY\s+\.\s+\S+\s*$", body, re.MULTILINE)
    assert offenders == [], f"the Dockerfile copies the whole context: {offenders}"


@pytest.mark.architecture
def test_the_development_dependency_group_is_not_installed_into_the_image() -> None:
    """pytest, ruff, and the rest are not runtime dependencies (§8)."""
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert "--no-dev" in body, "the image must be built without the dev dependency group"


@pytest.mark.architecture
def test_the_image_resolves_from_the_lockfile_rather_than_re_resolving() -> None:
    """`--frozen` is what makes the artifact the tree the suite ran against."""
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert "--frozen" in body


@pytest.mark.architecture
def test_the_spike_page_is_not_shipped() -> None:
    """ADR-0002's decision harness registers WebMCP tools of its own.

    `vite.config.ts` keeps it out of the product surface by making it a separate
    entry point; shipping the page would put it straight back, and it would be
    reachable at `/spike.html` on the public deployment.
    """
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert "rm -f dist/spike.html" in body, "the image still ships the ADR-0002 spike page"


@pytest.mark.architecture
def test_the_harness_venv_ships_every_integration_its_mounted_routes_import() -> None:
    """A lazy import on an always-mounted route is a runtime dependency.

    The benchmark routes import `integrations.google_evals` unconditionally —
    before any source-kind branch — so an image without that distribution turns
    every `POST /benchmarks` into a 500, in the image and nowhere else: the uv
    workspace on a developer machine and in CI always has every member
    installed, which is exactly why this failed first on the deployed instance
    (2026-09-03, criterion-4 attestation run).

    `integrations/shopify` is listed too, though its imports sit behind the
    `EXTERNAL_AUDIT_ENABLED` gate (which refuses with `AUDIT_NOT_AUTHORIZED`
    before any import runs — verified against the deployed instance): the gate
    protects the *disabled* deployment, but an operator who enables the module
    on the shipped image is one environment variable away from the same
    image-only 500, so the distribution ships.
    """
    body = DOCKERFILE.read_text(encoding="utf-8")
    for member, package in (
        ("./integrations/buggy_store", "actionwitness-integration-buggy-store"),
        ("./integrations/google_evals", "actionwitness-integration-google-evals"),
        ("./integrations/shopify", "actionwitness-integration-shopify"),
    ):
        assert member in body, f"{member} is not installed into the harness venv"
        assert f"--package {package}" in body, f"{package}'s pinned deps are not exported"


# --- the single-worker rule (§29.1, ADR-0003) --------------------------------


@pytest.mark.architecture
def test_the_entrypoint_pins_one_uvicorn_worker() -> None:
    """Load-bearing, not a default.

    ADR-0003's `BEGIN IMMEDIATE` + busy-timeout model assumes one writer process.
    A second worker is a second writer against one SQLite file, and the lock model
    has no answer for it — so "tuning up" workers is a correctness change wearing
    the costume of a performance change.
    """
    body = ENTRYPOINT.read_text(encoding="utf-8")
    assert "--workers 1" in body, "the entrypoint no longer pins a single Uvicorn worker"


@pytest.mark.architecture
def test_the_store_is_not_published_outside_the_container() -> None:
    """The only route in is the harness's own proxy, which the origin policy guards."""
    body = ENTRYPOINT.read_text(encoding="utf-8")
    assert "0.0.0.0" not in body.split("exec")[0], (
        "the store process must stay on loopback; only the harness binds a public interface"
    )


@pytest.mark.architecture
def test_the_two_applications_get_separate_virtualenvs() -> None:
    """§26.7's isolation, made a property of the artifact (ADR-0006).

    One shared site-packages would put `actionwitness_core` an `import` away from
    the demo target, and the boundary would hold only because nobody had written
    that import yet.
    """
    body = DOCKERFILE.read_text(encoding="utf-8")
    assert "uv venv /opt/harness" in body
    assert "uv venv /opt/store" in body
    assert "--package buggy-store" in body, (
        "the store's environment must be resolved from its own manifest alone"
    )
