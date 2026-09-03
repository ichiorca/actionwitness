/**
 * The harness side of the Shopify pairing (§12.12, §14, §15.7, §16.5, AC-18).
 *
 * The panel is the tab a person watches while an agent works in another one, so
 * these tests are about what it *says* rather than what it renders: does it name
 * the same pairing, expiry, actor and next action the storefront card names, and
 * does every ending — expiry, cancellation, a stopped pairing — arrive with an
 * instruction rather than as a dead control.
 *
 * The other thing under test is a negative: the launch URL carries a one-time
 * credential in its fragment, and no assertion anywhere should be able to find
 * that credential in the DOM.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ShopifyPairing } from "../api/shopify";
import {
  PAIRING_GUIDANCE,
  ShopifyPairingPanel,
  isTerminalPairing,
  redactLaunchUrl,
} from "./ShopifyPairingPanel";

const CREDENTIAL = "one-time-credential-abcdefgh";
const LAUNCH = "https://authorized-dev-store.example/#actionwitness=pair_abcd." + CREDENTIAL;

const PAIRING: ShopifyPairing = {
  pairingId: "pair_0000abcd",
  status: "created",
  expiresAt: "2026-09-03T12:15:00Z",
  storeOrigin: "https://authorized-dev-store.example",
  contractId: "con_1",
  runId: null,
  overallResult: null,
  report: null,
  activeActor: null,
  nextAction: null,
  observations: [],
};

function renderPanel(overrides: Partial<React.ComponentProps<typeof ShopifyPairingPanel>> = {}) {
  const onCreate = vi.fn();
  const onOpenStorefront = vi.fn();
  render(
    <ShopifyPairingPanel
      moduleStatus="enabled"
      moduleReason=""
      contractId="con_1"
      pairing={null}
      redactedLaunchUrl={null}
      busy={false}
      error={null}
      onCreate={onCreate}
      onOpenStorefront={onOpenStorefront}
      {...overrides}
    />,
  );
  return { onCreate, onOpenStorefront };
}

describe("ShopifyPairingPanel", () => {
  it("says the target is off, with its reason, rather than offering a form that would refuse", () => {
    // Arrange / Act
    renderPanel({ moduleStatus: "disabled", moduleReason: "no development store is configured" });

    // Assert — §21.1: a cut or unconfigured Tier 3 feature is visibly disabled
    // with its reason, never half-enabled.
    expect(screen.getByText(/no development store is configured/)).toBeDefined();
    expect(screen.queryByRole("button", { name: /create pairing/i })).toBeNull();
  });

  it("explains in words why it cannot pair, rather than only greying the button", () => {
    // Arrange / Act — enabled, but nothing selected to verify.
    renderPanel({ contractId: null });

    // Assert — §8.4 forbids the disabled state being the only signal.
    const create = screen.getByRole("button", { name: /create pairing/i });
    expect(create.hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/Select a Shopify contract first/)).toBeDefined();
  });

  it("creates a pairing when a contract is selected", () => {
    // Arrange
    const { onCreate } = renderPanel();

    // Act
    fireEvent.click(screen.getByRole("button", { name: /create pairing/i }));

    // Assert
    expect(onCreate).toHaveBeenCalledTimes(1);
  });

  it("shows the pairing suffix, expiry, actor and next action the other tab shows", () => {
    // Arrange / Act — §14: both tabs, the same four facts.
    renderPanel({ pairing: { ...PAIRING, status: "armed" } });

    // Assert
    expect(screen.getByText("…abcd")).toBeDefined();
    expect(screen.getByText("2026-09-03T12:15:00Z")).toBeDefined();
    expect(screen.getByText("agent")).toBeDefined();
    expect(screen.getByText(/verify_shopify_outcome/)).toBeDefined();
  });

  it("prefers the server's own actor and next action when it sends them", () => {
    // Arrange / Act — FR-120: the server owns what happens next; the table is
    // only the rendering of a state it did not annotate.
    renderPanel({
      pairing: {
        ...PAIRING,
        status: "armed",
        activeActor: "human_approver",
        nextAction: "Approve the pending confirmation.",
      },
    });

    // Assert
    expect(screen.getByText("human_approver")).toBeDefined();
    expect(screen.getByText("Approve the pending confirmation.")).toBeDefined();
    expect(screen.queryByText(/verify_shopify_outcome/)).toBeNull();
  });

  it.each([
    ["expired", /Create a new pairing and open its launch URL/],
    ["cancelled", /Create a new pairing when you are ready/],
    ["error", /nothing from the stopped attempt is carried forward/],
    ["failed", /Create a new pairing to run the journey again/],
  ])("gives a bounded recovery instruction in the %s state", (status, expected) => {
    // Arrange / Act — §14: a closed tab, a reload, an expiry and a cancellation
    // each end with an instruction, never with a silent disabled control.
    renderPanel({ pairing: { ...PAIRING, status } });

    // Assert
    expect(screen.getByText(expected)).toBeDefined();
    expect(screen.getByRole("button", { name: /create a new pairing/i })).toBeDefined();
  });
});

describe("the launch URL is a credential", () => {
  it("never renders the one-time credential, before or after the transfer", () => {
    // Arrange / Act — the fragment is the one-time credential (§15.7).
    const { onOpenStorefront } = renderPanel({
      pairing: PAIRING,
      redactedLaunchUrl: redactLaunchUrl(LAUNCH),
    });

    // Assert — the origin is shown so a person can check where the link goes;
    // the secret is not, anywhere in the tree or in any attribute.
    expect(document.body.textContent).toContain("https://authorized-dev-store.example/#…");
    expect(document.body.innerHTML).not.toContain(CREDENTIAL);

    // Act — the transfer to the storefront goes through a handler holding the
    // URL in memory. Pressing it must not put one in the DOM either.
    fireEvent.click(screen.getByRole("button", { name: /open the storefront tab/i }));

    // Assert
    expect(onOpenStorefront).toHaveBeenCalledTimes(1);
    expect(document.body.innerHTML).not.toContain(CREDENTIAL);
  });

  it("redacts the fragment and leaves a URL without one alone", () => {
    // Assert
    expect(redactLaunchUrl(LAUNCH)).toBe("https://authorized-dev-store.example/#…");
    expect(redactLaunchUrl("https://shop.example/")).toBe("https://shop.example/");
  });
});

describe("cart evidence", () => {
  it("reports the normalized totals, and says when a number is missing", () => {
    // Arrange / Act — FR-113's normalized totals. A null subtotal is not zero:
    // "nobody computed one" and "it is 0.00" are different facts.
    renderPanel({
      pairing: {
        ...PAIRING,
        status: "verifying",
        observations: [
          {
            phase: "before",
            capturedAt: "2026-09-03T12:00:00Z",
            contentHash: "sha256:aaa",
            capturePath: "/en-gb/cart.js",
            provider: "shopify_cart_state",
            provenance: "platform_session_api",
            itemCount: 0,
            currency: "USD",
            subtotal: "0.00",
            total: "0.00",
          },
          {
            phase: "after",
            capturedAt: "2026-09-03T12:05:00Z",
            contentHash: "sha256:bbb",
            capturePath: "/en-gb/cart.js",
            provider: "shopify_cart_state",
            provenance: "platform_session_api",
            itemCount: 1,
            currency: "USD",
            subtotal: "25.00",
            total: null,
          },
        ],
      },
    });

    // Assert
    expect(screen.getByText("25.00")).toBeDefined();
    expect(screen.getAllByText("not reported").length).toBe(1);
    expect(screen.getByRole("columnheader", { name: "Source" })).toBeDefined();
    expect(screen.getAllByText("shopify_cart_state / platform_session_api")).toHaveLength(2);
  });

  it("says there is no evidence yet rather than showing an empty table", () => {
    // Arrange / Act
    renderPanel({ pairing: { ...PAIRING, status: "paired" } });

    // Assert
    expect(screen.getByText(/No cart evidence yet/)).toBeDefined();
  });

  it("links the immutable report once the run has one", () => {
    // Arrange / Act
    renderPanel({
      pairing: {
        ...PAIRING,
        status: "passed",
        runId: "run_1",
        overallResult: "passed",
        report: "/api/v1/runs/run_1/report",
      },
    });

    // Assert — the verdict is a word, not a colour (§8.4), and it is said twice:
    // once as the pairing's state and once as the run's own result. Those are
    // two different facts that happen to agree here, and a reader should be able
    // to see both rather than infer one from the other.
    expect(screen.getAllByText("passed").length).toBe(2);
    expect(screen.getByRole("link", { name: /open the immutable report/i })).toBeDefined();
  });
});

describe("the pairing state machine (§16.5)", () => {
  it("treats every terminal state as terminal and no other", () => {
    // Assert
    for (const terminal of [
      "passed",
      "passed_with_warnings",
      "failed",
      "expired",
      "cancelled",
      "error",
    ]) {
      expect(isTerminalPairing(terminal), terminal).toBe(true);
    }
    for (const live of ["created", "paired", "armed", "verifying"]) {
      expect(isTerminalPairing(live), live).toBe(false);
    }
  });

  it("has guidance for every state the machine can be in", () => {
    // Assert — a state with no row would render "Waiting for a pairing" over a
    // live journey, which is the silent dead end §14 rules out.
    for (const state of [
      "created",
      "paired",
      "armed",
      "verifying",
      "passed",
      "passed_with_warnings",
      "failed",
      "expired",
      "cancelled",
      "error",
    ]) {
      expect(PAIRING_GUIDANCE[state], state).toBeDefined();
    }
  });
});
