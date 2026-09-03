/**
 * The collector script an operator runs on a storefront they are auditing.
 *
 * **This module emits source text; it performs no WebMCP access.** Nothing here
 * runs in this page. The string below is JavaScript for a *different document* —
 * the audited storefront's — and it is written out so a person can paste it
 * into that page's console, because that is the only place the evidence exists.
 *
 * A document can enumerate only its own `modelContext`, and `cart.js` is a read
 * of the caller's own session; neither crosses an origin. The alternative — the
 * harness fetching either itself — is what §12.17 forbids and what
 * `tests/architecture/test_audit_guardrails.py` fails the build over, so the
 * snippet is not a convenience, it is the boundary.
 *
 * It lives in its own file, and in `webmcp/`, for the isolation gate's sake: the
 * browser-API tokens below are unavoidable in a script *about* the browser API,
 * and a reviewer should be able to confirm at a glance that this file only ever
 * returns a string. Exempting the interactive component that shows it would
 * have exempted everything that component grows into later.
 */

/** The pack fields the collector needs, and no more. */
export interface CollectorPack {
  readonly signature: readonly string[];
  readonly neverInvoked: readonly string[];
}

/**
 * Build the collector for one pack.
 *
 * The never-invoked list is baked in from the pack rather than left to whoever
 * pastes the script (FR-162). `proceed_to_checkout` against a real storefront
 * creates a real order for a real customer, so the tools the script is willing
 * to exercise are computed here and the forbidden ones are named twice: once as
 * a filter, once as a guard inside the loop.
 *
 * `ARGUMENTS` is deliberately left empty for the operator to complete. Only
 * they know what a valid cart line looks like on their store, and a harness
 * that guessed would be sending invented input to somebody's shop.
 */
export function collectorFor(pack: CollectorPack): string {
  const exercisable = pack.signature.filter((name) => !pack.neverInvoked.includes(name));
  const plans = exercisable
    .map((name) => `    ${JSON.stringify(name)}: { args: {}, expectsStateChange: false },`)
    .join("\n");

  return `// ActionWitness collector — run this in the console ON THE STOREFRONT
// you are authorized to audit. It reads and exercises; it never checks out.
(async () => {
  // Fill in arguments for the tools you want exercised. A tool left out here is
  // reported as present but not tried, which is a finding, not a failure.
  const ARGUMENTS = {
${plans}
  };

  // Never invoked (FR-162): ${pack.neverInvoked.join(", ") || "none"}
  const FORBIDDEN = ${JSON.stringify(pack.neverInvoked)};

  const mc = document.modelContext ?? navigator.modelContext;
  if (!mc) { throw new Error("This page exposes no WebMCP surface."); }

  // §25.8's locale-aware same-session read. Your session, your store.
  const cartUrl = ((window.Shopify && window.Shopify.routes && window.Shopify.routes.root) || "/") + "cart.js";
  const readCart = async () => {
    try { return await (await fetch(cartUrl, { credentials: "same-origin" })).json(); }
    catch { return null; }  // No independent channel is a finding, not a crash.
  };

  const tools = await mc.getTools();
  const enumerated = tools.map((t) => t.name);
  const observed_before = await readCart();
  const reports = {};

  for (const [name, plan] of Object.entries(ARGUMENTS)) {
    if (FORBIDDEN.includes(name)) { continue; }
    const tool = tools.find((t) => t.name === name);
    if (!tool) { continue; }
    let summary;
    try { summary = JSON.stringify(await mc.executeTool(tool, JSON.stringify(plan.args))); }
    catch (error) { summary = String(error); }
    reports[name] = { summary, expects_state_change: plan.expectsStateChange === true };
  }

  const observed_after = await readCart();
  const transcript = { enumerated, reports, observed_before, observed_after };
  console.log(JSON.stringify(transcript, null, 2));
  return transcript;
})();`;
}
