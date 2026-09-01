"""What this importer is pinned to (ADR-0005, spec §25.3, FR-090).

ADR-0005 is the record; this module is the executable half of it. Both exist
because a pin written only in prose drifts the first time somebody regenerates
a fixture, and a pin written only in code has no reasoning attached to it when
somebody wants to move it.

**The pin is a refusal, not a preference.** FR-090 admits "an allowlisted
external-evaluator adapter and schema version" — so a report announcing a
schema outside `SUPPORTED_REPORTER_SCHEMAS` is refused rather than parsed
optimistically. An evaluator report is untrusted input from an experimental
upstream package; accepting an unknown version would mean guessing which fields
mean what, and every guess lands in a benchmark that claims to be evidence.

The architecture lane asserts these values against ADR-0005 itself, so moving
the pin without re-verifying the record fails the build.
"""

from __future__ import annotations

__all__ = [
    "NORMALIZER_VERSION",
    "REPORTER_COMMIT",
    "REPORTER_PACKAGE",
    "REPORTER_SCHEMA",
    "REPORTER_VERSION",
    "SUPPORTED_REPORTER_SCHEMAS",
    "is_supported_schema",
]

#: The upstream package, as it names itself.
REPORTER_PACKAGE = "webmcp-evals"

#: ADR-0005 decision 1: the released tag, not the studied commit and not HEAD.
REPORTER_VERSION = "0.0.4"

#: The commit that tag resolves to, recorded so provenance survives a retag.
REPORTER_COMMIT = "fe33c1b"

#: ADR-0005 decision 2: the label written into every normalized artifact, so a
#: reader of a benchmark can tell which report shape produced it.
REPORTER_SCHEMA = f"{REPORTER_PACKAGE}/{REPORTER_VERSION}"

#: Exactly one today. A frozenset rather than a bare constant because FR-090
#: says "allowlisted ... schema version" in the plural: adding a second
#: supported version must be an entry here, never a loosened comparison.
SUPPORTED_REPORTER_SCHEMAS = frozenset({REPORTER_SCHEMA})

#: ADR-0005 decision 3. Recorded beside the reporter schema in every normalized
#: artifact: the same report normalized by a later version of this code is a
#: different derived artifact, and FR-094 requires that to be visible rather
#: than silently recalculated over the old one.
NORMALIZER_VERSION = "1"


def is_supported_schema(schema: str) -> bool:
    """Whether this importer is allowed to read a report announcing `schema`.

    Exact membership, never a prefix or a version comparison. "Newer than the
    pin" is not the same as "compatible with the pin", and a `startswith`
    check would quietly admit `webmcp-evals/0.0.40`.
    """
    return schema in SUPPORTED_REPORTER_SCHEMAS
