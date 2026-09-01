"""The public JSON Schema for a regression eval case (§24.2 step 10, FR-082).

FR-082: "the repository shall define and version its own `RegressionEvalCase`
Pydantic and JSON Schema models. Creating or running an eval shall require no
private package, repository, schema, or credential."

Both halves of that sentence shape this module.

**The JSON Schema is generated from the Pydantic model, not written beside it.**
Two hand-maintained definitions of one format drift, and the drift is silent
until somebody's case validates against one and fails the other.
`test_the_committed_schema_matches_the_model` is the gate that keeps them equal.

**Validation is the model, and the schema is what a stranger validates with.**
The core has no JSON Schema *validator* and does not acquire one: adding a
dependency to re-check a document this package can already parse would be new
attack surface for no new information. So `validate_case_document` parses
through the model — the authority the schema was generated from — while the
committed `.json` file is what a consumer in another language uses. They cannot
disagree, because one produces the other.

The file ships **inside the installed package**, so `pip install
actionwitness-core` is enough to have it. A schema that lived only in the git
repository would make FR-082's "no private repository" claim false for anyone
who installed rather than cloned.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from actionwitness_core.evals.models import CASE_SCHEMA_VERSION, RegressionEvalCase
from actionwitness_core.kernel import ContractError, CoreErrorCode

__all__ = [
    "CASE_SCHEMA_FILENAME",
    "case_schema_path",
    "load_case_schema",
    "validate_case_document",
]

CASE_SCHEMA_FILENAME = "regression_eval_case_1_0.json"


def case_schema_path() -> Path:
    """Where the published schema lives inside the installed package."""
    # Beside the module rather than in a `schema/` subdirectory: a package
    # directory named the same as this module shadows it on some import
    # paths, and a data file is not worth that ambiguity.
    return Path(__file__).parent / CASE_SCHEMA_FILENAME


@lru_cache(maxsize=1)
def load_case_schema() -> Mapping[str, Any]:
    """The committed schema document, as data."""
    return json.loads(case_schema_path().read_text(encoding="utf-8"))


def validate_case_document(document: Mapping[str, Any]) -> RegressionEvalCase:
    """Validate a case document and return the parsed case.

    Accepts a stored document — one carrying its own top-level `content_hash` —
    and verifies that hash rather than ignoring it. A document whose hash does
    not describe its contents is not a case: the hash is the only thing a reader
    who was handed the file can check, and accepting a broken one would make
    every downstream "verified" claim hollow.
    """
    payload = {key: value for key, value in document.items() if key != "content_hash"}
    declared = document.get("content_hash")

    try:
        case = RegressionEvalCase.model_validate(payload)
    except Exception as invalid:
        raise ContractError(
            f"the eval case did not validate against schema {CASE_SCHEMA_VERSION}: {invalid}",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        ) from invalid

    if declared is not None and declared != case.content_hash():
        raise ContractError(
            "the eval case's content hash does not describe its contents; it has been "
            "edited since it was generated",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )
    return case
