"""Prebuilt Buggy Store contract templates (spec v1.9 §10.1, FR-020; 003-T12).

Every template is validated through the core's real parser rather than eyeballed,
because a template is only useful if it is a contract the engine will accept — a
seeded template that failed validation at arm time would break the journey it
exists to enable, and would do it in front of whoever was demonstrating it.

The templates are also checked against the *adapter's* published surface: every
tool a contract names must be one the adapter actually publishes, and every
protected tool it expects must carry a confirmation policy. That is §10.2's
target-scoped validation, and running it here means a contract cannot ship
naming a tool that does not exist.
"""

from __future__ import annotations

import pytest
from actionwitness_core.contracts.enums import PolicyType
from actionwitness_core.contracts.models import parse_contract
from actionwitness_core.ports.enums import SideEffectClass
from actionwitness_core.security.canonical import content_hash
from integrations.buggy_store.templates import (
    ALLOWED_DISCOUNT_CODES,
    FORM_PARAMETERS,
    TEMPLATES,
    template_for,
    template_ids,
)

from integrations.buggy_store import TARGET_ID, TOOL_SPECS

PROTECTED = {
    spec.name for spec in TOOL_SPECS if spec.side_effect is SideEffectClass.PROTECTED_MUTATING
}
PUBLISHED = {spec.name for spec in TOOL_SPECS}


# --- the seeded set (FR-020) ------------------------------------------------


@pytest.mark.contracts
def test_at_least_three_templates_are_published() -> None:
    """FR-020: "at least three Buggy Store integration contracts"."""
    assert len(TEMPLATES) >= 3
    assert template_ids() == (
        "one_mug_save20_no_checkout",
        "retry_safe_cart_update",
        "confirmed_checkout_only",
        # 013-T5. Appended rather than inserted: template order is publication
        # order and a reordering would change what a UI lists first for no reason.
        "one_mug_no_side_effects",
        # 014-T5/T6. Appended, for the same reason 013 appended.
        "one_mug_stable_surface",
    )


@pytest.mark.contracts
def test_template_identifiers_are_unique() -> None:
    identifiers = [template.template_id for template in TEMPLATES]
    assert len(set(identifiers)) == len(identifiers)


@pytest.mark.contracts
def test_every_template_says_what_it_demonstrates() -> None:
    """An operator picking a contract needs to know which defect it exposes."""
    for template in TEMPLATES:
        assert template.title.strip()
        assert template.summary.strip()
        assert template.demonstrates.strip()


@pytest.mark.contracts
def test_the_retry_template_demonstrates_correct_behaviour_not_a_defect() -> None:
    """BUILD_ORDER: "the retry contract exercises correct idempotent behavior".

    Its broken counterpart, `duplicate_on_retry`, is Tier 3 and has no injector
    in this build — so this contract must pass today rather than fail.
    """
    retry = template_for("retry_safe_cart_update")
    assert retry is not None
    assert retry.demonstrates == "none"


@pytest.mark.contracts
def test_no_template_claims_a_profile_this_build_cannot_produce() -> None:
    """A contract asserting a fault nothing can inject would look like coverage."""
    from buggy_store.failure_injection import IMPLEMENTED_PROFILES

    implemented = {profile.value for profile in IMPLEMENTED_PROFILES}
    for template in TEMPLATES:
        assert template.demonstrates in implemented, (
            f"{template.template_id} claims {template.demonstrates}, which has no injector"
        )


# --- every template is a valid contract (§10.2) -----------------------------


@pytest.mark.contracts
@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_every_template_parses_through_the_real_validator(template) -> None:
    """A seeded template that failed at arm time would break the demonstration."""
    contract = parse_contract(template.document)
    assert contract.target_id == TARGET_ID
    assert contract.schema_version == "1.0"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
@pytest.mark.contracts
def test_every_template_validates_against_the_adapters_published_tools(template) -> None:
    """§10.2: every expected tool must be one the selected adapter publishes."""
    parse_contract(template.document).validate_against_target(
        target_id=TARGET_ID, tool_names=PUBLISHED, protected_tools=PROTECTED
    )


@pytest.mark.contracts
@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_every_template_has_a_stable_content_hash(template) -> None:
    """FR-023 gives every saved contract an immutable ID, version, and hash."""
    first = parse_contract(template.document).content_hash()
    assert first == parse_contract(template.document).content_hash()
    assert first.startswith("sha256:")


@pytest.mark.contracts
def test_the_templates_have_distinct_content_hashes() -> None:
    hashes = {parse_contract(t.document).content_hash() for t in TEMPLATES}
    assert len(hashes) == len(TEMPLATES)


@pytest.mark.contracts
@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_every_template_round_trips_through_its_canonical_document(template) -> None:
    """A stored contract must be verifiable against its own hash."""
    contract = parse_contract(template.document)
    assert parse_contract(contract.canonical_document()) == contract


@pytest.mark.contracts
@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_a_template_expecting_a_protected_tool_requires_consent(template) -> None:
    """§10.2 rejects "destructive policy configurations that omit confirmation"."""
    contract = parse_contract(template.document)
    expected = set(contract.expected_tools.calls) if contract.expected_tools else set()
    for tool in expected & PROTECTED:
        assert tool in contract.confirmed_tools(), (
            f"{template.template_id} omits consent for {tool}"
        )


@pytest.mark.contracts
@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.template_id)
def test_every_template_is_written_in_its_own_canonical_form(template) -> None:
    """Otherwise the contract cannot generate an eval case (§24.2, FR-085).

    Seeding hashes the template document *as written*; §24.2 step 6 re-verifies a
    source contract by hashing the parsed contract's `canonical_document()`. A
    field left to its default — `allow_paths` on `no_undeclared_changes` is the
    one that bit — is absent from the first and present in the second, so the two
    hashes differ and case generation refuses the run with "the source contract
    does not match its stored hash".

    The failure surfaces far from its cause: the contract validates, the run
    arms, the journey succeeds, verification produces the right verdict, and only
    *generating a regression case* fails. Caught here instead.
    """
    document = dict(template.document)
    assert content_hash(document) == content_hash(parse_contract(document).canonical_document()), (
        f"{template.template_id} is not written in canonical form; a field left to "
        "its default will break eval-case generation for every run that uses it"
    )


@pytest.mark.contracts
def test_every_declared_parameter_is_one_the_form_can_carry() -> None:
    """FR-021 keeps the flat form to allowlisted scalars (012-T5).

    Tier 1 declared none — "expanding with no caller input" was the safest
    reading of "the template is trusted, the input is not" while nothing could
    submit one. Tier 3 ships the declarative form, so the templates now name
    what they accept, and the rule becomes the stronger one: a template may
    allowlist only scalars §25.2 actually puts on the form.

    A template naming anything else would allowlist a control that does not
    exist — the expansion would accept a parameter no caller can send, which
    reads as a supported option and is not one.
    """
    for template in TEMPLATES:
        assert set(template.parameters) <= set(FORM_PARAMETERS), (
            f"{template.template_id} allowlists a scalar the declarative form "
            f"does not carry: {sorted(set(template.parameters) - set(FORM_PARAMETERS))}"
        )


@pytest.mark.contracts
def test_a_template_allowlists_only_scalars_its_own_terms_use() -> None:
    """The allowlist has to match the document, in both directions.

    A template accepting `discount_code` with no discount term would let a
    person pick a code and create a contract that never checks one. A template
    that *has* a discount term but does not accept the scalar is the milder
    error, and still a surprise — the control is disabled for a contract that
    plainly involves a discount.
    """
    for template in TEMPLATES:
        # Named the code rather than said the word: a contract whose outcome
        # depends on a discount has to state which one, so its presence in the
        # document is the honest signal. Matching on "discount" anywhere would
        # fail the moment somebody wrote the word in a description.
        names_a_code = any(code in str(template.document) for code in ALLOWED_DISCOUNT_CODES)
        assert ("discount_code" in template.parameters) == names_a_code, (
            f"{template.template_id}'s discount allowlist disagrees with its terms"
        )


# --- the canonical example (§10.1) ------------------------------------------


@pytest.mark.contracts
def test_the_canonical_template_is_the_specs_example() -> None:
    """Transcribed from §10.1, including its intent and every assertion."""
    contract = parse_contract(template_for("one_mug_save20_no_checkout").document)

    assert contract.name == "one-mug-save20-no-checkout"
    assert [assertion.id for assertion in contract.assertions] == [
        "mug-quantity",
        "discounted-total",
        "order-not-created",
    ]
    assert contract.expected_tools is not None
    assert contract.expected_tools.ordered is False
    assert contract.expected_tools.calls == ("search_catalog", "update_cart", "apply_discount")
    assert {policy.type for policy in contract.policies} == {
        PolicyType.IDEMPOTENT_BY_REQUEST_ID,
        PolicyType.REQUIRES_CONFIRMATION,
    }


@pytest.mark.contracts
def test_the_canonical_template_asserts_the_total_the_fault_fails_to_produce() -> None:
    """This is the assertion Appendix B shows failing with actual "25.00"."""
    contract = parse_contract(template_for("one_mug_save20_no_checkout").document)
    discounted = next(a for a in contract.assertions if a.id == "discounted-total")
    assert str(discounted.path) == "target.cart.total"
    assert discounted.value == "20.00"


@pytest.mark.contracts
def test_the_canonical_template_redacts_the_specified_paths() -> None:
    """§10.1's redaction block, and §20.3's defaults on top of it."""
    contract = parse_contract(template_for("one_mug_save20_no_checkout").document)
    policy = contract.redaction_policy()
    assert [str(pattern) for pattern in policy.patterns] == ["**.email", "**.payment_token"]
    assert policy.apply_defaults is True


# --- the retry template -----------------------------------------------------


@pytest.mark.contracts
def test_the_retry_template_expects_the_repeat_as_part_of_the_journey() -> None:
    """§10.3: duplicate names express multiplicity, not an incidental extra call."""
    contract = parse_contract(template_for("retry_safe_cart_update").document)
    assert contract.expected_tools is not None
    assert contract.expected_tools.calls == ("update_cart", "update_cart")
    assert contract.expected_tools.ordered is True


@pytest.mark.contracts
def test_the_retry_template_asserts_the_cart_changed_only_once() -> None:
    """Two mugs, one line, one subtotal — a duplicated mutation breaks all three."""
    contract = parse_contract(template_for("retry_safe_cart_update").document)
    by_id = {assertion.id: assertion for assertion in contract.assertions}
    assert by_id["mug-quantity-after-retry"].value == 2
    assert by_id["one-cart-line"].value == 1
    assert by_id["subtotal-charged-once"].value == "50.00"


# --- the consent template ---------------------------------------------------


@pytest.mark.contracts
def test_the_consent_template_pairs_its_assertion_with_a_policy() -> None:
    """An assertion on `order.created` alone would pass an unapproved order."""
    contract = parse_contract(template_for("confirmed_checkout_only").document)
    assert any(a.id == "order-created" for a in contract.assertions)
    assert contract.confirmed_tools() == frozenset({"proceed_to_checkout"})


@pytest.mark.contracts
def test_an_unknown_template_id_returns_nothing() -> None:
    assert template_for("no_such_template") is None
