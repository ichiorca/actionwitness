"""007-T4 — redaction substitution, the public schema, and hash order.

Three rules protecting one property: a case somebody was handed must be usable
and checkable without trusting whoever handed it over.

**Substitution** exists because `[REDACTED]` is not an email address. A replay
sending the marker fails argument validation at the target, and the case then
reproduces an argument error rather than the regression it was cut from.

**The schema is generated from the model.** Two hand-maintained definitions of
one format drift silently until somebody's case validates against one and fails
the other.

**The hash is verified on load, not merely computed on save.** It is the only
thing a reader who was handed the file can check.
"""

from __future__ import annotations

import json

import pytest
from actionwitness_core.contracts.models import OutcomeContract
from actionwitness_core.engine.enums import FailureClassification
from actionwitness_core.evals.models import (
    EmbeddedContract,
    EnvironmentExpectation,
    EvalExpectations,
    EvalFixture,
    EvalSource,
    EvalTarget,
    RegressionEvalCase,
    TrajectoryStep,
)
from actionwitness_core.evals.schema import (
    case_schema_path,
    load_case_schema,
    validate_case_document,
)
from actionwitness_core.evals.substitution import substitute_redacted
from actionwitness_core.kernel import ContractError
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.security.canonical import content_hash
from actionwitness_core.security.redaction import REDACTED

pytestmark = pytest.mark.unit


@pytest.fixture
def stored_case() -> dict:
    """One case as it is written to disk: the document plus its own hash."""
    document = OutcomeContract.model_validate(
        {
            "schema_version": "1.0",
            "name": "one-mug",
            "target_id": "buggy-store",
            "intent": "Add one mug and apply SAVE20 without creating an order.",
            "assertions": [
                {
                    "id": "total",
                    "path": "target.cart.total",
                    "operator": "equals",
                    "value": "20.00",
                    "severity": "critical",
                }
            ],
        }
    )
    case = RegressionEvalCase(
        id="eval_example_001",
        name="one-mug",
        source=EvalSource(
            run_id="run_example_001",
            implementation_version="0.1.0",
            overall_result=LayerResult.FAILED,
            critical_classifications=(FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH,),
        ),
        target=EvalTarget(
            type="managed_application", id="buggy-store", adapter="integrations.buggy_store"
        ),
        fixture=EvalFixture(content_hash=content_hash({"cart": {}}), target_state={"cart": {}}),
        trajectory=(
            TrajectoryStep(sequence=1, tool="apply_discount", arguments={"code": "SAVE20"}),
        ),
        contract=EmbeddedContract(
            content_hash=content_hash(document.canonical_document()), document=document
        ),
        expected=EvalExpectations(
            current=EnvironmentExpectation(overall_result=LayerResult.PASSED),
            reproduce_source=EnvironmentExpectation(
                overall_result=LayerResult.FAILED,
                required_classifications=(FailureClassification.FALSE_SUCCESS_OR_STATE_MISMATCH,),
            ),
        ),
    )
    return dict(case.as_stored_document())


# --- substitution ------------------------------------------------------------


def test_a_redacted_email_becomes_a_type_valid_address() -> None:
    """§24.2 step 5's own example. A replayed call carrying `[REDACTED]` fails
    validation at the target; one carrying an address does not."""
    # Arrange / Act
    result = substitute_redacted({"email": REDACTED})

    # Assert
    assert result == {"email": "eval-user@example.invalid"}


def test_substitutes_live_in_reserved_namespaces() -> None:
    """RFC 2606 reserves `.invalid` precisely so it can never resolve.

    A plausible-looking substitute would be worse than the marker: somebody
    would eventually believe it.
    """
    # Arrange / Act
    result = substitute_redacted({"email": REDACTED, "ip": REDACTED})

    # Assert
    assert str(result["email"]).endswith(".invalid")
    assert result["ip"] == "192.0.2.1"  # RFC 5737 documentation range


def test_substitution_is_deterministic() -> None:
    """Two generations of one case must produce identical bytes, including in
    the fields nobody looks at."""
    # Arrange / Act
    first = substitute_redacted({"customer_email": REDACTED})
    second = substitute_redacted({"customer_email": REDACTED})

    # Assert
    assert first == second


def test_a_field_name_matches_by_whole_word() -> None:
    """`customer_email` resolves like an email; `emailed_at` does not, because
    it is a timestamp and an address there would be nonsense."""
    # Arrange / Act
    matched = substitute_redacted({"customer_email": REDACTED})
    unmatched = substitute_redacted({"emailed_at": REDACTED})

    # Assert
    assert matched["customer_email"] == "eval-user@example.invalid"
    assert unmatched["emailed_at"] == "eval-value-000000"


def test_markers_nested_inside_arguments_are_reached() -> None:
    """A marker inside an argument object is still a marker the target will
    reject."""
    # Arrange / Act
    result = substitute_redacted({"customer": {"email": REDACTED}, "items": [REDACTED]})

    # Assert
    assert result["customer"]["email"] == "eval-user@example.invalid"
    assert result["items"] == ["eval-value-000000"]


def test_values_that_were_never_redacted_are_untouched() -> None:
    """Redaction only ever writes a string marker, so a number that survived was
    never sensitive — rewriting it would change the call being replayed."""
    # Arrange / Act
    result = substitute_redacted({"quantity": 1, "code": "SAVE20", "ok": True})

    # Assert
    assert result == {"quantity": 1, "code": "SAVE20", "ok": True}


def test_no_original_value_is_recoverable() -> None:
    """There is none to recover: the source was redacted before it was ever
    persisted (§20.3). A case is portable because it carries no secret, not
    because its secrets are well hidden."""
    # Arrange / Act
    result = substitute_redacted({"payment_token": REDACTED})

    # Assert — a fixed constant, derived from the field name and nothing else.
    assert result == {"payment_token": "eval-token-000000000000"}
    assert substitute_redacted({"payment_token": REDACTED}) == result


# --- the published schema ----------------------------------------------------


def test_the_schema_ships_inside_the_installed_package() -> None:
    """FR-082: "no private package, repository, schema, or credential".

    A schema that lived only in the git repository would make that false for
    anyone who installed rather than cloned.
    """
    # Arrange / Act / Assert
    assert case_schema_path().is_file()


def test_the_committed_schema_matches_the_model() -> None:
    """The drift gate.

    Two hand-maintained definitions of one format diverge silently, and the
    divergence surfaces as somebody's case validating against one and failing
    the other. Regenerating is the fix; editing the JSON by hand is the bug.
    """
    # Arrange — only the identity keys are added on export; everything else,
    # including the title and description Pydantic derives from the model, must
    # match exactly.
    committed = dict(load_case_schema())
    for key in ("$schema", "$id"):
        committed.pop(key, None)

    # Act
    generated = RegressionEvalCase.model_json_schema()

    # Assert
    assert json.loads(json.dumps(committed, sort_keys=True)) == json.loads(
        json.dumps(generated, sort_keys=True)
    )


def test_the_schema_declares_its_own_identity_and_version() -> None:
    """A published artifact a stranger validates against has to say what it is,
    or two versions become indistinguishable."""
    # Arrange / Act
    schema = load_case_schema()

    # Assert
    assert str(schema["$schema"]).startswith("https://json-schema.org/")
    assert "regression-eval-case/1.0" in str(schema["$id"])


# --- hash order --------------------------------------------------------------


def test_a_stored_case_validates_and_verifies_its_hash(stored_case: dict) -> None:
    """§24.2 step 11: the hash is computed last and describes the document."""
    # Arrange / Act
    case = validate_case_document(stored_case)

    # Assert
    assert case.content_hash() == stored_case["content_hash"]


def test_an_edited_case_is_refused(stored_case: dict) -> None:
    """The hash is the only thing a reader who was handed the file can check.

    Accepting a document whose hash no longer describes it would make every
    downstream "verified" claim hollow.
    """
    # Arrange
    tampered = dict(stored_case)
    tampered["name"] = "something-else"

    # Act / Assert
    with pytest.raises(ContractError, match="edited since it was generated"):
        validate_case_document(tampered)


def test_a_malformed_case_is_refused_as_an_invalid_definition(stored_case: dict) -> None:
    """An invalid definition is FR-088's exit code 2 territory, so it has to be
    distinguishable from a run that simply did not match its expectation."""
    # Arrange
    broken = dict(stored_case)
    broken.pop("trajectory")

    # Act / Assert
    with pytest.raises(ContractError, match="did not validate"):
        validate_case_document(broken)


def test_a_case_without_a_declared_hash_still_validates(stored_case: dict) -> None:
    """A freshly built document has no stored hash yet, and validating one is
    how the generator checks its own output before writing it."""
    # Arrange
    payload = {key: value for key, value in stored_case.items() if key != "content_hash"}

    # Act
    case = validate_case_document(payload)

    # Assert
    assert case.content_hash() == stored_case["content_hash"]
