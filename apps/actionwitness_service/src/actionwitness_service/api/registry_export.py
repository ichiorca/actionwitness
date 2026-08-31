"""Generate the shared name registry consumed by the React workspace.

One artifact, one source of truth. The registry is composed here — in the
composition root — from the core's target-neutral domain vocabulary and the
service's HTTP-bearing error codes, because neither layer may import the other's
concerns (constitution §1).

Regenerate with:

    uv run python -m actionwitness_service.api.registry_export

The result is committed, and `tests/unit/test_registry.py` fails if the committed
file drifts from what this module produces. Committing it means the frontend
build needs no Python step and a reviewer can see the vocabulary change in the
diff.

Scope note: Shopify pairing states (spec §16.5) are intentionally absent. They are
target-specific Tier 3 vocabulary, so putting them here would either pull a
commerce dependency into the composition root or a target into the core. They join
when M10 ships, through the integration that owns them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from actionwitness_core.registry import CLOSED_ENUMS

from actionwitness_service.api.errors import API_ERROR_DESCRIPTIONS
from actionwitness_service.config import SERVICE_CLOSED_ENUMS

#: Bump when the artifact's *shape* changes, not when a member is added.
REGISTRY_SCHEMA_VERSION = 1

REPO_ROOT = Path(__file__).resolve().parents[5]
REGISTRY_PATH = (
    REPO_ROOT
    / "apps"
    / "actionwitness_service"
    / "frontend"
    / "src"
    / "generated"
    / "registry.json"
)

_BANNER = (
    "GENERATED FILE — do not edit by hand. Regenerate with: "
    "uv run python -m actionwitness_service.api.registry_export"
)


def build_registry() -> dict[str, Any]:
    """Compose the registry document.

    Deterministic by construction: enum order comes from `CLOSED_ENUMS`, member
    order from each enum's declaration order, and error codes are sorted by code.
    Nothing here reads a clock, the environment, or the filesystem, so
    regenerating without a source change is always a no-op diff.
    """
    return {
        "//": _BANNER,
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "enums": {
            **{
                closed.name: {
                    "spec_ref": closed.spec_ref,
                    "members": dict(closed.members),
                }
                for closed in CLOSED_ENUMS
            },
            # Service-owned vocabulary. `module_status` is here rather than in the
            # core because module availability is a deployment concern, but the
            # capability bar renders it, so it must not fork either.
            **{
                name: {
                    "spec_ref": spec_ref,
                    "members": {str(member.value): text for member, text in descriptions.items()},
                }
                for name, spec_ref, descriptions in SERVICE_CLOSED_ENUMS
            },
        },
        "error_codes": {
            code.value: {
                "http_status": spec.http_status,
                "retryable": spec.retryable,
                "description": spec.description,
                "spec_ref": spec.spec_ref,
                "provenance": spec.provenance,
            }
            for code, spec in sorted(API_ERROR_DESCRIPTIONS.items(), key=lambda kv: kv[0].value)
        },
    }


def render_registry() -> str:
    """The exact text of the committed artifact, newline-terminated."""
    return json.dumps(build_registry(), indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def write_registry(path: Path | None = None) -> Path:
    destination = path or REGISTRY_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_registry(), encoding="utf-8", newline="\n")
    return destination


def main() -> None:
    written = write_registry()
    print(f"wrote {written}")


if __name__ == "__main__":
    main()
