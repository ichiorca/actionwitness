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

#: Spec 006's exit gate, in the spec's own words.
#:
#: Criteria 1 and 2 are about a *real browser*, and no automated test in this
#: repository can discharge them: the browser lane is opt-in, and the gate below
#: forbids covering an exit-gate criterion from an opt-in lane. They are
#: operator-attested against `docs/tier-1-gate-checklist.md`, whose existence is
#: asserted — a deleted checklist would leave two criteria covered by nothing
#: while this map still looked complete.
#:
#: The server half of criterion 2 *is* automated and is named here: the human
#: path needs no tool, because the tools drive the endpoints the page does.
EXIT_GATE_006: dict[str, tuple[str, str]] = {
    "1. a compatible browser completes Journeys A and B through real WebMCP tools "
    "(OPERATOR-ATTESTED: docs/tier-1-gate-checklist.md)": (
        "tests/architecture/test_exit_gate_traceability.py",
        "test_the_operator_checklist_covers_the_browser_criteria",
    ),
    "2a. an unsupported browser completes the manual equivalent — the journey "
    "needs no browser agent": (
        "tests/integration/test_006_exit_gate.py",
        "test_gate_2_the_whole_journey_needs_no_browser_agent",
    ),
    "2b. …and shows setup guidance before anything is configured": (
        "tests/integration/test_006_exit_gate.py",
        "test_gate_2_guidance_is_present_before_anything_is_configured",
    ),
    "3a. banner, controls, status result, tool `next_action`, and action history "
    "share one action code at every transition": (
        "tests/integration/test_006_exit_gate.py",
        "test_gate_3_every_surface_names_one_action_code_through_journey_b",
    ),
    "3b. the handoff to the human names exactly one actor": (
        "tests/integration/test_006_exit_gate.py",
        "test_gate_3_the_handoff_to_the_human_names_exactly_one_actor",
    ),
    "4a. no order exists before approval": (
        "tests/integration/test_journey_b.py",
        "test_journey_b_creates_the_order_only_after_the_approval",
    ),
    "4b. the approval is consumed exactly once": (
        "tests/integration/test_journey_b.py",
        "test_one_approval_produces_exactly_one_order",
    ),
    "4c. denial, expiry, and cancellation create no order": (
        "tests/integration/test_006_exit_gate.py",
        "test_gate_4_a_refused_action_creates_no_order",
    ),
    "5a. StrictMode double-mount leaves exactly one registration": (
        "apps/actionwitness_service/frontend/src/webmcp/adapter.test.ts",
        "leaves exactly one live tool under StrictMode's double mount",
    ),
    "5b. registration cleanup on unmount": (
        "apps/actionwitness_service/frontend/src/webmcp/adapter.test.ts",
        "unregisters on unmount, so a closed panel leaves no callable tool",
    ),
    "5c. polling does not stop on an empty page": (
        "apps/actionwitness_service/frontend/src/state/useRunTimeline.test.ts",
        "keeps polling after an empty page while the run is live",
    ),
    "5d. error normalization returns isError rather than rejecting": (
        "apps/actionwitness_service/frontend/src/webmcp/adapter.test.ts",
        "normalizes a thrown handler into isError rather than rejecting",
    ),
    "5e. refresh rebuilds a pending confirmation": (
        "tests/integration/test_run_read_and_findings.py",
        "test_a_refreshing_client_can_rebuild_a_pending_dialog",
    ),
    "5f. accessibility — no preselected approval, focus trapped and restored": (
        "apps/actionwitness_service/frontend/src/components/panels.test.tsx",
        "preselects neither choice",
    ),
    "6a. AC-06 — an order exists only behind an approval consumed once": (
        "tests/integration/test_journey_b.py",
        "test_the_consent_policy_passes_on_the_approved_journey",
    ),
    "6b. AC-09 — the workspace stays usable without WebMCP": (
        "apps/actionwitness_service/frontend/src/components/panels.test.tsx",
        "reports a browser without WebMCP as a fact, not a failure",
    ),
    "6c. AC-21 — guidance names one actor and one next action at each handoff": (
        "tests/integration/test_journey_b.py",
        "test_the_guidance_names_the_human_then_hands_back",
    ),
    # AC-01 defers to M8 with AC-10, which BUILD_ORDER already schedules there;
    # AC-03/04/11/19/20 are 005's gate and are mapped above.
}

MAPS = {
    "003": EXIT_GATE_003,
    "004": EXIT_GATE_004,
    "005": EXIT_GATE_005,
    "006": EXIT_GATE_006,
}


def _defines(path: Path, function: str) -> bool:
    """Whether `path` defines the named test.

    Python is parsed rather than string-searched: a mention in a docstring or a
    comment is not coverage, and `assert "def name" in source` would accept
    both.

    TypeScript cannot be parsed here — the Python lane has no Node toolchain —
    so a vitest case is matched by its `it("…")` title, which is the name a
    reader would search for anyway. Weaker than the AST check, and worth being
    explicit about: what it still catches is a renamed or deleted test quietly
    ceasing to cover a criterion, which is the failure this gate exists for.
    """
    source = path.read_text(encoding="utf-8")
    if path.suffix in {".ts", ".tsx"}:
        return f'it("{function}"' in source
    tree = ast.parse(source)
    return any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
        for node in tree.body
    )


CHECKLIST = REPO_ROOT / "docs" / "tier-1-gate-checklist.md"


@pytest.mark.architecture
def test_the_operator_checklist_covers_the_browser_criteria() -> None:
    """006's criteria 1 and 2 are operator-attested, so the checklist *is* the
    coverage — and a deleted checklist would leave them covered by nothing while
    the map above still looked complete.

    Named as criterion 1's covering "test" deliberately: a map should point at
    something that fails when the coverage disappears, and this does.
    """
    assert CHECKLIST.is_file(), "the Tier 1 operator checklist is missing"
    text = CHECKLIST.read_text(encoding="utf-8")
    for required in ("Journeys A and B", "unsupported browser", "Attested by:", "no order"):
        assert required in text, f"the checklist no longer covers {required!r}"


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


#: How many criteria each spec publishes. Stated per milestone rather than
#: assumed: 003–005 list five, and 006 lists six because the Tier 1 gate is a
#: criterion in its own right. A shared constant would have to be loosened to
#: admit 006, and loosening it is exactly how a dropped criterion gets through.
PUBLISHED_CRITERIA: dict[str, int] = {"003": 5, "004": 5, "005": 5, "006": 6}


@pytest.mark.architecture
@pytest.mark.parametrize("milestone", sorted(MAPS))
def test_each_map_covers_every_published_criterion(milestone: str) -> None:
    """These maps split some criteria into parts; none may be missing entirely.

    Checked by leading number rather than by count, so splitting criterion 5
    into nine entries stays honest while dropping criterion 3 entirely does not.
    """
    expected = {str(number) for number in range(1, PUBLISHED_CRITERIA[milestone] + 1)}
    covered = {
        re.match(r"\d+", criterion.split(".")[0])[0]  # type: ignore[index]
        for criterion in MAPS[milestone]
    }
    assert covered == expected


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
