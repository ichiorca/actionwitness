/**
 * The audit journey a person can actually walk (§12.17, FR-160–FR-163).
 *
 * The server half shipped complete and tested with no client at all, so these
 * tests are about the half that was missing: does a person get an honest answer
 * when the module is off, can they authorize exactly one origin, is the pack
 * genuinely their choice, and does the collector this page hands them refuse to
 * touch checkout.
 *
 * The last one is the test that matters most. `proceed_to_checkout` against a
 * real storefront creates a real order for a real customer, so "never invoked"
 * has to be a property of the generated text and not a line in a README.
 */

import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { AuditPack, LiveAudit } from "../api/audit";
import { AuditSection } from "./AuditSection";

const CART_PACK: AuditPack = {
  packId: "shopify_cart",
  title: "Shopify storefront — cart pass",
  signature: ["get_cart", "update_cart", "proceed_to_checkout"],
  neverInvoked: ["proceed_to_checkout", "manage_orders"],
};

const LIVE: LiveAudit = {
  auditId: "aud_1",
  authorizedOrigin: "https://shop.example",
  assertedBy: "the shop owner",
  assertedAt: "2026-01-01T00:00:00Z",
  status: "authorized",
};

function renderSection(overrides: Partial<React.ComponentProps<typeof AuditSection>> = {}) {
  const onAuthorize = vi.fn();
  const onSubmit = vi.fn();
  const onCancel = vi.fn();
  render(
    <AuditSection
      moduleStatus="enabled"
      moduleReason=""
      packs={[CART_PACK]}
      audit={null}
      outcome={null}
      busy={false}
      onAuthorize={onAuthorize}
      onSubmit={onSubmit}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onAuthorize, onSubmit, onCancel };
}

describe("AuditSection", () => {
  it("says the module is off rather than showing a form that would refuse", () => {
    // Arrange / Act
    renderSection({ moduleStatus: "disabled", moduleReason: "EXTERNAL_AUDIT_ENABLED is off" });

    // Assert — §21.1's mechanism: a cut feature is visibly disabled with its
    // reason, not a control that fails when pressed.
    expect(screen.getByText(/EXTERNAL_AUDIT_ENABLED is off/)).toBeDefined();
    expect(screen.queryByRole("button", { name: /authorize this audit/i })).toBeNull();
  });

  it("will not authorize until a person affirms they are allowed to", () => {
    // Arrange
    renderSection();
    const authorize = screen.getByRole("button", { name: /authorize this audit/i });

    // Act — everything filled in except the affirmation.
    fireEvent.change(screen.getByLabelText("Origin"), {
      target: { value: "https://shop.example" },
    });
    fireEvent.change(screen.getByLabelText("Asserted by"), { target: { value: "owner" } });

    // Assert — FR-160: absent authorization there is no audit, and a checkbox
    // that defaulted to ticked would be authorizing on the operator's behalf.
    expect(authorize.hasAttribute("disabled")).toBe(true);
  });

  it("authorizes one origin once the affirmation is given", () => {
    // Arrange
    const { onAuthorize } = renderSection();
    fireEvent.change(screen.getByLabelText("Origin"), {
      target: { value: "https://shop.example" },
    });
    fireEvent.change(screen.getByLabelText("Asserted by"), { target: { value: "owner" } });
    fireEvent.click(screen.getByLabelText(/i am authorized/i));

    // Act
    fireEvent.click(screen.getByRole("button", { name: /authorize this audit/i }));

    // Assert
    expect(onAuthorize).toHaveBeenCalledWith("https://shop.example", "owner");
  });

  it("offers the packs and picks none of them for you", () => {
    // Arrange / Act
    renderSection({ audit: LIVE });

    // Assert — FR-161: a pack is offered and the operator selects it
    // explicitly, because the pack decides whether a write path is exercised.
    const select = screen.getByLabelText(/contract pack/i);
    expect((select as HTMLSelectElement).value).toBe("");
    expect(screen.getByRole("option", { name: CART_PACK.title })).toBeDefined();
  });

  it("generates a collector that refuses to invoke checkout", () => {
    // Arrange
    renderSection({ audit: LIVE });

    // Act — choosing the cart pack reveals the snippet.
    fireEvent.change(screen.getByLabelText(/contract pack/i), { target: { value: "shopify_cart" } });

    // Assert — FR-162. `proceed_to_checkout` must appear only as forbidden,
    // never in the map of tools the collector will exercise.
    const collector = screen.getByText(/ActionWitness collector/);
    const source = collector.textContent ?? "";
    expect(source).toContain('"proceed_to_checkout"');
    expect(source).toContain("FORBIDDEN");
    // The exercisable map lists the read/cart tools and stops there.
    expect(source).toContain('"get_cart": { args: {}');
    expect(source).toContain('"update_cart": { args: {}');
    expect(source).not.toContain('"proceed_to_checkout": { args: {}');
    expect(source).not.toContain('"manage_orders": { args: {}');
  });

  it("reads the cart through the platform's own session, not through the harness", () => {
    // Arrange / Act
    renderSection({ audit: LIVE });
    fireEvent.change(screen.getByLabelText(/contract pack/i), { target: { value: "shopify_cart" } });

    // Assert — §25.8's locale-aware same-session read. If this ever became a
    // call back to the harness, the observation would stop being independent
    // of the thing it observes.
    const source = screen.getByText(/ActionWitness collector/).textContent ?? "";
    expect(source).toContain("cart.js");
    expect(source).toContain('credentials: "same-origin"');
    expect(source).not.toContain("/api/v1");
  });

  it("renders the merchant summary and keeps the limits visible", () => {
    // Arrange / Act
    renderSection({
      audit: LIVE,
      outcome: {
        reportArtifactId: "art_1",
        contentHash: "sha256:abc",
        report: {
          audited_site: "https://shop.example",
          summary: {
            headline: "One tool said it worked when it had not.",
            what_this_means: "A shopper's cart would not have changed.",
            tools: [
              { tool: "update_cart", says: "reported success", what_to_do: "Fix this first." },
            ],
            not_checked: ["proceed_to_checkout"],
            limits: ["A clean audit is not a guarantee."],
          },
          evidence: [],
        },
      },
    });

    // Assert
    expect(screen.getByText(/One tool said it worked when it had not\./)).toBeDefined();
    expect(screen.getByText(/Fix this first\./)).toBeDefined();
    expect(screen.getByText(/proceed_to_checkout/)).toBeDefined();
    expect(screen.getByText(/A clean audit is not a guarantee\./)).toBeDefined();
  });

  it("does not render a report field that arrived in the wrong shape", () => {
    // Arrange / Act — the report is composed server-side but still crosses the
    // boundary as `unknown`; `String()` on an object would print
    // "[object Object]" into a report somebody forwards to their developer.
    renderSection({
      audit: LIVE,
      outcome: {
        reportArtifactId: "art_1",
        contentHash: "sha256:abc",
        report: { summary: { headline: { unexpected: true }, tools: [] }, evidence: [] },
      },
    });

    // Assert
    expect(screen.queryByText(/object Object/)).toBeNull();
  });
});
