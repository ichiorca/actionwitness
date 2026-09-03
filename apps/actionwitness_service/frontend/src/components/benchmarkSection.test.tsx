/**
 * The frozen intent manifest, from a person's side (FR-100, §12.11).
 *
 * `BenchmarkService.freeze_variants` enforced FR-100's two timing rules and had
 * tests for both, and no route and no control could reach it — so the
 * requirement was implemented and unexercisable. These tests are about the half
 * that was missing, and every one of them asserts what the section does *after*
 * an interaction rather than what it renders on arrival.
 *
 * Three properties carry the block:
 *
 * - **the approval names a subset, and the subset is the reviewer's.** A
 *   reviewer who unticks one has decided; the request must carry that decision
 *   by position, because that is how the server binds an approval to texts.
 * - **approving nothing is a decision, not a validation failure.** Rejecting
 *   every variant is a real outcome; refusing to send it would push a reviewer
 *   into keeping one they did not want.
 * - **frozen and not-frozen are legible as words.** §8.4 forbids colour as the
 *   only status channel, and this state decides whether an action is still
 *   available at all.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { parseBenchmark } from "../api/benchmark";
import { BenchmarkSection } from "./BenchmarkSection";

const CANONICAL = "Add one ceramic mug to the cart and apply the SAVE20 discount.";

function rate(numerator: number, denominator: number, value: string | null) {
  return { numerator, denominator, value };
}

const COUNTS = {
  call_level_pass_outcome_pass: 0,
  call_level_pass_outcome_fail: 0,
  call_level_fail_outcome_pass: 0,
  call_level_fail_outcome_fail: 0,
  eligible_trials: 0,
  excluded_trials: 0,
  error_trials: 0,
  total_trials: 0,
};

const METRICS = {
  call_level_pass_rate: rate(0, 0, null),
  outcome_pass_rate: rate(0, 0, null),
  end_to_end_success_rate: rate(0, 0, null),
  silent_outcome_failure_rate: rate(0, 0, null),
  incremental_outcome_failure_trials: 0,
};

function frozenSet(overrides: Record<string, unknown> = {}) {
  return {
    canonical_intent: CANONICAL,
    variants: [
      { kind: "paraphrased", text: "Please add a ceramic mug and use the SAVE20 code." },
      { kind: "ambiguous", text: "I would like a mug, discounted somehow." },
    ],
    approval: {
      candidates_fingerprint: "sha256:" + "a".repeat(64),
      approved_indices: [0, 1],
      actor: "human",
      reviewer: "ada",
      approved_at: "2026-09-01T12:00:00+00:00",
      note: "",
    },
    ...overrides,
  };
}

function payload(overrides: Record<string, unknown> = {}, manifest: Record<string, unknown> = {}) {
  return {
    benchmark_id: "bench_1",
    status: "draft",
    source_kind: "recorded_fixture",
    correlation_mode: "imported_trajectory_replay",
    result_artifact_id: null,
    manifest_content_hash: "sha256:" + "b".repeat(64),
    manifest: {
      source_kind: "recorded_fixture",
      correlation_mode: "imported_trajectory_replay",
      evaluator_name: null,
      evaluator_version: null,
      model_provider: null,
      model_name: null,
      target_build_commit: null,
      reporter_schema: null,
      normalized_adapter_version: null,
      frozen_variants: null,
      ...manifest,
    },
    counts: COUNTS,
    metrics: METRICS,
    by_scenario: [],
    by_failure_profile: [],
    trials: [],
    ...overrides,
  };
}

function renderSection(
  options: {
    benchmark?: Record<string, unknown> | null;
    manifest?: Record<string, unknown>;
    liveEvaluatorStatus?: string;
    liveEvaluatorReason?: string;
  } = {},
) {
  const onFreezeVariants = vi.fn();
  const body = options.benchmark === null ? null : payload(options.benchmark, options.manifest);
  render(
    <BenchmarkSection
      benchmarks={[
        {
          benchmarkId: "bench_1",
          status: "draft",
          sourceKind: "recorded_fixture",
          correlationMode: "imported_trajectory_replay",
          resultArtifactId: null,
          createdAt: "2026-09-01T12:00:00+00:00",
        },
      ]}
      selectedId="bench_1"
      benchmark={body === null ? null : parseBenchmark(body)}
      busy={false}
      liveEvaluatorStatus={options.liveEvaluatorStatus ?? "enabled"}
      liveEvaluatorReason={options.liveEvaluatorReason ?? ""}
      onSelect={vi.fn()}
      onCreate={vi.fn()}
      onImport={vi.fn()}
      onFreezeVariants={onFreezeVariants}
      onReplay={vi.fn()}
      onFinalize={vi.fn()}
      trialHref={() => ""}
      reportHref=""
    />,
  );
  return { onFreezeVariants };
}

/** Fill the first row and the two fields every approval needs. */
function fillOneVariant(text: string): void {
  fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });
  fireEvent.change(screen.getByLabelText("Reviewer"), { target: { value: "ada" } });
  fireEvent.change(screen.getByLabelText("Variant 1"), { target: { value: text } });
}

describe("BenchmarkSection — the frozen intent manifest", () => {
  it("sends the whole reviewed set and the positions the reviewer kept", () => {
    // Arrange
    const { onFreezeVariants } = renderSection();
    fillOneVariant("Please add a ceramic mug and use the SAVE20 code.");
    fireEvent.click(screen.getByRole("button", { name: "Add variant" }));
    fireEvent.change(screen.getByLabelText("Variant 2"), {
      target: { value: "I would like a mug, discounted somehow." },
    });
    fireEvent.change(screen.getByLabelText("Kind of variant 2"), {
      target: { value: "ambiguous" },
    });

    // Act — the reviewer turns the second one down.
    fireEvent.click(screen.getByLabelText("Approve variant 2"));
    fireEvent.click(screen.getByRole("button", { name: "Freeze variant set" }));

    // Assert — both texts travel, because an approval is bound to the set that
    // was screened; only the first position is approved.
    expect(onFreezeVariants).toHaveBeenCalledTimes(1);
    expect(onFreezeVariants.mock.calls[0]?.[0]).toEqual({
      canonicalIntent: CANONICAL,
      reviewer: "ada",
      variants: [
        { kind: "paraphrased", text: "Please add a ceramic mug and use the SAVE20 code." },
        { kind: "ambiguous", text: "I would like a mug, discounted somehow." },
      ],
      approvedIndices: [0],
    });
  });

  it("sends an approval of nothing when the reviewer kept nothing", () => {
    // Arrange
    const { onFreezeVariants } = renderSection();
    fillOneVariant("Please add a ceramic mug and use the SAVE20 code.");

    // Act
    fireEvent.click(screen.getByLabelText("Approve variant 1"));
    fireEvent.click(screen.getByRole("button", { name: "Freeze variant set" }));

    // Assert — a reviewer who rejects everything has done the job, and the
    // record says so rather than the form refusing to carry it.
    expect(onFreezeVariants.mock.calls[0]?.[0].approvedIndices).toEqual([]);
  });

  it("will not send an approval nobody signed", () => {
    // Arrange
    const { onFreezeVariants } = renderSection();
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });
    fireEvent.change(screen.getByLabelText("Variant 1"), {
      target: { value: "Please add a ceramic mug and use the SAVE20 code." },
    });

    // Act — reviewer left blank.
    fireEvent.click(screen.getByRole("button", { name: "Freeze variant set" }));

    // Assert
    expect(onFreezeVariants).not.toHaveBeenCalled();
    expect(screen.getByText(/Name the reviewer/)).toBeTruthy();
  });

  it("will not send an empty variant", () => {
    // Arrange
    const { onFreezeVariants } = renderSection();
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });
    fireEvent.change(screen.getByLabelText("Reviewer"), { target: { value: "ada" } });

    // Act — the row is still blank.
    fireEvent.click(screen.getByRole("button", { name: "Freeze variant set" }));

    // Assert
    expect(onFreezeVariants).not.toHaveBeenCalled();
    expect(screen.getByText(/Every variant needs text/)).toBeTruthy();
  });

  it("stops offering rows at FR-100's ceiling of six", () => {
    // Arrange
    renderSection();
    const add = screen.getByRole("button", { name: "Add variant" });

    // Act
    for (let index = 0; index < 5; index += 1) {
      fireEvent.click(add);
    }

    // Assert
    expect(screen.getByLabelText("Variant 6")).toBeTruthy();
    expect(screen.queryByLabelText("Variant 7")).toBeNull();
    expect(screen.getByRole("button", { name: "Add variant" })).toHaveProperty("disabled", true);
  });

  it("removes a row a reviewer no longer wants", () => {
    // Arrange
    renderSection();
    fireEvent.click(screen.getByRole("button", { name: "Add variant" }));
    fireEvent.change(screen.getByLabelText("Variant 2"), { target: { value: "second phrasing" } });

    // Act
    fireEvent.click(screen.getByRole("button", { name: "Remove variant 2" }));

    // Assert
    expect(screen.queryByLabelText("Variant 2")).toBeNull();
    // The last row cannot be removed: an approval with no set at all is not a
    // review of anything.
    expect(screen.getByRole("button", { name: "Remove variant 1" })).toHaveProperty(
      "disabled",
      true,
    );
  });

  it("says not frozen in words, not by colour alone", () => {
    // Arrange / Act
    renderSection();

    // Assert
    expect(screen.getByText("Not frozen")).toBeTruthy();
  });

  it("reports the frozen set and the manifest it was sealed into", () => {
    // Arrange / Act
    renderSection({ manifest: { frozen_variants: frozenSet() } });

    // Assert — FR-100 says *content-hashed*, so the identity is shown rather
    // than left as a fact only the server holds.
    expect(screen.getByText("Frozen")).toBeTruthy();
    expect(screen.getByText("sha256:" + "b".repeat(64))).toBeTruthy();
    expect(screen.getByText(/approved by ada \(human\)/)).toBeTruthy();
    expect(screen.getByText(/Please add a ceramic mug/)).toBeTruthy();
  });

  it("withdraws the control once a set is frozen", () => {
    // Arrange / Act
    renderSection({ manifest: { frozen_variants: frozenSet() } });

    // Assert — FR-100 forbids rerunning generation between repetitions, so the
    // page does not offer an action the server would refuse.
    expect(screen.queryByRole("button", { name: "Freeze variant set" })).toBeNull();
  });

  it("distinguishes an approval of nothing from a suite that never had variants", () => {
    // Arrange / Act
    renderSection({
      manifest: {
        frozen_variants: frozenSet({ variants: [], approval: frozenSet().approval }),
      },
    });

    // Assert — "a human reviewed some and kept none" is a different fact from
    // "no variants were ever generated", and the page says which one this is.
    expect(screen.getByText("Frozen")).toBeTruthy();
    expect(screen.getByText(/approved none of the generated variants/)).toBeTruthy();
  });

  it("explains rather than offers the seal once the suite has left draft", () => {
    // Arrange / Act
    renderSection({ benchmark: { status: "ready" } });

    // Assert — "before trials begin" is the requirement, and `draft` is the one
    // state in which no trial has been imported or replayed.
    expect(screen.queryByRole("button", { name: "Freeze variant set" })).toBeNull();
    expect(screen.getByText(/frozen before trials begin/)).toBeTruthy();
  });

  it("asks for a suite before it asks for variants", () => {
    // Arrange / Act
    renderSection({ benchmark: null });

    // Assert
    expect(screen.queryByRole("button", { name: "Freeze variant set" })).toBeNull();
    expect(screen.getByText(/Choose or create a suite before freezing/)).toBeTruthy();
  });

  it("keeps the control when no model backend is configured, and says so", () => {
    // Arrange / Act
    renderSection({
      liveEvaluatorStatus: "disabled",
      liveEvaluatorReason: "GOOGLE_EVALS_ENABLED is off",
    });

    // Assert — the module only says whether a model was available to draft the
    // texts. Hiding the human's seal behind it would leave FR-100 unreachable
    // in every default deployment, which is the failure this section fixes.
    expect(screen.getByText(/GOOGLE_EVALS_ENABLED is off/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Freeze variant set" })).toBeTruthy();
  });

  it("offers the seal through native controls, so a keyboard reaches it", () => {
    // Arrange / Act
    renderSection();
    const freeze = screen.getByRole("button", { name: "Freeze variant set" });
    const approve = screen.getByLabelText("Approve variant 1");

    // Assert — a real button and a real checkbox are operated by Enter and
    // Space without this component reimplementing either.
    expect(freeze.tagName).toBe("BUTTON");
    expect(approve.tagName).toBe("INPUT");
    expect(approve.getAttribute("type")).toBe("checkbox");
  });

  it("names each variant's kind from the server's own vocabulary", () => {
    // Arrange / Act
    renderSection();

    // Assert — the three kinds come from the generated registry, so a fourth
    // would appear here without an edit and a rename could not silently break
    // the request.
    const kinds = screen.getByLabelText("Kind of variant 1");
    expect(kinds.textContent).toContain("paraphrased");
    expect(kinds.textContent).toContain("ambiguous");
    expect(kinds.textContent).toContain("adversarial");
  });
});
