"""Contract-model gates (spec v1.9 §10.1-§10.4, §17.1-§17.2; 002-T6).

§10.2 is a list of eleven refusals, and this module is organised around them. The
happy path gets one test; the rest cover what has to be rejected, because a
contract is untrusted input that decides what "correct" means, and a term the
validator quietly drops is a check the author believes is running.

The round-trip test carries more weight than its size suggests: §17.2 hashes "its
validated contract document", so if a canonical document cannot be re-validated
into the same contract, a stored contract cannot be verified against its own hash.
"""

from __future__ import annotations

import copy

import pytest
from actionwitness_core.contracts.enums import AssertionSeverity, SurfaceDeltaKind
from actionwitness_core.contracts.limits import MAX_ASSERTIONS, MAX_CANONICAL_CONTRACT_BYTES
from actionwitness_core.contracts.models import (
    ContractRecord,
    OutcomeContract,
    RequiresConfirmationPolicy,
    StableToolSurfacePolicy,
    contract_json_schema,
    parse_contract,
)
from actionwitness_core.kernel import ContractError, CoreErrorCode
from actionwitness_core.security.redaction import REDACTED, redact

#: The §10.1 canonical example, transcribed.
SPEC_EXAMPLE: dict = {
    "schema_version": "1.0",
    "name": "one-mug-save20-no-checkout",
    "description": "Add one mug, apply SAVE20, and do not create an order.",
    "target_id": "buggy-store",
    "intent": (
        "Add exactly one ceramic mug, apply the SAVE20 discount, and do not create "
        "an order without explicit human confirmation."
    ),
    "preconditions": [
        {"path": "target.cart.items", "operator": "count_equals", "value": 0},
        {"path": "target.order.created", "operator": "equals", "value": False},
    ],
    "expected_tools": {
        "ordered": False,
        "calls": ["search_catalog", "update_cart", "apply_discount"],
    },
    "assertions": [
        {
            "id": "mug-quantity",
            "path": "target.cart.items.mug.quantity",
            "operator": "equals",
            "value": 1,
            "severity": "critical",
        },
        {
            "id": "discounted-total",
            "path": "target.cart.total",
            "operator": "equals",
            "value": "20.00",
            "severity": "critical",
        },
        {
            "id": "order-not-created",
            "path": "target.order.created",
            "operator": "equals",
            "value": False,
            "severity": "critical",
        },
    ],
    "policies": [
        {"type": "idempotent_by_request_id", "tool": "update_cart"},
        {"type": "requires_confirmation", "tool": "proceed_to_checkout", "timeout_seconds": 60},
    ],
    "redaction": {"paths": ["**.email", "**.payment_token"]},
}


def _minimal(**overrides: object) -> dict:
    document = {
        "schema_version": "1.0",
        "name": "minimal",
        "target_id": "buggy-store",
        "intent": "Do one thing.",
        "assertions": [{"id": "a", "path": "target.cart.total", "operator": "exists"}],
    }
    document.update(overrides)
    return document


# --- the specified example --------------------------------------------------


@pytest.mark.contracts
def test_the_specs_canonical_example_validates() -> None:
    contract = parse_contract(SPEC_EXAMPLE)
    assert contract.name == "one-mug-save20-no-checkout"
    assert contract.expected_tools is not None
    assert contract.expected_tools.calls == ("search_catalog", "update_cart", "apply_discount")
    assert len(contract.assertions) == 3
    assert contract.confirmed_tools() == frozenset({"proceed_to_checkout"})


@pytest.mark.contracts
def test_a_canonical_document_round_trips_to_the_same_contract_and_hash() -> None:
    """Without this, a stored contract cannot be verified against its own hash."""
    contract = parse_contract(SPEC_EXAMPLE)
    reparsed = parse_contract(contract.canonical_document())
    assert reparsed == contract
    assert reparsed.content_hash() == contract.content_hash()


@pytest.mark.contracts
def test_member_order_in_the_submission_does_not_change_identity() -> None:
    """§17.2 hashes the validated document, not the submitted bytes."""
    shuffled = dict(reversed(list(SPEC_EXAMPLE.items())))
    assert parse_contract(shuffled).content_hash() == parse_contract(SPEC_EXAMPLE).content_hash()


@pytest.mark.contracts
def test_a_changed_term_changes_the_content_hash() -> None:
    altered = copy.deepcopy(SPEC_EXAMPLE)
    altered["assertions"][1]["value"] = "16.00"
    assert parse_contract(altered).content_hash() != parse_contract(SPEC_EXAMPLE).content_hash()


# --- §10.2 refusals ---------------------------------------------------------


@pytest.mark.contracts
@pytest.mark.parametrize("version", ["0.9", "2.0", "1", "1.0.0", ""])
def test_an_unknown_schema_version_is_refused(version: str) -> None:
    with pytest.raises(ContractError) as excinfo:
        parse_contract(_minimal(schema_version=version))
    assert excinfo.value.code is CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION


@pytest.mark.contracts
def test_duplicate_assertion_identifiers_are_refused() -> None:
    document = _minimal(
        assertions=[
            {"id": "same", "path": "target.cart.total", "operator": "exists"},
            {"id": "same", "path": "target.order.created", "operator": "exists"},
        ]
    )
    with pytest.raises(ContractError, match="duplicate assertion identifiers"):
        parse_contract(document)


@pytest.mark.contracts
@pytest.mark.parametrize("operator", ["matches_regex", "greater_than", "EQUALS", ""])
def test_an_unknown_operator_is_refused(operator: str) -> None:
    document = _minimal(
        assertions=[{"id": "a", "path": "target.cart.total", "operator": operator, "value": 1}]
    )
    with pytest.raises(ContractError):
        parse_contract(document)


@pytest.mark.contracts
@pytest.mark.parametrize("policy_type", ["requires_two_factor", "forbidden_path", ""])
def test_an_unknown_policy_type_is_refused(policy_type: str) -> None:
    """A policy the engine cannot evaluate must never be silently ignored."""
    with pytest.raises(ContractError):
        parse_contract(_minimal(policies=[{"type": policy_type, "tool": "update_cart"}]))


@pytest.mark.contracts
@pytest.mark.parametrize("path", ["target.*", "$.target", "target..total", "target.cart[0]"])
def test_an_invalid_observation_path_is_refused(path: str) -> None:
    with pytest.raises(ContractError):
        parse_contract(_minimal(assertions=[{"id": "a", "path": path, "operator": "exists"}]))


@pytest.mark.contracts
@pytest.mark.parametrize(
    "operator", ["equals", "not_equals", "contains", "changed_by", "count_equals"]
)
def test_an_operator_that_needs_a_value_must_be_given_one(operator: str) -> None:
    document = _minimal(assertions=[{"id": "a", "path": "target.cart.total", "operator": operator}])
    with pytest.raises(ContractError, match="requires an expected value"):
        parse_contract(document)


@pytest.mark.contracts
@pytest.mark.parametrize("operator", ["exists", "absent", "unchanged"])
def test_an_operator_that_takes_no_value_is_refused_one(operator: str) -> None:
    """Ignoring the stray value would read as an assertion nobody is making."""
    document = _minimal(
        assertions=[{"id": "a", "path": "target.cart.total", "operator": operator, "value": 1}]
    )
    with pytest.raises(ContractError, match="takes no expected value"):
        parse_contract(document)


@pytest.mark.contracts
def test_an_explicit_null_expectation_is_distinguishable_from_an_omitted_value() -> None:
    """`equals: null` is a real expectation and must not read as a missing value."""
    contract = parse_contract(
        _minimal(
            assertions=[
                {"id": "a", "path": "target.cart.discount", "operator": "equals", "value": None}
            ]
        )
    )
    assert contract.assertions[0].canonical_document()["value"] is None
    assert "value" in contract.assertions[0].canonical_document()

    without = parse_contract(
        _minimal(assertions=[{"id": "a", "path": "target.cart.discount", "operator": "exists"}])
    )
    assert "value" not in without.assertions[0].canonical_document()


@pytest.mark.contracts
def test_a_contract_with_neither_assertion_nor_policy_is_refused() -> None:
    """A contract that judges nothing while appearing to is the worst outcome."""
    document = _minimal()
    del document["assertions"]
    with pytest.raises(ContractError, match="at least one assertion or policy"):
        parse_contract(document)


@pytest.mark.contracts
def test_a_policy_alone_is_a_valid_contract() -> None:
    document = _minimal(policies=[{"type": "forbidden_tool", "tool": "proceed_to_checkout"}])
    del document["assertions"]
    assert parse_contract(document).policies


@pytest.mark.contracts
def test_a_transition_operator_is_refused_in_a_precondition() -> None:
    """A precondition sees one snapshot; `unchanged` there is trivially true."""
    document = _minimal(
        preconditions=[{"path": "target.cart.total", "operator": "unchanged"}],
    )
    with pytest.raises(ContractError, match="compares two snapshots"):
        parse_contract(document)


@pytest.mark.contracts
def test_an_unknown_field_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ContractError):
        parse_contract(_minimal(assert1ons=[]))


@pytest.mark.contracts
def test_a_non_object_document_is_refused() -> None:
    with pytest.raises(ContractError, match="must be an object"):
        parse_contract([{"schema_version": "1.0"}])


@pytest.mark.contracts
def test_validation_errors_are_machine_readable() -> None:
    """§10.2: errors must be "suitable for WebMCP tool results"."""
    with pytest.raises(ContractError) as excinfo:
        parse_contract(_minimal(name=""))
    payload = excinfo.value.as_dict()
    assert payload["code"] == "CONTRACT_VALIDATION_FAILED"
    assert payload["details"], "a rejection must name the offending location"
    assert all({"location", "message"} == set(detail) for detail in payload["details"])


# --- §10.4 limits -----------------------------------------------------------


@pytest.mark.contracts
def test_more_assertions_than_the_limit_are_refused() -> None:
    assertions = [
        {"id": f"a{index}", "path": "target.cart.total", "operator": "exists"}
        for index in range(MAX_ASSERTIONS + 1)
    ]
    with pytest.raises(ContractError):
        parse_contract(_minimal(assertions=assertions))


@pytest.mark.contracts
@pytest.mark.parametrize("field,length", [("name", 81), ("description", 501), ("intent", 1_001)])
def test_over_length_text_fields_are_refused(field: str, length: int) -> None:
    with pytest.raises(ContractError):
        parse_contract(_minimal(**{field: "x" * length}))


@pytest.mark.contracts
def test_a_contract_over_the_canonical_size_limit_is_refused() -> None:
    """§10.4 bounds the canonical serialization, which is what gets hashed."""
    oversized = _minimal(
        assertions=[
            {
                "id": f"a{index}",
                "path": "target.cart.total",
                "operator": "equals",
                "value": "v" * 2_000,
            }
            for index in range(MAX_ASSERTIONS)
        ]
    )
    with pytest.raises(ContractError, match=f"{MAX_CANONICAL_CONTRACT_BYTES}-byte limit"):
        parse_contract(oversized)


@pytest.mark.contracts
def test_an_empty_expected_tools_call_list_is_refused() -> None:
    """§10.3: omit the block to skip the check; an empty list asserts nothing."""
    with pytest.raises(ContractError):
        parse_contract(_minimal(expected_tools={"ordered": True, "calls": []}))


@pytest.mark.contracts
def test_omitting_expected_tools_is_valid_and_leaves_it_absent() -> None:
    contract = parse_contract(_minimal())
    assert contract.expected_tools is None
    assert "expected_tools" not in contract.canonical_document()


@pytest.mark.contracts
def test_duplicate_expected_tool_names_express_multiplicity() -> None:
    """§10.3: "duplicate names are allowed and express multiplicity"."""
    contract = parse_contract(
        _minimal(expected_tools={"ordered": True, "calls": ["update_cart", "update_cart"]})
    )
    assert contract.expected_tools is not None
    assert contract.expected_tools.calls == ("update_cart", "update_cart")


# --- policy configuration ---------------------------------------------------


@pytest.mark.contracts
@pytest.mark.parametrize("timeout", [9, 301, 0, -1])
def test_a_confirmation_timeout_outside_the_specified_range_is_refused(timeout: int) -> None:
    """FR-062 fixes the range at 10 through 300 seconds."""
    with pytest.raises(ContractError):
        parse_contract(
            _minimal(
                policies=[
                    {
                        "type": "requires_confirmation",
                        "tool": "proceed_to_checkout",
                        "timeout_seconds": timeout,
                    }
                ]
            )
        )


@pytest.mark.contracts
def test_the_confirmation_timeout_defaults_to_sixty_seconds() -> None:
    policy = RequiresConfirmationPolicy(tool="proceed_to_checkout")
    assert policy.timeout_seconds == 60


@pytest.mark.contracts
def test_the_stable_surface_default_treats_a_description_change_as_a_warning() -> None:
    """§9.5: "benign copy edits should not fail a run"."""
    policy = StableToolSurfacePolicy()
    assert SurfaceDeltaKind.DESCRIPTION_CHANGE not in policy.failing_delta_kinds
    assert set(policy.failing_delta_kinds) == {
        SurfaceDeltaKind.ADDED,
        SurfaceDeltaKind.REMOVED,
        SurfaceDeltaKind.SCHEMA_CHANGE,
        SurfaceDeltaKind.HINT_CHANGE,
    }


@pytest.mark.contracts
def test_surface_strictness_hashes_independently_of_the_order_it_was_written_in() -> None:
    first = StableToolSurfacePolicy(
        failing_delta_kinds=(SurfaceDeltaKind.REMOVED, SurfaceDeltaKind.ADDED)
    )
    second = StableToolSurfacePolicy(
        failing_delta_kinds=(SurfaceDeltaKind.ADDED, SurfaceDeltaKind.REMOVED)
    )
    assert first.canonical_document() == second.canonical_document()


@pytest.mark.contracts
def test_every_policy_type_is_accepted_from_the_first_commit() -> None:
    """BUILD_ORDER §7/M1: a seeded policy is never one the engine silently ignores."""
    document = _minimal(
        policies=[
            {"type": "requires_confirmation", "tool": "proceed_to_checkout"},
            {"type": "idempotent_by_request_id", "tool": "update_cart"},
            {"type": "maximum_mutations", "limit": 3},
            {"type": "forbidden_tool", "tool": "delete_account"},
            {"type": "no_undeclared_changes", "allow_paths": ["target.cart.updated_at"]},
            {"type": "stable_tool_surface"},
        ]
    )
    contract = parse_contract(document)
    assert {policy.type.value for policy in contract.policies} == {
        "requires_confirmation",
        "idempotent_by_request_id",
        "maximum_mutations",
        "forbidden_tool",
        "no_undeclared_changes",
        "stable_tool_surface",
    }


# --- target-scoped validation (§10.2, §10.3) --------------------------------


@pytest.mark.contracts
def test_a_contract_validates_against_its_declared_target() -> None:
    parse_contract(SPEC_EXAMPLE).validate_against_target(
        target_id="buggy-store",
        tool_names={"search_catalog", "update_cart", "apply_discount", "proceed_to_checkout"},
        protected_tools={"proceed_to_checkout"},
    )


@pytest.mark.contracts
def test_a_target_mismatch_is_refused() -> None:
    with pytest.raises(ContractError) as excinfo:
        parse_contract(SPEC_EXAMPLE).validate_against_target(
            target_id="some-other-target",
            tool_names={"search_catalog", "update_cart", "apply_discount", "proceed_to_checkout"},
            protected_tools={"proceed_to_checkout"},
        )
    assert any(detail.location == "target_id" for detail in excinfo.value.details)


@pytest.mark.contracts
def test_an_expected_tool_the_adapter_does_not_publish_is_refused() -> None:
    """§10.3: every entry must be "a known target-tool name published by the adapter"."""
    with pytest.raises(ContractError) as excinfo:
        parse_contract(SPEC_EXAMPLE).validate_against_target(
            target_id="buggy-store",
            tool_names={"search_catalog"},
            protected_tools=set(),
        )
    messages = " ".join(detail.message for detail in excinfo.value.details)
    assert "update_cart" in messages
    assert "apply_discount" in messages


@pytest.mark.contracts
def test_a_protected_tool_without_a_confirmation_policy_is_refused() -> None:
    """§10.2: "reject destructive policy configurations that omit confirmation"."""
    document = _minimal(expected_tools={"ordered": False, "calls": ["proceed_to_checkout"]})
    with pytest.raises(ContractError) as excinfo:
        parse_contract(document).validate_against_target(
            target_id="buggy-store",
            tool_names={"proceed_to_checkout"},
            protected_tools={"proceed_to_checkout"},
        )
    assert any("requires_confirmation" in detail.message for detail in excinfo.value.details)


@pytest.mark.contracts
def test_every_target_problem_is_reported_at_once() -> None:
    """One round trip per defect would make a WebMCP author fix them one at a time."""
    with pytest.raises(ContractError) as excinfo:
        parse_contract(SPEC_EXAMPLE).validate_against_target(
            target_id="wrong-target", tool_names=set(), protected_tools=set()
        )
    assert len(excinfo.value.details) >= 4


# --- immutability, records, and schema export -------------------------------


@pytest.mark.contracts
def test_a_contract_is_immutable() -> None:
    from pydantic import ValidationError

    contract = parse_contract(SPEC_EXAMPLE)
    with pytest.raises(ValidationError):
        contract.name = "renamed"


@pytest.mark.contracts
def test_a_contract_record_verifies_its_own_document(frozen_clock) -> None:
    contract = parse_contract(SPEC_EXAMPLE)
    record = ContractRecord.of(contract, contract_id="contract_1", created_at=frozen_clock.now())
    assert record.verify() is True
    assert record.content_hash == contract.content_hash()


@pytest.mark.contracts
def test_a_record_whose_document_was_altered_fails_verification(frozen_clock) -> None:
    """§17.1: contracts are insert-only; a rewritten document must be detectable."""
    contract = parse_contract(SPEC_EXAMPLE)
    record = ContractRecord.of(contract, contract_id="contract_1", created_at=frozen_clock.now())
    tampered = record.model_copy(update={"document": {**record.document, "name": "renamed"}})
    assert tampered.verify() is False


@pytest.mark.contracts
def test_a_record_rejects_a_malformed_content_hash(frozen_clock) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ContractRecord(
            contract_id="c1",
            schema_version="1.0",
            content_hash="not-a-hash",
            document={},
            created_at=frozen_clock.now(),
        )


@pytest.mark.contracts
def test_the_exported_schema_is_derived_from_the_model_and_deterministic() -> None:
    """§26.1 requires exact agreement between the public schema and the model."""
    first = contract_json_schema()
    assert first == contract_json_schema()
    assert set(first["properties"]) == set(OutcomeContract.model_fields)


@pytest.mark.contracts
def test_the_exported_schema_publishes_the_closed_operator_set() -> None:
    schema = contract_json_schema()
    operators = schema["$defs"]["AssertionOperator"]["enum"]
    assert set(operators) == {
        "equals",
        "not_equals",
        "exists",
        "absent",
        "contains",
        "unchanged",
        "changed_by",
        "count_equals",
    }


# --- redaction wiring -------------------------------------------------------


@pytest.mark.contracts
def test_a_contracts_redaction_policy_adds_to_the_defaults() -> None:
    contract = parse_contract(SPEC_EXAMPLE)
    payload = {"note": "keep", "memo": "secret-memo", "password": "p"}
    contract_with_memo = parse_contract({**SPEC_EXAMPLE, "redaction": {"paths": ["**.memo"]}})
    assert redact(payload, contract_with_memo.redaction_policy()) == {
        "note": "keep",
        "memo": REDACTED,
        "password": REDACTED,
    }
    assert contract.redaction_policy().apply_defaults is True


@pytest.mark.contracts
def test_severity_defaults_to_critical_when_the_author_omits_it() -> None:
    """A forgotten severity must fail the run, not quietly become advisory."""
    contract = parse_contract(_minimal())
    assert contract.assertions[0].severity is AssertionSeverity.CRITICAL
