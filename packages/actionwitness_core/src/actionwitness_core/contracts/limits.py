"""Contract limits, transcribed from spec v1.9 §10.4.

One table, one place. These are bounds on untrusted input: a contract arrives
from an HTTP body or a WebMCP tool argument, and every unbounded field in it is
a way to make the harness do unbounded work (constitution §5, §21). Enforcing
them at the model boundary means the engine downstream can treat a validated
contract as already-bounded.

The canonical-size limit is measured over the *canonical serialization* rather
than the submitted bytes, because that is what gets hashed and stored, and
because whitespace in the submission is not the thing being bounded.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "MAX_ASSERTIONS",
    "MAX_CANONICAL_CONTRACT_BYTES",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_EXPECTED_TOOLS",
    "MAX_INTENT_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_OBSERVATION_PATH_LENGTH",
    "MAX_POLICIES",
    "MAX_REDACTION_PATHS",
]

#: Contract name, in characters (§10.4).
MAX_NAME_LENGTH: Final = 80

#: Contract description, in characters (§10.4).
MAX_DESCRIPTION_LENGTH: Final = 500

#: User intent, in characters (§10.4).
MAX_INTENT_LENGTH: Final = 1_000

#: One observation path, in characters (§10.4). Also bounds path depth: a path
#: this long cannot hold more than a hundred single-character segments.
MAX_OBSERVATION_PATH_LENGTH: Final = 200

#: Assertions per contract (§10.4).
MAX_ASSERTIONS: Final = 25

#: Policies per contract (§10.4).
MAX_POLICIES: Final = 10

#: Entries in `expected_tools.calls` (§10.4). Duplicates express multiplicity and
#: each entry counts, so this bounds required occurrences rather than tool names.
MAX_EXPECTED_TOOLS: Final = 20

#: Redaction path patterns per contract (§10.4).
MAX_REDACTION_PATHS: Final = 25

#: Canonical serialized contract, in bytes (§10.4: 32 KiB).
MAX_CANONICAL_CONTRACT_BYTES: Final = 32 * 1024
