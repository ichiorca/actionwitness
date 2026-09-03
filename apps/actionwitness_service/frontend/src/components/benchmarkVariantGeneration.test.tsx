/**
 * Drafting intent variants with the model, from a person's side (FR-100).
 *
 * The route and the client are covered in Python; what these tests hold is the
 * part only the browser can get wrong.
 *
 * - **a draft is not an approval.** Rows arrive unticked, and a freeze straight
 *   after a draft carries an empty `approvedIndices` — because nobody has read
 *   anything yet. This is the single most tempting shortcut in the feature and
 *   the one the constitution names by hand.
 * - **the model's text is editable before it is approved.** A reviewer who
 *   changes a word must have their words frozen, not the model's.
 * - **the panel degrades honestly.** With no live backend the control is
 *   disabled and says why *in words*, and the hand-written path is untouched —
 *   that is the default deployment, so it is the state most people see.
 * - **a refusal is shown, not swallowed.** The server distinguishes "no
 *   backend" from "the model refused" from "the answer was unusable", and the
 *   page shows that sentence rather than a generic apology.
 *
 * Every test asserts what happens *after* an interaction. The generator is
 * injected, so nothing here performs a request.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { DraftedVariants } from "../api/benchmark";
import { parseBenchmark } from "../api/benchmark";
import { BenchmarkSection } from "./BenchmarkSection";

const CANONICAL = "Add one ceramic mug to the cart and apply the SAVE20 discount.";

const DRAFTED: DraftedVariants = {
  canonicalIntent: CANONICAL,
  variants: [
    { kind: "paraphrased", text: "Please add a ceramic mug and use the SAVE20 code." },
    { kind: "ambiguous", text: "I would like a mug, discounted somehow." },
  ],
  modelProvider: "google",
  modelName: "example-model-1",
  approved: false,
};

function rate() {
  return { numerator: 0, denominator: 0, value: null };
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
  call_level_pass_rate: rate(),
  outcome_pass_rate: rate(),
  end_to_end_success_rate: rate(),
  silent_outcome_failure_rate: rate(),
  incremental_outcome_failure_trials: 0,
};

function payload() {
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
    },
    counts: COUNTS,
    metrics: METRICS,
    by_scenario: [],
    by_failure_profile: [],
    trials: [],
  };
}

function renderSection(
  options: {
    generate?: (
      benchmarkId: string,
      canonicalIntent: string,
      count: number,
    ) => Promise<DraftedVariants>;
    liveEvaluatorStatus?: string;
    liveEvaluatorReason?: string;
  } = {},
) {
  const onFreezeVariants = vi.fn();
  const onGenerateVariants = vi.fn(options.generate ?? (() => Promise.resolve(DRAFTED)));
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
      benchmark={parseBenchmark(payload())}
      busy={false}
      liveEvaluatorStatus={options.liveEvaluatorStatus ?? "enabled"}
      liveEvaluatorReason={options.liveEvaluatorReason ?? ""}
      onSelect={vi.fn()}
      onCreate={vi.fn()}
      onImport={vi.fn()}
      onFreezeVariants={onFreezeVariants}
      onGenerateVariants={onGenerateVariants}
      onReplay={vi.fn()}
      onFinalize={vi.fn()}
      trialHref={() => ""}
      reportHref=""
    />,
  );
  return { onFreezeVariants, onGenerateVariants };
}

function draftButton(): HTMLElement {
  return screen.getByRole("button", { name: "Draft variants with the model" });
}

describe("BenchmarkSection — drafting variants with the model", () => {
  it("fills the rows with what the model proposed", async () => {
    // Arrange
    const { onGenerateVariants } = renderSection();
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });

    // Act
    fireEvent.click(draftButton());

    // Assert
    await waitFor(() => {
      expect(screen.getByLabelText("Variant 2")).toBeTruthy();
    });
    expect(onGenerateVariants).toHaveBeenCalledWith("bench_1", CANONICAL, 3);
    expect(screen.getByLabelText("Variant 1")).toHaveProperty("value", DRAFTED.variants[0]?.text);
    expect(screen.getByLabelText("Kind of variant 2")).toHaveProperty("value", "ambiguous");
  });

  it("leaves every drafted row unapproved", async () => {
    // Arrange
    const { onFreezeVariants } = renderSection();
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });
    fireEvent.change(screen.getByLabelText("Reviewer"), { target: { value: "ada" } });

    // Act — draft, then freeze without ticking anything.
    fireEvent.click(draftButton());
    await waitFor(() => {
      expect(screen.getByLabelText("Variant 2")).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: "Freeze variant set" }));

    // Assert — a draft nobody read approves nothing. Both texts still travel,
    // because an approval is bound to the set that was screened.
    expect(onFreezeVariants.mock.calls[0]?.[0].approvedIndices).toEqual([]);
    expect(onFreezeVariants.mock.calls[0]?.[0].variants).toHaveLength(2);
  });

  it("freezes the reviewer's edit rather than the model's wording", async () => {
    // Arrange
    const { onFreezeVariants } = renderSection();
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });
    fireEvent.change(screen.getByLabelText("Reviewer"), { target: { value: "ada" } });
    fireEvent.click(draftButton());
    await waitFor(() => {
      expect(screen.getByLabelText("Variant 1")).toBeTruthy();
    });

    // Act
    fireEvent.change(screen.getByLabelText("Variant 1"), {
      target: { value: "Add one mug and apply SAVE20, please." },
    });
    fireEvent.click(screen.getByLabelText("Approve variant 1"));
    fireEvent.click(screen.getByRole("button", { name: "Freeze variant set" }));

    // Assert
    expect(onFreezeVariants.mock.calls[0]?.[0].variants[0]).toEqual({
      kind: "paraphrased",
      text: "Add one mug and apply SAVE20, please.",
    });
    expect(onFreezeVariants.mock.calls[0]?.[0].approvedIndices).toEqual([0]);
  });

  it("says in words that nothing is approved yet", async () => {
    // Arrange
    renderSection();
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });

    // Act
    fireEvent.click(draftButton());

    // Assert — §8.4: an unticked checkbox is a visual state, and the sentence
    // is what a screen reader and a colour-blind reader both get.
    await waitFor(() => {
      expect(screen.getByText(/None is approved/)).toBeTruthy();
    });
    expect(screen.getByText(/example-model-1/)).toBeTruthy();
  });

  it("asks for the canonical intent before it asks the model", () => {
    // Arrange
    const { onGenerateVariants } = renderSection();

    // Act — the intent is still blank.
    fireEvent.click(draftButton());

    // Assert
    expect(onGenerateVariants).not.toHaveBeenCalled();
    expect(screen.getByText(/Write the canonical intent first/)).toBeTruthy();
  });

  it("sends the number of candidates the reviewer chose", async () => {
    // Arrange
    const { onGenerateVariants } = renderSection();
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });

    // Act
    fireEvent.change(screen.getByLabelText("Candidates to draft"), { target: { value: "6" } });
    fireEvent.click(draftButton());

    // Assert
    await waitFor(() => {
      expect(onGenerateVariants).toHaveBeenCalledWith("bench_1", CANONICAL, 6);
    });
  });

  it("reports a model that proposed nothing as a result, not a blank form", async () => {
    // Arrange
    const empty: DraftedVariants = { ...DRAFTED, variants: [] };
    renderSection({ generate: () => Promise.resolve(empty) });
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });

    // Act
    fireEvent.click(draftButton());

    // Assert — "the model had nothing to suggest" is a fact the reviewer needs,
    // and it is different from a form that never filled in.
    await waitFor(() => {
      expect(screen.getByText(/proposed no variants/)).toBeTruthy();
    });
    expect(screen.getByLabelText("Variant 1")).toHaveProperty("value", "");
  });

  it("shows the server's own refusal rather than a generic apology", async () => {
    // Arrange
    renderSection({
      generate: () => Promise.reject(new Error("This deployment has no live model backend.")),
    });
    fireEvent.change(screen.getByLabelText("Canonical intent"), { target: { value: CANONICAL } });

    // Act
    fireEvent.click(draftButton());

    // Assert
    await waitFor(() => {
      expect(screen.getByText(/no live model backend/)).toBeTruthy();
    });
    // The refusal never silently fills the form with something.
    expect(screen.getByLabelText("Variant 1")).toHaveProperty("value", "");
  });

  it("degrades to the hand-written path when no backend is configured", () => {
    // Arrange / Act
    renderSection({
      liveEvaluatorStatus: "disabled",
      liveEvaluatorReason: "LIVE_EVALUATOR_ENABLED is off",
    });

    // Assert — disabled *and* explained. §8.4 forbids the greying-out being the
    // only signal, and the freeze this panel exists for is still offered.
    expect(draftButton()).toHaveProperty("disabled", true);
    expect(screen.getByText(/Drafting is unavailable/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Freeze variant set" })).toBeTruthy();
    expect(screen.getByLabelText("Variant 1")).toBeTruthy();
  });

  it("offers drafting through native controls a keyboard reaches", () => {
    // Arrange / Act
    renderSection();

    // Assert
    expect(draftButton().tagName).toBe("BUTTON");
    expect(screen.getByLabelText("Candidates to draft").tagName).toBe("SELECT");
  });
});
