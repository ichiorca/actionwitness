/**
 * The guidance banner's copy (§14, §15.1, FR-120, AC-21).
 *
 * AC-21 requires a blocking transition to render one active actor, one primary
 * next action, why it is required, its expected consequence, any waiting
 * condition, and a safe recovery. Five of those were already sentences. The
 * sixth was not: the recovery rendered `guidance.recoveryActionCode` directly,
 * so a person reading a stalled workspace was told "If this stalls:
 * reset_workspace" — an enum token, in the one place the product is supposed to
 * be explaining itself to a human being.
 *
 * The fix is a lookup, not new copy: `GUIDANCE_ACTION_DESCRIPTIONS` in the core
 * already describes every code, and the generated registry publishes it here.
 * So these tests assert two separable things — that a sentence is shown, and
 * that the sentence is the server's rather than one this component made up.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { parseGuidance } from "../api/workspace";
import registry from "../generated/registry.json";
import { GuidanceBanner } from "./GuidanceBanner";

const AWAITING = parseGuidance({
  phase: "awaiting_confirmation",
  active_actor: "human_approver",
  next_actor: "agent",
  headline: "A protected action needs a person's decision.",
  instruction: "Review the pending action and choose Approve once or Deny.",
  reason: "This action changes state that cannot be undone by retrying.",
  expected_consequence: "Approving lets this one invocation proceed; denying stops it.",
  action_code: "decide_confirmation",
  recovery_action_code: "cancel_confirmation",
  waiting_for: "the agent is waiting for this decision before it can continue",
  requires_human_input: true,
});

const PASSED = parseGuidance({
  phase: "passed",
  active_actor: "operator",
  next_actor: null,
  headline: "The outcome passed.",
  instruction: "Read the report to see what was observed and how it was judged.",
  reason: "Every assertion held against independently observed state.",
  expected_consequence: "The run is terminal; its evidence is retained.",
  action_code: "review_findings",
  recovery_action_code: null,
  waiting_for: null,
  requires_human_input: false,
});

describe("guidance banner recovery copy", () => {
  it("renders the recovery as a sentence rather than the enum token", () => {
    // Arrange / Act
    render(<GuidanceBanner guidance={AWAITING} loading={false} />);

    // Assert — the sentence is present...
    const recovery = screen.getByText(/If this stalls:/).closest("p");
    expect(recovery).not.toBeNull();
    expect(recovery?.textContent).toContain("Withdraw the pending request");

    // ...and the bare token is not what a reader is shown. It survives only in
    // the hidden element AC-21 uses to compare surfaces.
    const visible = (recovery?.textContent ?? "").replace("cancel_confirmation", "");
    expect(visible).not.toContain("cancel_confirmation");
  });

  it("takes the sentence from the generated registry, not from this component", () => {
    // Arrange — the server's own description for the code under test.
    const expected = registry.enums.guidance_action_code.members.cancel_confirmation;

    // Act
    render(<GuidanceBanner guidance={AWAITING} loading={false} />);

    // Assert — FR-120: the frontend renders the server's answer; it does not
    // author a parallel one that could drift from the audit trail.
    expect(screen.getByText(expected, { exact: false })).toBeDefined();
  });

  it("still exposes the recovery code for the surfaces that must agree on it", () => {
    // Arrange / Act
    render(<GuidanceBanner guidance={AWAITING} loading={false} />);

    // Assert — §12.13 keeps the code stable while the copy may change, so a
    // test comparing the banner against the tool result compares codes.
    expect(screen.getByTestId("banner-recovery-action-code").textContent).toBe(
      "cancel_confirmation",
    );
  });

  it("renders no recovery line when the server offers none", () => {
    // Arrange / Act — a reached verdict did not stall, so the null is meaning,
    // not a missing value the banner should fill in.
    render(<GuidanceBanner guidance={PASSED} loading={false} />);

    // Assert
    expect(screen.queryByText(/If this stalls:/)).toBeNull();
    expect(screen.queryByTestId("banner-recovery-action-code")).toBeNull();
  });

  it("renders every one of AC-21's six elements when the server sends them", () => {
    // Arrange / Act
    render(<GuidanceBanner guidance={AWAITING} loading={false} />);

    // Assert — actor, action, reason, consequence, waiting condition, recovery.
    expect(screen.getByText(/Whose turn:/)).toBeDefined();
    expect(screen.getByText(/Next:/)).toBeDefined();
    expect(screen.getByText(/Why:/)).toBeDefined();
    expect(screen.getByText(/What happens:/)).toBeDefined();
    expect(screen.getByText(/Waiting for:/)).toBeDefined();
    expect(screen.getByText(/If this stalls:/)).toBeDefined();
  });
});

describe("the registry the banner reads", () => {
  it("describes every guidance action code the server can send", () => {
    // Arrange
    const members: Readonly<Record<string, string>> =
      registry.enums.guidance_action_code.members;

    // Act / Assert — a code with no description would put a raw token back in
    // front of a person the moment a new phase started using it.
    expect(Object.keys(members).length).toBeGreaterThan(0);
    for (const [code, text] of Object.entries(members)) {
      expect(text.trim(), `${code} has no readable description`).not.toBe("");
      expect(text, `${code} is described by its own name`).not.toBe(code);
    }
  });
});
