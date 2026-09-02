/**
 * 015-T4 — the fixture storefront that lies (§12.17).
 *
 * A test bed is only useful if it genuinely reproduces the failure, so this
 * asserts the fixture's dishonesty directly: the cart tool returns a success
 * whose text is *identical* to the working version's, and the cart does not
 * move.
 *
 * The honest variant is tested too. Without it a green audit would be
 * unfalsifiable — a harness that reported "silently failed" for every surface
 * would pass the headline test and be useless.
 */

import { describe, expect, it } from "vitest";

import { EMPTY_CART, halfBrokenStorefront } from "./halfBrokenStorefront";
import { ModelContextDouble } from "./modelContextDouble";

async function surface(cartToolLies: boolean) {
  const fixture = halfBrokenStorefront({ cartToolLies });
  const modelContext = new ModelContextDouble();
  await fixture.register(modelContext);
  return { fixture, modelContext };
}

describe("the half-broken storefront", () => {
  it("publishes read tools that genuinely work", async () => {
    // What makes the surface convincing to whoever shipped it.
    const { modelContext } = await surface(true);

    const names = (await modelContext.getTools()).map((tool) => tool.name).sort();

    expect(names).toEqual(["get_cart", "proceed_to_checkout", "search_catalog", "update_cart"]);
  });

  it("reports success from the cart tool while the cart stays empty", async () => {
    // The Allbirds-shaped failure, reproduced in a fixture we own — the
    // guardrails forbid demonstrating this against somebody else's store.
    const { fixture, modelContext } = await surface(true);

    const result = await modelContext.invoke("update_cart", { variant_id: 111, quantity: 1 });

    expect(JSON.stringify(result)).toContain("success");
    expect(fixture.readCart()).toEqual(EMPTY_CART);
  });

  it("returns the same words when it is telling the truth", async () => {
    // The reason a call-level evaluator cannot tell the two apart, and the
    // reason an independent read can. If the lying variant answered
    // differently, the fixture would be demonstrating a much easier problem.
    const lying = await surface(true);
    const honest = await surface(false);

    const fromLiar = await lying.modelContext.invoke("update_cart", {
      variant_id: 111,
      quantity: 1,
    });
    const fromHonest = await honest.modelContext.invoke("update_cart", {
      variant_id: 111,
      quantity: 1,
    });

    expect(JSON.stringify(fromLiar)).toEqual(JSON.stringify(fromHonest));
  });

  it("actually updates the cart when it is not lying", async () => {
    // The falsifiability half.
    const { fixture, modelContext } = await surface(false);

    await modelContext.invoke("update_cart", { variant_id: 111, quantity: 1 });

    expect(fixture.readCart().item_count).toBe(1);
    expect(fixture.readCart().total_price).toBe(2599);
  });

  it("reads the cart through the same state the observation channel reads", async () => {
    // If the tool wrote somewhere the read could not see, the fixture would
    // prove the harness works by rigging it.
    const { fixture, modelContext } = await surface(false);
    await modelContext.invoke("update_cart", { variant_id: 111, quantity: 2 });

    const viaTool = await modelContext.invoke("get_cart", {});

    expect(JSON.stringify(viaTool)).toContain(String(fixture.readCart().total_price));
  });

  it("refuses to let anything exercise checkout", async () => {
    // FR-162 forbids it against an external target. The fixture publishes the
    // tool so the audit has something real to report as present-but-unexercised,
    // and makes calling it fail loudly so a test cannot quietly do so.
    const { modelContext } = await surface(true);

    await expect(modelContext.invoke("proceed_to_checkout", {})).rejects.toThrow(
      /refuses/,
    );
  });
});
