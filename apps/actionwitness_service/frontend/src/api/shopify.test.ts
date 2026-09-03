/**
 * The pairing client's narrowing (§15.7, FR-113, constitution §5).
 *
 * These are boundary tests, not transport tests. Every field here arrives as
 * `unknown` from a server that is being written alongside this client, so what
 * matters is what happens when a payload is *not* the shape the UI hoped for:
 * a missing identifier must name itself, a changed money representation must
 * not be quietly converted, and a cosmetic envelope must not strand the panel.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { createShopifyPairing, readShopifyPairing } from "./shopify";

function respond(body: unknown, status = 200): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(body), { status })),
  );
}

const RECORD = {
  pairing_id: "pair_0000abcd",
  status: "armed",
  store_origin: "https://authorized-dev-store.example",
  contract_id: "con_1",
  expires_at: "2026-09-03T12:15:00Z",
  run_id: "run_1",
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("readShopifyPairing", () => {
  it("narrows a bare record", async () => {
    // Arrange
    respond(RECORD);

    // Act
    const pairing = await readShopifyPairing("pair_0000abcd");

    // Assert
    expect(pairing.pairingId).toBe("pair_0000abcd");
    expect(pairing.status).toBe("armed");
    expect(pairing.runId).toBe("run_1");
    // Absent and empty mean the same thing — no evidence has landed yet.
    expect(pairing.observations).toEqual([]);
  });

  it("narrows the same record inside a pairing envelope", async () => {
    // Arrange — both spellings are in use across this API.
    respond({ pairing: RECORD });

    // Act / Assert
    expect((await readShopifyPairing("pair_0000abcd")).status).toBe("armed");
  });

  it("names the field that was missing rather than failing later", async () => {
    // Arrange
    respond({ status: "armed" });

    // Act / Assert — a TypeError three components deeper is not a diagnosis.
    await expect(readShopifyPairing("x")).rejects.toThrow(/pairing_id/);
  });
});

describe("normalized cart evidence (FR-113)", () => {
  it("keeps money as the fixed-scale string the server sent", async () => {
    // Arrange
    respond({
      ...RECORD,
      observations: [
        {
          phase: "after",
          captured_at: "2026-09-03T12:05:00Z",
          content_hash: "sha256:bbb",
          capture_url_path: "/en-gb/cart.js",
          item_count: 1,
          currency: "USD",
          subtotal: "25.00",
          total: "25.00",
        },
      ],
    });

    // Act
    const pairing = await readShopifyPairing("pair_0000abcd");

    // Assert
    expect(pairing.observations[0]?.subtotal).toBe("25.00");
    expect(pairing.observations[0]?.capturePath).toBe("/en-gb/cart.js");
  });

  it("refuses a numeric money value instead of converting it", async () => {
    // Arrange — FR-113 normalizes minor units into fixed-scale decimal strings.
    // A number arriving here means the representation changed, and `2500 / 100`
    // is exactly the float arithmetic that representation exists to avoid. The
    // honest render is "not reported", which a person can see is wrong; a
    // silently converted 25 is a total they would believe.
    respond({
      ...RECORD,
      observations: [{ phase: "after", subtotal: 2500, total: 2500, item_count: 1 }],
    });

    // Act
    const pairing = await readShopifyPairing("pair_0000abcd");

    // Assert
    expect(pairing.observations[0]?.subtotal).toBeNull();
    expect(pairing.observations[0]?.total).toBeNull();
    expect(pairing.observations[0]?.itemCount).toBe(1);
  });
});

describe("createShopifyPairing", () => {
  it("requires the launch URL, because a pairing nobody can open is not one", async () => {
    // Arrange
    respond({ ...RECORD, status: "created" });

    // Act / Assert
    await expect(createShopifyPairing("con_1")).rejects.toThrow(/launch_url/);
  });

  it("returns the launch URL beside the record", async () => {
    // Arrange
    const launch = "https://authorized-dev-store.example/#actionwitness=pair_abcd.secret";
    respond({ ...RECORD, status: "created", launch_url: launch });

    // Act
    const created = await createShopifyPairing("con_1");

    // Assert — the caller is responsible for keeping it out of the DOM; this
    // boundary's job is only to hand over exactly what the server issued.
    expect(created.launchUrl).toBe(launch);
    expect(created.status).toBe("created");
  });
});
