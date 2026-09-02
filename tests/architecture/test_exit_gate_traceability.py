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

#: 007's exit gate, each criterion to its covering test.
EXIT_GATE_007: dict[str, tuple[str, str]] = {
    "1. generation is idempotent, redacted, schema-valid and source-preserving; "
    "a proposal run is refused by name": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_1_generation_is_idempotent_redacted_valid_and_source_preserving",
    ),
    "2. `reproduce_source` recreates the failure and the exact critical set, and exits 0": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_2_reproduce_source_matches_by_set_equality_and_exits_zero",
    ),
    "3. an unrelated or additional critical classification exits 1": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_3_an_unrelated_or_additional_classification_exits_one",
    ),
    "4. `current` exits 0; an invalid definition or harness execution exits 2": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_4_current_exits_zero_and_an_invalid_case_or_harness_exits_two",
    ),
    "5a. §24.3a — the recorded surface replays into the policy that needs it": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_5_recorded_surface_replays_into_the_policy_that_needs_it",
    ),
    "5b. …and no recorded surface never reads as satisfied (§16.1)": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_5_a_case_with_no_recorded_surface_never_reads_as_satisfied",
    ),
    "5c. an unevaluable policy is named and excluded from both sets; the selected "
    "profile appears in the report": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_5_an_unevaluable_policy_is_named_and_excluded_from_both_sets",
    ),
    "6. AC-08, AC-12 and AC-15 through both API and CLI": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_6_ac_08_ac_12_and_ac_15_hold_through_api_and_cli",
    ),
    "7. the evals-lane tripwire is replaced by real §24 coverage": (
        "tests/integration/test_007_exit_gate.py",
        "test_gate_7_the_evals_lane_carries_real_coverage_rather_than_a_tripwire",
    ),
}

#: 009's exit gate, in the spec's own words.
#:
#: Two of its six criteria are operator gates and cannot be discharged from a
#: terminal: criterion 2 needs a Render account, a real deploy, and a rehearsed
#: rollback; criteria 3 and 4 need a human driving a browser against the deployed
#: URL. They follow 006's precedent — operator-attested against a checklist whose
#: existence is asserted, so a deleted checklist leaves them covered by nothing
#: rather than leaving this map still looking complete.
#:
#: Everything that *can* be automated is. In particular criterion 6 is not an
#: inspection of the built image (the Python lane has no Docker daemon) but a set
#: of gates over the `Dockerfile` and `.dockerignore` that decide what the image
#: can contain; the CI `image` job greps the built filesystem for the same things.
EXIT_GATE_009: dict[str, tuple[str, str]] = {
    "1. a fresh checkout follows the README successfully (AC-10)": (
        "tests/architecture/test_readme_commands.py",
        "test_every_documented_npm_script_is_declared_by_a_frontend",
    ),
    "1b. …and every documented Python command and test path exists": (
        "tests/architecture/test_readme_commands.py",
        "test_every_documented_python_script_exists",
    ),
    "2. the image tested in staging is the image deployed; rollback rehearsed and "
    "the previous deploy retained (OPERATOR-ATTESTED: docs/release-checklist.md)": (
        "tests/architecture/test_exit_gate_traceability.py",
        "test_the_release_checklist_covers_the_operator_criteria",
    ),
    "3. the live URL loads without credentials and reports WebMCP support status "
    "(AC-01) (OPERATOR-ATTESTED: docs/release-checklist.md)": (
        "tests/architecture/test_exit_gate_traceability.py",
        "test_the_release_checklist_covers_the_operator_criteria",
    ),
    "3b. …the credential-free path is what the code does, not only what the checklist claims": (
        "tests/integration/test_cut_feature_hygiene.py",
        "test_the_module_surface_publishes_no_credential",
    ),
    "4. the live URL passes Tier 1 and Tier 2 manual acceptance with no "
    "third-party credential (OPERATOR-ATTESTED: docs/release-checklist.md)": (
        "tests/architecture/test_exit_gate_traceability.py",
        "test_the_release_checklist_covers_the_operator_criteria",
    ),
    "5a. health and readiness signals are visible": (
        "tests/integration/test_production_security_posture.py",
        "test_health_reports_the_configured_origin_and_no_secret",
    ),
    "5b. database changes are forward-compatible — a redeploy against existing data is a no-op": (
        "tests/integration/test_harness_migrations.py",
        "test_applying_migrations_twice_is_a_no_op",
    ),
    "6a. release artifacts contain no secrets": (
        "tests/architecture/test_release_artifact_hygiene.py",
        "test_the_build_context_excludes_what_must_never_ship",
    ),
    "6b. …no local paths or private fixtures — the build context is filtered at all": (
        "tests/architecture/test_release_artifact_hygiene.py",
        "test_the_build_context_is_filtered_at_all",
    ),
    "6c. …and no build debris: the spike page is not shipped": (
        "tests/architecture/test_release_artifact_hygiene.py",
        "test_the_spike_page_is_not_shipped",
    ),
    "6d. …and no undeclared dependencies: the image resolves from the lockfile": (
        "tests/architecture/test_release_artifact_hygiene.py",
        "test_the_image_resolves_from_the_lockfile_rather_than_re_resolving",
    ),
    # §29.1's composition constraint is not a numbered criterion, but the gate
    # depends on it: "process co-location shall not bypass the versioned target
    # API or adapter boundary". Named here so deleting its test breaks this map.
    "6e. …and the artifact preserves the §25.11 boundary (ADR-0006)": (
        "tests/integration/test_one_origin_composition.py",
        "test_a_storefront_call_reaches_the_store_over_its_versioned_http_api",
    ),
}

#: 008's exit gate, each criterion to its covering test.
EXIT_GATE_008: dict[str, tuple[str, str]] = {
    "1a. the import path is configured without any credential": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_1_the_import_path_is_configured_without_any_credential",
    ),
    "1b. the complete fixture path runs with no LLM or Shopify credential": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_1_the_complete_fixture_path_runs_with_no_credential",
    ),
    "1c. …and with Node unavailable — the importer runs no process": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_1_no_node_or_subprocess_is_used",
    ),
    "2a. malformed, unsupported, and unpinned reports fail closed": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_2_unreadable_reports_fail_closed",
    ),
    "2b. an oversized report fails closed before parsing": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_2_an_oversized_report_fails_closed",
    ),
    "2c. an ambiguous binding fails closed": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_2_an_ambiguous_binding_fails_closed",
    ),
    "2d. a duplicate binding fails closed": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_2_a_duplicate_binding_fails_closed",
    ),
    "2e. a cross-workspace benchmark fails closed": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_2_a_cross_workspace_benchmark_fails_closed",
    ),
    "3a. matrix cells sum to eligible; eligible plus excluded equals total": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_3_the_counting_identities_hold_on_the_fixture",
    ),
    "3b. errors are a disclosed subset of excluded, never a third population": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_3_errors_are_a_disclosed_subset_never_a_third_population",
    ),
    "3c. an empty population yields null rates rather than zeroes": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_3_an_empty_population_yields_null_rates_not_zeroes",
    ),
    "4a. source kinds, scenario modes, and failure profiles are never pooled": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_4_populations_are_never_pooled",
    ),
    "4b. correlation modes stay separate": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_4_a_suite_refuses_a_trial_from_the_other_mode",
    ),
    "5a. AC-16 passes from the checked-in fixture": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_5_ac_16_passes_from_the_checked_in_fixture",
    ),
    "5b. …and the Tier 2 gate still needs AC-25 and AC-26 from later milestones": (
        "tests/integration/test_008_exit_gate.py",
        "test_gate_5_the_tier_two_gate_needs_more_than_this_milestone",
    ),
}

#: 010's exit gate, each criterion to its covering test.
EXIT_GATE_010: dict[str, tuple[str, str]] = {
    "1a. the suite is labelled `live_model_run` by the application": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_1_a_live_suite_is_labelled_by_the_application",
    ),
    "1b. a fixture-backed run is never represented as a live execution": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_1_a_fixture_is_never_represented_as_a_live_execution",
    ),
    "2. exported evaluator and model parameters are recorded without invention": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_2_exported_parameters_are_recorded_without_invention",
    ),
    "3a. a live report travels the AC-16 pipeline unchanged": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_3_a_live_report_travels_the_ac_16_pipeline_unchanged",
    ),
    "3b. each eligible trial binds and the dual-layer matrix holds": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_3_every_eligible_trial_binds_and_the_matrix_holds",
    ),
    "4a. the credential stays in the evaluator process environment": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_4_the_credential_stays_in_the_evaluator_environment",
    ),
    "4b. a credential cannot arrive through an uploaded manifest": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_4_a_credential_cannot_arrive_through_an_upload",
    ),
    "5. the CI fixture path still passes, labelled `recorded_fixture`": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_5_the_ci_fixture_path_passes_with_no_credential",
    ),
    "6a. AC-17 needs a live run this suite cannot perform (010-T11 is open)": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_6_ac_17_needs_a_live_run_this_suite_cannot_perform",
    ),
    "6b. …and Shopify work has therefore not started": (
        "tests/integration/test_010_exit_gate.py",
        "test_gate_6_shopify_work_has_not_started",
    ),
}

#: 013's exit gate, in the spec's own words.
#:
#: Criterion 6 is "full suite, architecture lane, both frontend gates green",
#: which no test can assert about itself — a suite that failed would not be
#: around to report it. It is covered instead by the gate that would fail if a
#: lane stopped selecting tests, since a marker typo silently deselecting a lane
#: is the way "all green" becomes true by running nothing.
EXIT_GATE_013: dict[str, tuple[str, str]] = {
    "1. same snapshots produce a byte-identical changed-path set and partition": (
        "tests/integration/test_013_exit_gate.py",
        "test_gate_1_the_same_snapshots_produce_an_identical_partition",
    ),
    "1b. …proved at the diff itself, including decimal reformatting and ordering": (
        "tests/unit/test_state_diff.py",
        "test_the_same_snapshots_produce_a_byte_identical_result",
    ),
    "2. a run whose only defect is an unnamed-path mutation fails with `undeclared_state_change`": (
        "tests/integration/test_013_exit_gate.py",
        "test_gate_2_every_named_assertion_passes_and_the_run_fails",
    ),
    "2b. …the report names the paths": (
        "tests/integration/test_013_exit_gate.py",
        "test_gate_2_the_failure_is_classified_and_names_its_paths",
    ),
    "2c. …and the same journey passes with the fault inactive (FR-019's pair)": (
        "tests/integration/test_013_exit_gate.py",
        "test_gate_2_the_same_journey_passes_with_the_fault_inactive",
    ),
    "2d. …the UI shows a 'changed outside contract' panel": (
        "apps/actionwitness_service/frontend/src/components/panels.test.tsx",
        "lists every path the server said was undeclared",
    ),
    "3. `allow_paths` admits declared churn without widening anything else": (
        "tests/integration/test_undeclared_change_evaluation.py",
        "test_a_waiver_admits_churn_without_widening_anything_else",
    ),
    "3b. …a waiver respects the dotted-key boundary, so it is a hatch not a hole": (
        "tests/unit/test_trajectory_and_policies.py",
        "test_a_waiver_respects_the_same_boundary_rule",
    ),
    "3c. …and every applied waiver is recorded so it is never invisible": (
        "tests/unit/test_trajectory_and_policies.py",
        "test_an_applied_waiver_is_recorded_so_it_is_never_invisible",
    ),
    "4. a generated eval case reproduces the same classification in replay": (
        "tests/integration/test_013_exit_gate.py",
        "test_gate_4_a_generated_case_reproduces_the_classification",
    ),
    "4b. …and the policy is evaluated during replay rather than skipped": (
        "tests/integration/test_undeclared_change_replay.py",
        "test_the_policy_is_evaluated_in_replay_rather_than_skipped",
    ),
    "5. the `undeclared_side_effect` template ships": (
        "tests/integration/test_013_exit_gate.py",
        "test_gate_5_the_undeclared_side_effect_template_ships",
    ),
    "5b. …and the template-honesty test now covers three profiles": (
        "tests/integration/test_013_exit_gate.py",
        "test_gate_5_the_template_honesty_gate_covers_the_profile_013_added",
    ),
    "6. full suite, architecture lane, and both frontend gates green (DISCHARGED "
    "BY RUNNING THEM; the covering test is the lane gate that fails if one "
    "stops selecting tests)": (
        "tests/architecture/test_test_lanes.py",
        "test_every_lane_selects_at_least_one_test",
    ),
}

#: 014's exit gate, in the spec's own words.
#:
#: Criterion 6 is the same shape as 013's — "full suite, architecture lane, both
#: frontend gates green" is not something a suite can assert about itself — so it
#: is carried by the lane gate that fails when a lane stops selecting tests.
EXIT_GATE_014: dict[str, tuple[str, str]] = {
    "1. arming a run persists a surface baseline whose hash is reproducible from "
    "the recorded definitions": (
        "tests/integration/test_014_exit_gate.py",
        "test_gate_1_arming_persists_a_reproducible_baseline",
    ),
    "1b. …the canonicalisation that hash rests on does not invent a delta (§17.2)": (
        "tests/unit/test_tool_surface.py",
        "test_order_insensitive_keywords_hash_identically",
    ),
    "1c. …and does not erase one either": (
        "tests/unit/test_tool_surface.py",
        "test_a_real_schema_difference_survives_canonicalisation",
    ),
    "2. a mid-run registration change produces `tool_surface_changed` with the "
    "correct §9.5 delta kind": (
        "tests/integration/test_014_exit_gate.py",
        "test_gate_2_a_mid_run_change_records_the_correct_kind",
    ),
    "2b. …declared churn produces no failure": (
        "tests/integration/test_014_exit_gate.py",
        "test_gate_2_declared_churn_produces_no_failure",
    ),
    "2c. …and the browser re-captures on every `toolchange` (FR-167)": (
        "apps/actionwitness_service/frontend/src/webmcp/surface.test.ts",
        "re-captures after a toolchange",
    ),
    "3. `stable_tool_surface` passes on a quiet surface, fails with "
    "`tool_surface_mutation` on an undeclared delta, and still fails closed when "
    "capture never ran": (
        "tests/integration/test_014_exit_gate.py",
        "test_gate_3_the_policy_fails_closed_without_a_baseline",
    ),
    "3b. …an undeclared delta of a configured kind fails": (
        "tests/unit/test_trajectory_and_policies.py",
        "test_a_delta_in_the_failing_set_fails_the_policy",
    ),
    "3c. …and a harness-partition delta does not (§9.11)": (
        "tests/unit/test_trajectory_and_policies.py",
        "test_a_harness_partition_delta_does_not_fail_the_policy",
    ),
    "4. the poisoned-profile journey fails on the definition diff while every "
    "business assertion passes": (
        "tests/integration/test_014_exit_gate.py",
        "test_gate_4_the_poisoned_journey_fails_on_the_definition_diff",
    ),
    "4b. …and the failure carries FR-169's side-by-side definitions": (
        "tests/integration/test_014_exit_gate.py",
        "test_gate_4_the_failure_carries_the_side_by_side_diff",
    ),
    "4c. …a pre-invocation identity mismatch is refused and recorded (FR-169)": (
        "tests/integration/test_tool_surface_capture.py",
        "test_a_changed_definition_refuses_the_invocation",
    ),
    "5. replay of a case carrying surface evidence reproduces the classification": (
        "tests/integration/test_014_exit_gate.py",
        "test_gate_5_a_case_with_surface_evidence_reproduces_the_classification",
    ),
    "6. full suite, architecture lane, and both frontend gates green (DISCHARGED "
    "BY RUNNING THEM; the covering test is the lane gate that fails if one "
    "stops selecting tests)": (
        "tests/architecture/test_test_lanes.py",
        "test_every_lane_selects_at_least_one_test",
    ),
}

#: 015's exit gate, in the spec's own words.
#:
#: Criterion 5 is the lane gate again, for the reason given above 013's map.
#:
#: Criterion 3 is the one worth reading twice. "Refuses at every layer" is three
#: separate claims, and the third — that no audit module can reach the network at
#: all — is the only one that survives somebody rewriting the calling code, so it
#: is named here from the architecture lane rather than folded into the API test.
EXIT_GATE_015: dict[str, tuple[str, str]] = {
    "1. with the module unconfigured, nothing about the feature is reachable and "
    "the capability surface reports it unavailable": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_1_an_unconfigured_module_reaches_nothing",
    ),
    "1b. …and unconfigured is what the deployment ships as, read from the "
    "settings default rather than from documentation (§29.1)": (
        "tests/architecture/test_audit_guardrails.py",
        "test_the_deployment_ships_with_the_audit_disabled",
    ),
    "2. an audit against the half-broken fixture names the silently-failing cart tool": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_2_the_audit_names_the_tool_that_lied",
    ),
    "2b. …with reported-versus-observed evidence beside it": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_2_the_evidence_shows_the_claim_beside_the_observation",
    ),
    "2c. …while an honest storefront is not accused (the falsifiability half)": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_2_an_honest_storefront_is_not_accused",
    ),
    "2d. …and the never-invoked tools are listed as present-but-unexercised": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_2_the_never_invoked_tools_are_reported_not_exercised",
    ),
    "2e. …and the report names the pack that was applied (FR-161)": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_2_the_pack_offered_is_the_pack_named_in_the_report",
    ),
    "2f. …the fixture's cart tool really does answer identically either way, "
    "which is the premise the whole criterion rests on": (
        "apps/actionwitness_service/frontend/src/test/halfBrokenStorefront.test.ts",
        "returns the same words when it is telling the truth",
    ),
    "3. a non-allowlisted origin is refused at the API": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_3_a_non_allowlisted_origin_is_refused_at_the_api",
    ),
    "3b. …and at the adapter, which will not accept an observation from anywhere else (§12.17)": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_3_a_non_allowlisted_origin_is_refused_at_the_adapter",
    ),
    "3c. …and structurally: no audit module holds a network client at all (FR-160a)": (
        "tests/architecture/test_audit_guardrails.py",
        "test_no_audit_module_can_reach_the_network",
    ),
    "3d. …nor any affordance for more than one target, since a list of origins "
    "is a scan queue with a friendlier name": (
        "tests/architecture/test_audit_guardrails.py",
        "test_the_audit_api_accepts_one_origin_and_never_a_list",
    ),
    "3e. …and no offered pack can dispatch checkout or order management (FR-162)": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_3_no_offered_pack_can_dispatch_a_consequential_tool",
    ),
    "4. the report carries none of the harness's vocabulary": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_4_the_report_uses_no_harness_vocabulary",
    ),
    "4b. …states its own limits, so a clean result is not read as a guarantee": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_4_the_report_states_its_own_limits",
    ),
    "4c. …and needs no browser to render, being data rather than a component": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_4_the_report_needs_no_browser_to_render",
    ),
    "4d. …and every outcome the classifier can produce has a sentence, so no row renders blank": (
        "tests/integration/test_015_exit_gate.py",
        "test_gate_4_every_outcome_the_classifier_can_produce_has_merchant_copy",
    ),
    "5. full suite, architecture lane, and both frontend gates green (DISCHARGED "
    "BY RUNNING THEM; the covering test is the lane gate that fails if one "
    "stops selecting tests)": (
        "tests/architecture/test_test_lanes.py",
        "test_every_lane_selects_at_least_one_test",
    ),
}

MAPS = {
    "003": EXIT_GATE_003,
    "004": EXIT_GATE_004,
    "005": EXIT_GATE_005,
    "006": EXIT_GATE_006,
    "007": EXIT_GATE_007,
    "008": EXIT_GATE_008,
    "009": EXIT_GATE_009,
    "010": EXIT_GATE_010,
    "013": EXIT_GATE_013,
    "014": EXIT_GATE_014,
    "015": EXIT_GATE_015,
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
RELEASE_CHECKLIST = REPO_ROOT / "docs" / "release-checklist.md"


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
def test_the_release_checklist_covers_the_operator_criteria() -> None:
    """009's criteria 2, 3 and 4 need a Render account and a human with a browser.

    Same precedent as 006's browser criteria: the checklist *is* the coverage, so
    it is named as their covering "test" and its content is asserted. A map should
    point at something that fails when the coverage disappears, and this does.

    The phrases below are the load-bearing ones rather than a transcript. Each
    marks a step that is easy to quietly drop and expensive to have dropped:
    promoting one built artifact rather than rebuilding, rehearsing the rollback
    rather than assuming it, and proving the live URL needs no credential.
    """
    assert RELEASE_CHECKLIST.is_file(), "the 009 operator release checklist is missing"
    text = RELEASE_CHECKLIST.read_text(encoding="utf-8")
    required = (
        "Image digest",
        "Rollback rehearsed",
        "previous deploy is retained",
        "credential-free",
        "HARNESS_PUBLIC_ORIGIN",
        "Attested by:",
    )
    missing = [phrase for phrase in required if phrase not in text]
    assert missing == [], f"the release checklist no longer covers {missing}"


@pytest.mark.architecture
def test_the_deferred_ac_01_row_names_where_it_is_discharged() -> None:
    """006 deferred AC-01 to M8. A deferral that names no destination is a drop.

    This is the link between the two checklists: 006 says "recheck once 009
    lands", and 009's checklist is where that recheck happens. If the release
    checklist is ever renamed, this fails rather than leaving AC-01 pointing at
    nothing.
    """
    tier_one = CHECKLIST.read_text(encoding="utf-8")
    assert "AC-01" in tier_one
    assert "009" in tier_one, "the deferred AC-01 row no longer names the milestone that closes it"
    assert RELEASE_CHECKLIST.is_file()


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
#: assumed: 003–005 list five, 006 lists six because the Tier 1 gate is a
#: criterion in its own right, and 007 lists seven. A shared constant would have
#: to be loosened to admit each of them, and loosening it is exactly how a
#: dropped criterion gets through.
PUBLISHED_CRITERIA: dict[str, int] = {
    "003": 5,
    "004": 5,
    "005": 5,
    "006": 6,
    "007": 7,
    "008": 5,
    "009": 6,
    "010": 6,
    "013": 6,
    "014": 6,
    "015": 5,
}


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
