"""Shared pytest fixtures for the workspace test suite.

Scaffolding: lane fixture builders are added with the first Tier 1 vertical slice.
"""

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures"

CANONICALIZATION_VECTORS = FIXTURE_ROOT / "canonicalization" / "rfc8785_vectors.json"


@pytest.fixture(scope="session")
def canonicalization_vectors() -> dict:
    """The RFC 8785 vector corpus (ADR-0004).

    Session-scoped and read-only: the canonicalizer M1 implements is judged
    against exactly this corpus, so no test may mutate it.
    """
    return json.loads(CANONICALIZATION_VECTORS.read_text(encoding="utf-8"))
