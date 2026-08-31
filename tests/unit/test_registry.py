"""Name-registry gates (spec v1.9 §15.8, §16, §17.1; 001-preflight-baseline AC-6).

AC-6 asks for a machine-readable registry of stable API error codes and closed
state/event enums "shared by (future) handlers, UI, tests". Sharing only works if
the registry cannot fall behind the code, so these tests make three kinds of drift
fail:

* a member added to an enum without a description;
* an enum class added to the core module without being registered for export;
* the committed frontend artifact drifting from what the exporter produces.

They also enforce two safety invariants that are easy to get wrong later: a
conflict response must never be marked retryable, and the evaluation event names
must remain spelled exactly like the outcome event names they reuse.
"""

import inspect
import json
from enum import StrEnum

import pytest
from actionwitness_core.journeys import enums as core_enums
from actionwitness_core.journeys.enums import (
    CLOSED_ENUMS,
    REGISTERED_ENUM_CLASSES,
    EvaluationEventType,
    OutcomeEventType,
)
from actionwitness_service.api.errors import API_ERROR_DESCRIPTIONS, ApiErrorCode
from actionwitness_service.api.registry_export import REGISTRY_PATH, render_registry

# Responses that report a rejected intent. Repeating one cannot succeed, and
# marking one retryable would invite a client to re-send a mutation.
NON_RETRYABLE_STATUSES = {400, 403, 409, 422}


# --- closed enums -----------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,enum_cls,descriptions", REGISTERED_ENUM_CLASSES, ids=lambda arg: str(arg)[:40]
)
def test_every_enum_member_is_documented(
    name: str, enum_cls: type[StrEnum], descriptions: dict
) -> None:
    members = set(enum_cls)
    documented = set(descriptions)
    assert members - documented == set(), (
        f"{name}: members added without a description: "
        f"{sorted(m.value for m in members - documented)}"
    )
    assert documented - members == set(), (
        f"{name}: descriptions left behind for removed members: "
        f"{sorted(m.value for m in documented - members)}"
    )


@pytest.mark.unit
def test_every_enum_class_in_the_core_module_is_registered() -> None:
    """An unregistered enum is invisible to the exporter and to the UI."""
    defined = {
        name
        for name, obj in inspect.getmembers(core_enums, inspect.isclass)
        if issubclass(obj, StrEnum) and obj is not StrEnum and obj.__module__ == core_enums.__name__
    }
    registered = {enum_cls.__name__ for _, enum_cls, _ in REGISTERED_ENUM_CLASSES}
    assert defined == registered, (
        f"enum classes defined but not registered: {sorted(defined - registered)}; "
        f"registered but not defined: {sorted(registered - defined)}"
    )


@pytest.mark.unit
def test_closed_enums_and_registered_classes_agree() -> None:
    assert [closed.name for closed in CLOSED_ENUMS] == [
        name for name, _, _ in REGISTERED_ENUM_CLASSES
    ]


@pytest.mark.unit
@pytest.mark.parametrize("closed", CLOSED_ENUMS, ids=lambda c: c.name)
def test_enum_members_are_snake_case_and_described(closed) -> None:
    assert closed.members, f"{closed.name} has no members"
    assert closed.spec_ref.strip(), f"{closed.name} names no spec reference"
    for value, description in closed.members.items():
        assert value == value.lower(), f"{closed.name}.{value} is not lower-case"
        assert " " not in value, f"{closed.name}.{value} contains a space"
        assert description.strip(), f"{closed.name}.{value} has an empty description"


@pytest.mark.unit
def test_evaluation_events_reuse_outcome_event_spellings_exactly() -> None:
    """Spec §16.3 reuses outcome names; a divergent spelling breaks the shared engine.

    The two streams are separate tables but one policy engine reads both, so a
    name that drifts in only one of them fails at replay rather than here — which
    is exactly why it is asserted here.
    """
    outcome = {member.value for member in OutcomeEventType}
    evaluation = {member.value for member in EvaluationEventType}
    eval_only = {value for value in evaluation if value.startswith("eval_replay_")}

    shared = evaluation - eval_only
    assert shared <= outcome, (
        "evaluation event names that are not spelled like their outcome "
        f"counterparts: {sorted(shared - outcome)}"
    )
    assert eval_only, "expected the eval-run boundary events to be present"


# --- API error codes --------------------------------------------------------


@pytest.mark.unit
def test_every_error_code_is_documented() -> None:
    codes = set(ApiErrorCode)
    documented = set(API_ERROR_DESCRIPTIONS)
    assert codes - documented == set(), (
        f"error codes added without a spec entry: {sorted(c.value for c in codes - documented)}"
    )
    assert documented - codes == set(), (
        f"spec entries for removed codes: {sorted(c.value for c in documented - codes)}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("code", sorted(ApiErrorCode, key=lambda c: c.value), ids=lambda c: c.value)
def test_error_code_entry_is_well_formed(code: ApiErrorCode) -> None:
    entry = API_ERROR_DESCRIPTIONS[code]
    assert code.value == code.name, f"{code.value}: enum name and wire value must match"
    assert code.value == code.value.upper(), f"{code.value}: wire codes are UPPER_SNAKE_CASE"
    assert 400 <= entry.http_status <= 599, f"{code.value}: implausible status {entry.http_status}"
    assert entry.description.strip(), f"{code.value}: empty description"
    assert entry.spec_ref.strip(), f"{code.value}: no spec reference"
    assert entry.provenance in {"spec", "project"}, (
        f"{code.value}: provenance must say whether the spec names this code"
    )


@pytest.mark.unit
@pytest.mark.parametrize("code", sorted(ApiErrorCode, key=lambda c: c.value), ids=lambda c: c.value)
def test_rejected_intent_is_never_advertised_as_retryable(code: ApiErrorCode) -> None:
    """A 4xx says the request was refused; retrying it can only duplicate intent."""
    entry = API_ERROR_DESCRIPTIONS[code]
    if entry.http_status in NON_RETRYABLE_STATUSES:
        assert entry.retryable is False, (
            f"{code.value}: status {entry.http_status} must not be retryable"
        )


@pytest.mark.unit
def test_project_allocated_codes_are_visibly_distinguished() -> None:
    """An invented name must never be mistakable for one the specification fixed."""
    project_codes = sorted(
        code.value
        for code, entry in API_ERROR_DESCRIPTIONS.items()
        if entry.provenance == "project"
    )
    assert project_codes == ["WORKSPACE_LOCK_TIMEOUT"], (
        "project-allocated codes changed; each one needs a record explaining why "
        f"the spec's vocabulary was insufficient. Found: {project_codes}"
    )


# --- generated artifact -----------------------------------------------------


@pytest.mark.unit
def test_committed_registry_matches_the_exporter() -> None:
    assert REGISTRY_PATH.is_file(), (
        f"expected the generated registry at {REGISTRY_PATH}; run "
        "`uv run python -m actionwitness_service.api.registry_export`"
    )
    assert REGISTRY_PATH.read_text(encoding="utf-8") == render_registry(), (
        "the committed registry has drifted from its source. Regenerate with "
        "`uv run python -m actionwitness_service.api.registry_export`"
    )


@pytest.mark.unit
def test_exporter_is_deterministic() -> None:
    """Same inputs, byte-identical output — the registry reads no clock or environment."""
    assert render_registry() == render_registry()


@pytest.mark.unit
def test_generated_registry_covers_both_halves() -> None:
    document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert set(document["enums"]) == {closed.name for closed in CLOSED_ENUMS}
    assert set(document["error_codes"]) == {code.value for code in ApiErrorCode}


@pytest.mark.unit
def test_generated_registry_is_marked_as_generated() -> None:
    """A hand-edit would be silently overwritten, so the file has to say so."""
    document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    assert "do not edit" in document["//"].lower()
    assert "registry_export" in document["//"]
