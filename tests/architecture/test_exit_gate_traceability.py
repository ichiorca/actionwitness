"""Exit-gate traceability for the milestones that have shipped (004-T13).

Spec 002 carries its own roll-up in `test_core_only_install.py`, next to the
core-only isolation job that criterion 1 depends on. This module holds the map
for 003, 004, and 005, and, from here on, for each milestone as it lands.

The point of naming the covering test rather than counting tests: a milestone
can be declared finished with one criterion covered by nothing at all, and
"there are tests" is not the same claim as "this requirement is tested". Naming
them puts the omission in the diff instead of in a review nobody has time for.

These assertions are deliberately mechanical — the file exists, the function is
defined in it. That is enough to catch the failure this gate is for: a renamed
or deleted test that quietly stops covering a criterion. Whether the test is a
*good* test is not something a filename check can decide, which is why the
criteria are also spelled out here in the spec's own words.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: `specs/003-buggy-store-target/spec.md`, each criterion to its covering test.
EXIT_GATE_003: dict[str, tuple[str, str]] = {
    "1. Buggy Store runs and tests with all assurance packages absent": (
        "tests/architecture/test_store_only_install.py",
        "test_the_store_installs_runs_and_tests_in_a_clean_environment",
    ),
    "2a. normal retries return the first persisted result": (
        "tests/integration/test_store_cart_mutation.py",
        "test_an_identical_repeat_returns_the_first_persisted_result",
    ),
    "2b. conflicting request-ID reuse returns a non-retryable conflict": (
        "tests/integration/test_store_cart_mutation.py",
        "test_reusing_a_request_id_with_a_different_payload_is_refused",
    ),
    "3a. the discount fault reports success without changing state in pre_fix": (
        "tests/integration/test_store_failure_injection.py",
        "test_in_pre_fix_the_discount_reports_success_and_changes_nothing",
    ),
    "3b. the same call actually applies the discount in post_fix": (
        "tests/integration/test_store_failure_injection.py",
        "test_in_post_fix_the_same_call_actually_applies_the_discount",
    ),
    "4a. the adapter allowlists its published tools": (
        "tests/adapters/test_buggy_store_adapter.py",
        "test_a_tool_outside_the_allowlist_is_refused",
    ),
    "4b. the adapter publishes closed input schemas": (
        "tests/adapters/test_buggy_store_adapter.py",
        "test_every_tool_publishes_a_closed_input_schema",
    ),
    "4c. the adapter declares §13.4 effect metadata": (
        "tests/adapters/test_buggy_store_adapter.py",
        "test_the_effect_map_is_the_specs_table_verbatim",
    ),
    "4d. the store imports nothing from the assurance stack": (
        "tests/architecture/test_import_boundaries.py",
        "test_buggy_store_is_independent_of_the_assurance_stack",
    ),
    "5a. one target call is traced end to end": (
        "tests/integration/test_buggy_store_end_to_end.py",
        "test_one_target_call_is_traced_end_to_end",
    ),
    "5b. the authoritative observation is a separate read, not an echo": (
        "tests/integration/test_buggy_store_end_to_end.py",
        "test_the_observation_is_a_separate_read_not_an_echo",
    ),
}

#: `specs/004-workspace-persistence/spec.md`, in the spec's own words.
EXIT_GATE_004: dict[str, tuple[str, str]] = {
    "1. two independent clients cannot read or mutate one another's state even with known IDs": (
        "tests/integration/test_004_exit_gate.py",
        "test_gate_1_two_clients_get_separate_workspaces_and_separate_state",
    ),
    "2a. cross-workspace read attempts fail": (
        "tests/integration/test_workspace_authorization.py",
        "test_a_second_client_cannot_read_the_first_ones_resource",
    ),
    "2b. someone else's resource is indistinguishable from a missing one": (
        "tests/integration/test_workspace_authorization.py",
        "test_someone_elses_resource_is_indistinguishable_from_a_missing_one",
    ),
    "2c. cross-workspace mutation attempts fail": (
        "tests/integration/test_004_exit_gate.py",
        "test_gate_2_a_known_identifier_grants_no_mutation",
    ),
    "2d. cross-workspace reset attempts fail": (
        "tests/integration/test_004_exit_gate.py",
        "test_gate_2_a_cross_workspace_reset_attempt_changes_nothing",
    ),
    "3a. a resource-ceiling failure leaves no partial state": (
        "tests/integration/test_004_exit_gate.py",
        "test_gate_3_a_resource_ceiling_refusal_leaves_no_partial_state",
    ),
    "3b. a rate-limit failure leaves no partial state": (
        "tests/integration/test_004_exit_gate.py",
        "test_gate_3_a_rate_limit_refusal_leaves_no_partial_state",
    ),
    "3c. a lock failure leaves no partial state": (
        "tests/integration/test_004_exit_gate.py",
        "test_gate_3_a_lock_timeout_leaves_no_partial_state",
    ),
    "4. reset cancels nonterminal state and unresolved confirmations while "
    "retaining terminal artifacts and the selected contract": (
        "tests/integration/test_004_exit_gate.py",
        "test_gate_4_reset_cancels_in_flight_work_and_retains_the_rest",
    ),
    "5a. the service starts with Buggy Store disabled and reports it unavailable": (
        "tests/integration/test_004_exit_gate.py",
        "test_gate_5_the_service_starts_with_the_buggy_store_disabled",
    ),
    "5b. the service starts with the integration package absent entirely": (
        "tests/integration/test_adapter_registry.py",
        "test_the_service_starts_with_the_integration_uninstalled",
    ),
}

#: `specs/005-run-slice/spec.md`, in the spec's own words.
#:
#: Criterion 5 names five acceptance criteria rather than one behaviour, so it
#: splits furthest. AC-19 is carried by tests that predate this milestone — the
#: point of naming them here is that 005 *depends* on them, so deleting one has
#: to break this map rather than pass unnoticed.
EXIT_GATE_005: dict[str, tuple[str, str]] = {
    "1. API-level Journey A fails with `false_success_or_state_mismatch` in "
    "`pre_fix` and passes in `post_fix`": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_1_the_same_journey_fails_before_and_passes_after",
    ),
    "1b. the contradicted call reported success (the premise of criterion 1)": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_1_the_failing_tool_call_itself_reported_success",
    ),
    "2. the report shows trajectory pass, execution pass, business outcome fail, "
    "and model selection `not_evaluated`": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_2_the_report_separates_execution_from_outcome",
    ),
    "3a. a new target action loses cleanly to verification, with no partial snapshot": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_3_an_action_overlapping_verification_loses_cleanly",
    ),
    "3b. the rejection creates no finding and no evidence": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_3_a_rejected_late_action_leaves_no_evidence",
    ),
    "3c. the genuine `RUN_ALREADY_VERIFYING` overlap window": (
        "tests/integration/test_verification_gate.py",
        "test_an_invocation_overlapping_verification_loses_cleanly",
    ),
    "4. a mismatched rerun remains valid but returns `not_comparable` with the differing fields": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_4_a_mismatched_rerun_stays_valid_and_is_not_comparable",
    ),
    "5a. AC-03 — the human view and the independent observation agree": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_5_ac_03_the_human_view_and_the_observation_agree",
    ),
    "5b. AC-04 — execution passes while the business outcome fails": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_5_ac_04_execution_passes_while_the_outcome_fails",
    ),
    "5c. AC-11 — two visitors share no contract, run, event, or artifact": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_5_ac_11_two_visitors_share_nothing",
    ),
    "5d. AC-19 — the core installs and tests with every application package absent": (
        "tests/architecture/test_core_only_install.py",
        "test_the_core_installs_and_tests_in_a_clean_environment",
    ),
    "5e. AC-19 — a forbidden-import gate fails if the core reaches an application": (
        "tests/architecture/test_import_boundaries.py",
        "test_actionwitness_core_has_no_forbidden_imports",
    ),
    "5f. AC-19 — a non-commerce fake target completes a contract through the "
    "same core interfaces": (
        "tests/adapters/test_non_commerce_adapter.py",
        "test_a_non_commerce_path_is_evaluated_end_to_end",
    ),
    "5g. AC-20 — two immutable runs differ in one variable and the original "
    "critical classification resolves": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_5_ac_20_two_immutable_runs_differ_in_one_variable",
    ),
    "5h. AC-20 — a completed run's scenario is never relabelled": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_5_ac_20_a_completed_runs_scenario_never_changes",
    ),
    # AC-20's "activates the fault only for the pre_fix run" holds
    # behaviourally — criterion 1 is that difference — but is not recorded as a
    # field: `runs.fault_active` is never populated, and the 005 plan carries it
    # as an open operator decision. The covering test characterizes the gap and
    # fails once the field lands, which is the prompt to restore this entry to
    # an ordinary assertion. Named here so the gap is part of the map rather
    # than an omission from it.
    "5i. AC-20 — the fault is active only for `pre_fix` (KNOWN GAP: recorded "
    "behaviourally, not as `runs.fault_active`; awaiting an operator decision)": (
        "tests/integration/test_005_exit_gate.py",
        "test_gate_5_ac_20_the_fault_activation_field_is_not_yet_recorded",
    ),
}

MAPS = {"003": EXIT_GATE_003, "004": EXIT_GATE_004, "005": EXIT_GATE_005}


def _defines(path: Path, function: str) -> bool:
    """Whether `path` defines a top-level function named `function`.

    Parsed rather than string-searched: a mention in a docstring or a comment is
    not coverage, and `assert "def name" in source` would accept both.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
        for node in tree.body
    )


@pytest.mark.architecture
@pytest.mark.parametrize("milestone", sorted(MAPS))
def test_every_exit_gate_criterion_names_a_covering_test(milestone: str) -> None:
    """A criterion covered by nothing at all must show up here, not at review."""
    missing: list[str] = []
    for criterion, (relative, function) in MAPS[milestone].items():
        path = REPO_ROOT / relative
        if not path.is_file() or not _defines(path, function):
            missing.append(f"{criterion} -> {relative}::{function}")
    assert not missing, "exit-gate criteria without a covering test:\n" + "\n".join(missing)


@pytest.mark.architecture
@pytest.mark.parametrize("milestone", sorted(MAPS))
def test_each_map_covers_all_five_published_criteria(milestone: str) -> None:
    """Every milestone lists five criteria; these maps split some into parts.

    Checked by leading number rather than by count, so splitting criterion 5
    into nine entries stays honest while dropping criterion 3 entirely does not.
    """
    covered = {
        re.match(r"\d+", criterion.split(".")[0])[0]  # type: ignore[index]
        for criterion in MAPS[milestone]
    }
    assert covered == {"1", "2", "3", "4", "5"}


@pytest.mark.architecture
def test_no_criterion_is_covered_by_a_test_in_a_disabled_lane() -> None:
    """A gate satisfied by a skipped test is not a gate.

    Every covering test named above must live in a lane the default run
    executes, so `benchmarks`, `browser`, and `shopify` — which are opt-in — are
    not acceptable homes for an exit-gate criterion.
    """
    opt_in = ("tests/benchmarks/", "tests/browser/", "tests/shopify/")
    for milestone, gate in MAPS.items():
        for criterion, (relative, _) in gate.items():
            assert not relative.startswith(opt_in), (
                f"{milestone} criterion {criterion!r} is covered from an opt-in lane"
            )
